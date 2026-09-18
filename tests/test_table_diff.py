"""Synthetic tables with different physical layouts; no customer content."""

from copy import deepcopy
import unittest

from cu_diff.compare import compare_documents
from cu_diff.web_evidence import web_result
from test_compare import item, operation, source


def bom_operation(part="SYN-A", manufacturer="ALPHA", quantity="1", unit="pcs",
                  split=False, row_number="1", order=None, compact=False):
    headers = {"row": "No", "description": "BOM ITEM", "quantity": "Q' TY",
               "part_number": "P/N", "flame_class": "FLAME CLASS",
               "material": "MATERIAL", "manufacturer": "manufacturer"}
    values = {"row": row_number, "description": "TEST COMPONENT",
              "quantity": quantity if split else quantity + ("" if compact else " ") + unit,
              "part_number": part, "flame_class": "RATING", "material": "SYN-MATERIAL",
              "manufacturer": manufacturer}
    keys = list(values)
    if split:
        headers.update(quantity="QUANTTY", unit="UNITS", part_number="PART NO.",
                       flame_class="Fireproofing rank")
        values["unit"] = unit
        keys.insert(3, "unit")
    keys = order or keys
    cells, sources = [], []
    for column, key in enumerate(keys):
        for row, content in ((0, headers[key]), (1, values[key])):
            evidence = source(x=column * 100 + 5, y=10 if row == 0 else 40, width=90, height=20)
            cells.append({"kind": "columnHeader" if row == 0 else "content",
                          "rowIndex": row, "columnIndex": column,
                          "rowSpan": 1, "columnSpan": 1, "content": content, "source": evidence})
            if row == 1:
                sources.append(evidence)
    response = operation([item(" ".join(values[key] for key in keys),
                               key=f"row {row_number}", evidence=";".join(sources))],
                         width=1000, height=500)
    response["result"]["contents"][0]["tables"] = [{
        "rowCount": 2, "columnCount": len(keys), "cells": cells,
    }]
    return response


def public_documents():
    return {role: {"id": role, "sha256": role, "pages": [
        {"number": 1, "width_pt": 720, "height_pt": 360}]} for role in ("old", "new")}


