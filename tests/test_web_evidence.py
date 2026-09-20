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


if __name__ == "__main__":
    unittest.main()
