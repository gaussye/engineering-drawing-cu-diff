"""Independent CU table evidence; all row text and coordinates are synthetic."""

from copy import deepcopy
import unittest

from cu_diff.compare import _extract, _record
from cu_diff.table_diff import reconcile_bom, refine_bom
from test_compare import item, operation, source


def table_operation(rows, bottom=True, explicit=False, schema=True, category="BOM"):
    headers = ["ITEM", "DESCRIPTION", "Q'TY", "U/M"]
    cells, items = [], []
    header_index = len(rows) if bottom else 0
    for ri, values in [(header_index, headers)] + [
            (index if bottom else index + 1, values) for index, values in enumerate(rows)]:
        sources = []
        for ci, value in enumerate(values):
            evidence = source(x=ci * 140 + 5, y=ri * 30 + 5, width=130, height=20)
            cells.append({"kind": "columnHeader" if explicit and ri == header_index else "content",
                          "rowIndex": ri, "columnIndex": ci, "content": value, "source": evidence})
            sources.append(evidence)
        if schema and ri != header_index:
            items.append(item(" ".join(values), key=f"row {values[0]}", category=category,
                              evidence=";".join(sources)))
    result = operation(items)
    result["result"]["contents"][0]["tables"] = [{
        "rowCount": len(rows) + 1, "columnCount": 4, "cells": cells,
    }]
    return result


BASE = [["1", "COMPONENT A", "As list", "EA"],
        ["2", "COMPONENT B 2.5A 250V", "1", "EA"]]
ADDED = ["3", "ADDED LABEL", "1", "EA"]


def unpaired(old, new):
    records = []
    for side, operation_value in (("old", old), ("new", new)):
        entries = _extract(operation_value, side, 0.8)[0]
        for entry in entries:
            records.append(_record(entry if side == "old" else None,
                                   entry if side == "new" else None, "unpaired", 0, "low"))
    return records


def table(op):
    return op["result"]["contents"][0]["tables"][0]