class CellTests(unittest.TestCase):
    def test_only_part_and_manufacturer_change_despite_split_quantity(self):
        old, new = bom_operation(), bom_operation("SYN-B", "BRAVO", split=True)
        before = deepcopy((old, new))
        report = compare_documents(old, new)
        record, = report["differences"]
        cells = record["cell_comparison"]
        self.assertEqual(cells["status"], "complete")
        self.assertEqual([f["key"] for f in cells["fields"] if f["change"] != "unchanged"],
                         ["part_number", "manufacturer"])
        public = web_result(report, public_documents(), {})["items"][0]
        for role in ("old", "new"):
            self.assertEqual([loc["field"] for loc in public[role]["locations"]],
                             ["part_number", "manufacturer"])
            self.assertEqual(len(public[role]["locations"]), 2)
            self.assertGreater(len(public[role]["context_locations"]), 2)
        self.assertEqual((old, new), before)

    def test_split_quantity_value_change_does_not_highlight_unchanged_unit(self):
        report = compare_documents(bom_operation(), bom_operation(quantity="2", split=True))
        field, = [f for f in report["differences"][0]["cell_comparison"]["fields"]
                  if f["change"] != "unchanged"]
        self.assertEqual(field["key"], "quantity")
        self.assertEqual(len(field["new"]["source"]), 1)
        self.assertIn("D(1,205,", field["new"]["source"][0])
        self.assertEqual(len(field["new"]["raw_cells"]), 2)

    def test_unit_only_change_does_not_highlight_unchanged_number(self):
        report = compare_documents(bom_operation(), bom_operation(unit="g", split=True))
        field, = [f for f in report["differences"][0]["cell_comparison"]["fields"]
                  if f["change"] != "unchanged"]
        self.assertEqual(field["key"], "quantity")
        self.assertEqual(len(field["new"]["source"]), 1)
        self.assertIn("D(1,305,", field["new"]["source"][0])

    def test_quantity_spacing_is_formatting_not_a_quantity_change(self):
        report = compare_documents(bom_operation(compact=True), bom_operation(split=True))
        record, = report["differences"]
        self.assertEqual(record["change"], "formatting_only")
        self.assertEqual(report["coverage"]["schema"]["modified_pairs"], 0)
        self.assertEqual(report["coverage"]["schema"]["formatting_only_pairs"], 1)

    def test_relocation_highlights_row_number_only(self):
        report = compare_documents(bom_operation(), bom_operation(row_number="8", split=True))
        record, = report["differences"]
        self.assertEqual(record["change"], "relocated")
        public = web_result(report, public_documents(), {})["items"][0]
        for role in ("old", "new"):
            self.assertEqual([loc["field"] for loc in public[role]["locations"]], ["row"])

    def test_relocation_and_material_change_are_separate_columns(self):
        old, new = bom_operation(), bom_operation(row_number="8")
        new["result"]["contents"][0]["tables"][0]["cells"][11]["content"] = "OTHER-MATERIAL"
        raw = new["result"]["contents"][0]["fields"]["Items"]["valueArray"][0]["valueObject"]["RawText"]
        raw["valueString"] = raw["valueString"].replace("SYN-MATERIAL", "OTHER-MATERIAL")
        record, = compare_documents(old, new)["differences"]
        self.assertEqual(record["change"], "modified")
        self.assertEqual([f["key"] for f in record["cell_comparison"]["fields"]
                          if f["change"] != "unchanged"], ["row", "material"])

    def test_reordered_columns_match_headers_not_column_positions(self):
        order = ["row", "description", "quantity", "material", "flame_class", "part_number", "manufacturer"]
        record, = compare_documents(bom_operation(), bom_operation("SYN-B", order=order))["differences"]
        changed, = [f for f in record["cell_comparison"]["fields"] if f["change"] != "unchanged"]
        self.assertEqual(changed["key"], "part_number")
        self.assertIn("D(1,505,", changed["new"]["source"][0])

    def test_no_tables_never_fall_back_to_whole_row_red_boxes(self):
        old, new = bom_operation(), bom_operation("SYN-B")
        del new["result"]["contents"][0]["tables"]
        report = compare_documents(old, new)
        self.assertEqual(report["differences"][0]["cell_comparison"]["status"], "unavailable")
        public = web_result(report, public_documents(), {})["items"][0]
        for role in ("old", "new"):
            self.assertEqual(public[role]["locations"], [])
            self.assertTrue(public[role]["context_locations"])
            self.assertIn("不将整行画成差异框", public[role]["location_error"])

    def test_conflicting_schema_and_table_values_are_not_silently_overwritten(self):
        old, new = bom_operation(), bom_operation("SYN-B")
        new["result"]["contents"][0]["tables"][0]["cells"][7]["content"] = "CONFLICT"
        record, = compare_documents(old, new)["differences"]
        self.assertEqual(record["cell_comparison"]["status"], "unavailable")
        self.assertIn("文字不一致", record["cell_comparison"]["issues"][0])
        self.assertIn("SYN-B", record["new"]["raw_text"])

    def test_duplicate_tables_and_merged_cells_are_ambiguous(self):
        for mode in ("duplicate", "merged", "missing-source", "wrong-page", "renamed-header"):
            with self.subTest(mode=mode):
                old, new = bom_operation(), bom_operation("SYN-B")
                tables = new["result"]["contents"][0]["tables"]
                if mode == "duplicate":
                    tables.append(deepcopy(tables[0]))
                elif mode == "merged":
                    tables[0]["cells"][7]["columnSpan"] = 2
                elif mode == "missing-source":
                    tables[0]["cells"][7].pop("source")
                elif mode == "wrong-page":
                    for cell in tables[0]["cells"]:
                        cell["source"] = cell["source"].replace("D(1,", "D(2,")
                else:
                    tables[0]["cells"][6]["content"] = "UNRECOGNIZED HEADER"
                record, = compare_documents(old, new)["differences"]
                self.assertEqual(record["cell_comparison"]["status"], "unavailable")
                self.assertTrue(record["review_required"])


class DateTests(unittest.TestCase):
    def comparison(self, left, right, category="title", key="designed by"):
        return compare_documents(
            operation([item(left, category=category, key=key, region="title")]),
            operation([item(right, category=category, key=key, region="title")]))

    def test_valid_date_spacing_only_preserves_raw_and_excludes_modified_count(self):
        record, = self.comparison("Design by TEST 2024. 02. 29", "Design by TEST 2024.02.29")["differences"]
        self.assertEqual(record["change"], "formatting_only")
        self.assertEqual(record["old"]["raw_text"], "Design by TEST 2024. 02. 29")
        self.assertEqual(record["new"]["raw_text"], "Design by TEST 2024.02.29")

    def test_actual_dates_invalid_dates_and_identifier_spacing_remain_modified(self):
        cases = [
            ("2024. 02. 29", "2024.03.01", "title", "date"),
            ("2023. 02. 29", "2023.02.29", "title", "date"),
            ("2024. 02. 29", "2024.02.29", "title", "part number"),
            ("2024. 02. 29", "2024.02.29", "title", "design number"),
            ("PN-2024. 02. 29", "PN-2024.02.29", "title", "design reference"),
            ("0. 75", "0.75", "dimension", "date"),
            ("Design by TEST 2024. 02.29", "Design by OTHER 2024.02.29", "title", "date"),
        ]
        for left, right, category, key in cases:
            with self.subTest(left=left, right=right, key=key):
                record, = self.comparison(left, right, category, key)["differences"]
                self.assertEqual(record["change"], "modified")
