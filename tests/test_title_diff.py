"""Synthetic title grouping/geometry cases; no customer strings or service calls."""

from copy import deepcopy
import unittest

from cu_diff.compare import compare_documents
from cu_diff.web_evidence import web_result
from test_compare import item, line, operation, source


def titles(value="PART-A", *, combined=False, label="Customer P/N", x=600, y=700):
    label_source = source(x=x, y=y, width=100)
    value_source = source(x=x+80, y=y+12, width=160, height=14)
    if combined:
        items = [item(f"{label} : {value}", category="title", region="title",
                      key="generated combined identity", evidence=label_source+";"+value_source)]
    else:
        items = [
            item(label+" :", category="title", region="title", key="generated label identity",
                 evidence=label_source),
            item(value, category="label", region="title", key="generated value identity",
                 evidence=value_source)]
    return operation(items, [line(label+" :", evidence=label_source),
                             line(value, evidence=value_source)])


class TitleDiffTests(unittest.TestCase):
    def pairs(self, result):
        return [r for r in result["differences"] + result["unchanged"]
                if r["match"]["method"] == "printed_title_label_value"]

    def test_split_combined_title_compares_values_and_preserves_sources(self):
        old, new = titles(), titles("N/A", combined=True)
        originals = deepcopy((old, new))
        report = compare_documents(old, new)
        pair, = self.pairs(report)
        self.assertEqual(pair["change"], "modified")
        self.assertEqual(pair["key"], "Customer P/N")
        self.assertEqual(pair["old"]["raw_text"], "PART-A")
        self.assertEqual(pair["new"]["raw_text"], "N/A")
        self.assertEqual(len(pair["old"]["schema_sources"]), 2)
        self.assertEqual(pair["new"]["schema_sources"][0]["raw_text"], "Customer P/N : N/A")
        self.assertEqual(pair["new"]["source"], new["result"]["contents"][0]["pages"][0]["lines"][1]["source"])
        self.assertEqual(report["coverage"]["schema"]["matched_old"], 2)
        self.assertEqual(report["coverage"]["schema"]["matched_new"], 1)
        self.assertEqual((old, new), originals)
        docs = {role: {"pages": [{"number": 1, "width_pt": 1000, "height_pt": 1000}]}
                for role in ("old", "new")}
        output = web_result(report, docs, {})
        title, = [r for r in output["items"] if r["match"]["method"] == "printed_title_label_value"]
        for role in ("old", "new"):
            loc, = title[role]["locations"]
            self.assertAlmostEqual(loc["x"], .68)
            self.assertAlmostEqual(loc["y"], .712)
            self.assertAlmostEqual(loc["width"], .16)

    def test_inverse_grouping_and_equal_value_do_not_invent_text_change(self):
        result = compare_documents(titles(combined=True), titles())
        pair, = self.pairs(result)
        self.assertEqual(pair["change"], "unchanged")
        changed, = self.pairs(compare_documents(titles("PART-OLD", combined=True), titles("PART-NEW")))
        self.assertEqual(changed["change"], "modified")

    def test_same_grouping_still_compares_values_not_generated_keys(self):
        for combined in (False, True):
            with self.subTest(combined=combined):
                pair, = self.pairs(compare_documents(
                    titles(combined=combined), titles("PART-B", combined=combined)))
                self.assertEqual(pair["old"]["raw_text"], "PART-A")
                self.assertEqual(pair["new"]["raw_text"], "PART-B")
                self.assertEqual(pair["change"], "modified")

    def test_arbitrary_printed_owner_and_part_number_alias(self):
        old = titles(label="SUPPLIER X P/N")
        new = titles("PART-B", combined=True, label="supplier x Part Number")
        pair, = self.pairs(compare_documents(old, new))
        self.assertEqual(pair["key"], "SUPPLIER X P/N")
        self.assertEqual(pair["new"]["raw_text"], "PART-B")

    def test_neighboring_other_field_label_is_not_selected_as_value(self):
        old = titles()
        old["result"]["contents"][0]["fields"]["Items"]["valueArray"].append(
            item("DWG. NO :", category="title", region="title", key="drawing label",
                 evidence=source(x=600, y=729, width=100)))
        pair, = self.pairs(compare_documents(old, titles("PART-B", combined=True)))
        self.assertEqual(pair["old"]["raw_text"], "PART-A")

    def test_duplicate_labels_and_ambiguous_value_candidates_remain_unresolved(self):
        for problem in ("duplicate", "two_values"):
            old = titles()
            items = old["result"]["contents"][0]["fields"]["Items"]["valueArray"]
            if problem == "duplicate":
                items.extend(titles("OTHER", x=200, y=500)["result"]["contents"][0]["fields"]["Items"]["valueArray"])
            else:
                items.append(item("CONFLICT", category="label", region="title",
                                  evidence=source(x=680, y=714, width=150)))
            self.assertEqual(self.pairs(compare_documents(old, titles("PART-B", combined=True))), [])

    def test_missing_or_conflicting_ocr_value_source_does_not_invent_box(self):
        for problem in ("missing", "wrong_text", "far_away", "conflicting_size"):
            new = titles("PART-B", combined=True)
            page = new["result"]["contents"][0]["pages"][0]
            if problem == "missing":
                page["lines"] = []
            elif problem == "wrong_text":
                page["lines"][1]["content"] = "OCR-DISAGREEMENT"
            elif problem == "far_away":
                page["lines"][1]["source"] = source(x=10, y=10)
            else:
                page["width"] = 100
            self.assertEqual(self.pairs(compare_documents(titles(), new)), [])

    def test_other_regions_and_unlabelled_number_similarity_are_not_title_identity(self):
        old, new = titles(), titles("PART-B", combined=True)
        for raw in old["result"]["contents"][0]["fields"]["Items"]["valueArray"]:
            raw["valueObject"]["Region"]["valueString"] = "cable"
        self.assertEqual(self.pairs(compare_documents(old, new)), [])

    def test_missing_label_separator_or_invalid_page_geometry_stays_unresolved(self):
        for problem in ("header", "unknown_unit", "infinite_width", "duplicate_page", "missing_source"):
            old, new = titles(), titles("PART-B", combined=True)
            content = old["result"]["contents"][0]
            raw_label = content["fields"]["Items"]["valueArray"][0]["valueObject"]["RawText"]
            if problem == "header":
                raw_label["valueString"] = "Customer P/N"
            elif problem == "unknown_unit":
                content["pages"][0]["unit"] = "unknown"
            elif problem == "infinite_width":
                content["pages"][0]["width"] = float("inf")
            elif problem == "duplicate_page":
                content["pages"].append(deepcopy(content["pages"][0]))
            else:
                raw_label["source"] = ""
            self.assertEqual(self.pairs(compare_documents(old, new)), [])

    def test_category_case_is_metadata_not_a_new_document_change(self):
        old = operation([item("1 SYNTHETIC COVER 1 pcs", category="bom")])
        new = operation([item("1 SYNTHETIC COVER 1 pcs", category="BOM")])
        result = compare_documents(old, new)
        record, = result["unchanged"]
        self.assertEqual(record["old"]["category"], "BOM")
        self.assertEqual(record["old"]["raw_category"], "bom")
        self.assertFalse(any("unknown category" in warning for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
