"""Synthetic table evidence contracts for the browser adapter and local report."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from cu_diff.report import write_report
from cu_diff.web_evidence import web_result


def entry(text, y):
    return {
        "raw_text": text, "source": "Synthetic cell source", "confidence": None,
        "polygons": [{"page_number": 1, "points": [[1, y], [3, y], [3, y+.1], [1, y+.1]]}],
        "page_context": [{"page_number": 1, "width": 10, "height": 5, "unit": "inch"}],
    }


class TableEvidenceTests(unittest.TestCase):
    def test_missing_row_stays_none_and_table_context_is_separate(self):
        record = {
            "region": "BOM", "key": "SYNTHETIC LABEL", "change": "table_row_added",
            "old": None, "new": entry("9 SYNTHETIC LABEL 1 EA", 2),
            "review_required": True, "review_reasons": ["Verify original drawing"],
            "match": {"method": "complete_table_rows", "score": 1, "certainty": "uncertain"},
            "table_comparison": {"status": "complete"},
            "table_context": {"old": entry("Old table context", 1),
                              "new": entry("New table context", 1)},
        }
        report = {"differences": [record], "unchanged": [], "ocr_differences": [],
                  "coverage": {}, "warnings": []}
        original = deepcopy(report)
        docs = {side: {"pages": [{"number": 1, "width_pt": 720, "height_pt": 360}]}
                for side in ("old", "new")}
        item, = web_result(report, docs, {})["items"]
        self.assertIsNone(item["old"])
        self.assertEqual(len(item["new"]["locations"]), 1)
        self.assertEqual(len(item["table_context"]["old"]["locations"]), 1)
        self.assertNotEqual(item["new"]["locations"], item["table_context"]["new"]["locations"])
        self.assertEqual(item["table_comparison"]["status"], "complete")
        self.assertEqual(report, original)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.md"
            write_report(report, output)
            text = output.read_text(encoding="utf-8")
            self.assertIn("新增行候选", text)
            self.assertIn("对应CU表格未提取到该行", text)
            self.assertIn("不伪造", text)

    def test_invalid_table_context_does_not_supply_a_guessed_location(self):
        row = entry("SYNTHETIC", 2)
        context = entry("Table", 1)
        context["page_context"][0]["width"] = 20
        report = {
            "differences": [{"change": "table_row_removed", "old": row, "new": None,
                             "table_comparison": {"status": "complete"},
                             "table_context": {"old": context, "new": context}}],
            "unchanged": [], "ocr_differences": [], "coverage": {}, "warnings": [],
        }
        docs = {side: {"pages": [{"number": 1, "width_pt": 720, "height_pt": 360}]}
                for side in ("old", "new")}
        item, = web_result(report, docs, {})["items"]
        self.assertIsNone(item["new"])
        self.assertEqual(item["table_context"]["new"]["locations"], [])
        self.assertTrue(item["table_context"]["new"]["location_error"])


class SemanticTextEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.record = {
            "key": "", "region": "OCR", "change": "modified", "review_required": True,
            "review_reasons": ["Model correspondence needs review"],
            "match": {"method": "llm_source_id_pairing", "certainty": "model_proposed", "score": None},
            "old": entry("Q=4.50±0.2", 1), "new": entry("Q=5.75±0.2", 2),
            "semantic_pairing": {"status": "supported", "rationale": "Same printed parameter"},
            "text_comparison": {"status": "complete", "issues": [],
                                "old": [entry("4.50", 1)], "new": [entry("5.75", 2)],
                                "changed_text": {"old": ["4.50"], "new": ["5.75"]}},
        }
        for side in ("old", "new"):
            points = self.record["text_comparison"][side][0]["polygons"][0]["points"]
            for p in points:
                p[0] = 1.6 if p[0] == 1 else 2.0
        self.report = {"differences": [], "ocr_differences": [self.record], "unchanged": [],
                       "coverage": {}, "warnings": []}
        self.docs = {side: {"pages": [{"number": 1, "width_pt": 720, "height_pt": 360}]}
                     for side in ("old", "new")}

    def test_semantic_d_record_keeps_both_sources_but_boxes_only_changed_words(self):
        original = deepcopy(self.report)
        item, = web_result(self.report, self.docs, {})["items"]
        self.assertEqual(item["id"], "D001")
        self.assertEqual(item["channel"], "ocr")
        self.assertEqual(item["key"], "Q=4.50±0.2")
        for side in ("old", "new"):
            self.assertEqual(item[side]["raw_text"], self.record[side]["raw_text"])
            self.assertAlmostEqual(item[side]["locations"][0]["width"], .04)
            self.assertAlmostEqual(item[side]["context_locations"][0]["width"], .2)
        self.assertEqual(item["text_comparison"]["changed_text"], {"old": ["4.50"], "new": ["5.75"]})
        self.assertEqual(self.report, original)

    def test_missing_or_mismapped_words_never_fall_back_to_full_line_box(self):
        for state in ("unavailable", "invalid_geometry"):
            with self.subTest(state=state):
                record = deepcopy(self.record)
                if state == "unavailable":
                    record["text_comparison"].update(status="unavailable", issues=["CU words missing"])
                else:
                    record["text_comparison"]["new"][0]["page_context"][0]["width"] = 20
                report = dict(self.report, ocr_differences=[record])
                item, = web_result(report, self.docs, {})["items"]
                self.assertEqual(item["text_comparison"]["status"], "unavailable")
                for side in ("old", "new"):
                    self.assertEqual(item[side]["locations"], [])
                    self.assertTrue(item[side]["context_locations"])
                    self.assertTrue(item[side]["location_error"])

    def test_written_report_discloses_semantic_correspondence_and_word_grounding(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "report.md"
            write_report(self.report, target)
            text = target.read_text(encoding="utf-8")
            self.assertIn("LLM语义配对与CU变化词", text)
            self.assertIn("4.50", text)
            self.assertIn("5.75", text)
            self.assertIn("Same printed parameter", text)


if __name__ == "__main__":
    unittest.main()