class ReconcileTests(unittest.TestCase):
    def test_bottom_content_headers_addition_and_inverse_are_actual_evidence(self):
        for inverse in (False, True):
            with self.subTest(inverse=inverse):
                old, new = table_operation(BASE, category="bom"), table_operation(BASE + [ADDED])
                if inverse:
                    old, new = new, old
                before = deepcopy((old, new))
                records = unpaired(old, new)
                self.assertEqual(reconcile_bom(records, old, new), [])
                refine_bom(records, old, new)
                self.assertEqual(len(records), 3)
                record, = [r for r in records if r["change"].startswith("table_row_")]
                actual, missing = ("old", "new") if inverse else ("new", "old")
                self.assertEqual(record["change"], "table_row_removed" if inverse else "table_row_added")
                self.assertIsNone(record[missing])
                self.assertEqual(record[actual]["raw_text"], "3 ADDED LABEL 1 EA")
                self.assertTrue(record[actual]["polygons"])
                self.assertEqual(record["table_comparison"]["status"], "complete")
                self.assertTrue(record["table_context"][missing]["polygons"])
                self.assertIn("do not prove absence", record["review_reasons"][-1])
                self.assertEqual((old, new), before)

    def test_missing_schema_row_is_supplemented_not_fabricated(self):
        old, new = table_operation(BASE), table_operation(BASE + [ADDED])
        new["result"]["contents"][0]["fields"]["Items"]["valueArray"].pop()
        records = unpaired(old, new)
        reconcile_bom(records, old, new)
        record, = [r for r in records if r["change"] == "table_row_added"]
        self.assertEqual(record["table_comparison"]["supplemented"], {"old": False, "new": True})
        self.assertEqual(record["table_comparison"]["schema_entry_ids"]["new"], [])
        self.assertEqual(record["new"]["schema_item_ids"], [])
        self.assertEqual(record["new"]["source"], [c["source"] for c in table(new)["cells"]
                                                 if c["rowIndex"] == 2])
        self.assertEqual(len(record["new"]["page_context"]), 4)
        self.assertEqual(record["table_comparison"]["coverage"]["new"]["sourced_cells"], 16)
        self.assertIsNone(record["old"])

    def test_schema_free_tables_and_explicit_header_match(self):
        old = table_operation(BASE, schema=False, explicit=True, bottom=False)
        new = table_operation(BASE + [ADDED], schema=False)
        records = []
        self.assertEqual(reconcile_bom(records, old, new), [])
        self.assertEqual(len(records), 3)
        self.assertEqual([r["change"] for r in records], ["unchanged", "unchanged", "table_row_added"])

    def test_reordered_columns_use_roles_and_renumbered_anchors(self):
        new_rows = deepcopy(BASE) + [ADDED]
        new_rows[0][0], new_rows[1][0] = "5", "4"
        old, new = table_operation(BASE), table_operation(new_rows, schema=False)
        for cell in table(new)["cells"]:
            column = cell["columnIndex"]
            cell["columnIndex"] = 3 - column
            cell["source"] = source(x=(3 - column) * 140 + 5,
                                    y=cell["rowIndex"] * 30 + 5, width=130, height=20)
        records = unpaired(old, new)
        reconcile_bom(records, old, new)
        refine_bom(records, old, new)
        self.assertEqual([r["change"] for r in records], ["relocated", "relocated", "table_row_added"])
        for record in records[:2]:
            self.assertEqual([f["key"] for f in record["cell_comparison"]["fields"]
                              if f["change"] != "unchanged"], ["row"])

    def test_changed_rating_uses_actual_description_cells_only(self):
        changed = deepcopy(BASE)
        changed[1][1] = "COMPONENT B 16A 250V"
        old, new = table_operation(BASE), table_operation(changed + [ADDED])
        records = unpaired(old, new)
        reconcile_bom(records, old, new)
        refine_bom(records, old, new)
        record, = [r for r in records if r["change"] == "modified"]
        field, = [f for f in record["cell_comparison"]["fields"] if f["change"] != "unchanged"]
        self.assertEqual(field["key"], "description")
        for side, expected in (("old", "COMPONENT B 2.5A 250V"), ("new", "COMPONENT B 16A 250V")):
            self.assertEqual(field[side]["raw_text"], expected)
            self.assertEqual(len(field[side]["source"]), 1)
            self.assertEqual(field[side]["source"][0], table(old if side == "old" else new)["cells"][9]["source"])

    def test_callout_same_text_remains_independent(self):
        old, new = table_operation(BASE), table_operation(BASE + [ADDED])
        callout = item("3 ADDED LABEL 1 EA", key="callout", category="bom",
                       evidence=source(y=800))
        new["result"]["contents"][0]["fields"]["Items"]["valueArray"].append(callout)
        records = unpaired(old, new)
        reconcile_bom(records, old, new)
        self.assertEqual(len(records), 4)
        self.assertEqual(sum(r["change"] == "table_row_added" for r in records), 1)
        callout_record, = [r for r in records if r["key"] == "callout"]
        self.assertEqual(callout_record["change"], "unpaired_new")
        self.assertNotIn("table_comparison", callout_record)

    def test_existing_pair_and_duplicate_schema_records_coalesce(self):
        old, new = table_operation(BASE), table_operation(BASE + [ADDED])
        left, right = _extract(old, "old", 0.8)[0], _extract(new, "new", 0.8)[0]
        records = [_record(left[0], right[0], "exact", 1, "high"),
                   _record(left[1], None, "unpaired", 0, "low"),
                   _record(None, right[1], "unpaired", 0, "low"),
                   _record(None, right[2], "unpaired", 0, "low"),
                   _record(None, deepcopy(right[2]), "unpaired", 0, "low")]
        reconcile_bom(records, old, new)
        self.assertEqual(len(records), 3)
        self.assertEqual(sum(r["change"] == "unchanged" for r in records), 2)
        added, = [r for r in records if r["change"] == "table_row_added"]
        self.assertEqual(added["new"]["schema_item_ids"], [right[2]["id"]])
        for record in records:
            for side in ("old", "new"):
                if record[side] is not None:
                    self.assertEqual(len(record[side]["schema_item_ids"]), 1)

    def test_coalesced_distinct_schema_ids_count_original_sources(self):
        old, new = table_operation(BASE), table_operation(BASE + [ADDED])
        items = new["result"]["contents"][0]["fields"]["Items"]["valueArray"]
        items.append(deepcopy(items[-1]))
        records = unpaired(old, new)
        reconcile_bom(records, old, new)
        added, = [r for r in records if r["change"] == "table_row_added"]
        self.assertEqual(added["new"]["schema_item_ids"], ["new:item:0:2", "new:item:0:3"])
        self.assertEqual(added["table_context"]["old"]["schema_item_ids"], [])
        self.assertEqual(added["table_context"]["new"]["schema_item_ids"], [])

    def test_incomplete_or_ambiguous_tables_never_claim_row_presence(self):
        modes = ("missing-table", "missing-cell", "missing-source", "header-source",
                 "header-only", "duplicate-table", "duplicate-row", "duplicate-identity",
                 "merged-cell", "unknown-content-header", "invalid-grid", "failed-operation")
        for mode in modes:
            with self.subTest(mode=mode):
                old, new = table_operation(BASE), table_operation(BASE + [ADDED])
                target = table(new)
                if mode == "missing-table":
                    new["result"]["contents"][0].pop("tables")
                elif mode == "missing-cell":
                    target["cells"].pop()
                elif mode == "missing-source":
                    target["cells"][-1].pop("source")
                elif mode == "header-source":
                    target["cells"][0].pop("source")
                elif mode == "header-only":
                    target["cells"] = target["cells"][:4]
                    target["rowCount"] = 1
                    for cell in target["cells"]:
                        cell["rowIndex"] = 0
                elif mode == "duplicate-table":
                    new["result"]["contents"][0]["tables"].append(deepcopy(target))
                elif mode == "duplicate-row":
                    new = table_operation(BASE + [["2", "ADDED LABEL", "1", "EA"]])
                elif mode == "duplicate-identity":
                    new = table_operation(BASE + [["3", "COMPONENT A", "1", "EA"]])
                elif mode == "merged-cell":
                    target["cells"][-1]["columnSpan"] = 2
                elif mode == "unknown-content-header":
                    target["cells"][3]["content"] = "OTHER"
                elif mode == "invalid-grid":
                    target["rowCount"] = None
                else:
                    new["status"] = "Running"
                records = unpaired(old, new)
                before = deepcopy(records)
                self.assertTrue(reconcile_bom(records, old, new))
                self.assertEqual(records, before)

    def test_row_number_alone_and_unresolved_substitutions_are_not_matches(self):
        for values in ([["1", "UNRELATED ITEM", "4", "EA"]],
                       [BASE[0], ["2", "UNRELATED ITEM", "4", "EA"]]):
            with self.subTest(values=values):
                old, new = table_operation(BASE), table_operation(values)
                records = unpaired(old, new)
                before = deepcopy(records)
                self.assertTrue(reconcile_bom(records, old, new))
                self.assertEqual(records, before)

    def test_conflicting_schema_is_not_overwritten_or_duplicated(self):
        old, new = table_operation(BASE), table_operation(BASE + [ADDED])
        table(new)["cells"][5]["content"] = "DIFFERENT COMPONENT"
        records = unpaired(old, new)
        before = deepcopy(records)
        self.assertTrue(reconcile_bom(records, old, new))
        self.assertEqual(records, before)

    def test_uppercase_quantity_spacing_and_as_list_remain_formatting_only(self):
        old, new = table_operation(BASE), table_operation(BASE)
        raw_items = new["result"]["contents"][0]["fields"]["Items"]["valueArray"]
        for raw in raw_items:
            field = raw["valueObject"]["RawText"]
            field["valueString"] = field["valueString"].replace("1 EA", "1EA").replace("As list EA", "As listEA")
        records = unpaired(old, new)
        reconcile_bom(records, old, new)
        refine_bom(records, old, new)
        self.assertEqual([r["change"] for r in records], ["formatting_only", "formatting_only"])

    def test_nonnumeric_quantity_unit_edit_highlights_only_unit_cell(self):
        changed = deepcopy(BASE)
        changed[0][3] = "SETS"
        old, new = table_operation(BASE), table_operation(changed)
        records = unpaired(old, new)
        reconcile_bom(records, old, new)
        refine_bom(records, old, new)
        record, = [r for r in records if r["change"] == "modified"]
        field, = [f for f in record["cell_comparison"]["fields"] if f["change"] != "unchanged"]
        self.assertEqual(field["key"], "quantity")
        for side in ("old", "new"):
            self.assertEqual(len(field[side]["source"]), 1)
            self.assertEqual(field[side]["source"][0], table(old if side == "old" else new)["cells"][7]["source"])

    def test_uppercase_ea_quantity_edit_preserves_unit_without_highlighting_it(self):
        changed = deepcopy(BASE)
        changed[1][2] = "2"
        old, new = table_operation(BASE), table_operation(changed)
        records = unpaired(old, new)
        reconcile_bom(records, old, new)
        refine_bom(records, old, new)
        record, = [r for r in records if r["change"] == "modified"]
        field, = [f for f in record["cell_comparison"]["fields"] if f["change"] != "unchanged"]
        self.assertEqual(field["key"], "quantity")
        for side, quantity in (("old", "1"), ("new", "2")):
            self.assertEqual(field[side]["raw_text"], f"{quantity} EA")
            self.assertEqual(len(field[side]["source"]), 1)
            self.assertEqual(field[side]["source"][0], table(old if side == "old" else new)["cells"][10]["source"])
            self.assertEqual(field[side]["raw_cells"][-1]["content"], "EA")

    def test_no_tables_is_noop_not_an_unrelated_report_warning(self):
        records = []
        self.assertEqual(reconcile_bom(records, operation(), operation()), [])
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
