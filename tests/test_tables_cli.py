"""Offline CLI provenance/report tests using synthetic PDFs and mocked table results."""

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

from cu_diff.cli import tables


class TablesCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        values = {"output": self.root / "results"}
        for role in ("old", "new"):
            path = self.root / f"{role}.pdf"
            with pymupdf.open() as document:
                document.new_page().insert_text((40, 40), f"SYNTHETIC {role}")
                document.save(path)
            response = self.root / f"{role}.response.json"
            response.write_text(json.dumps({"status": "Succeeded", "result": {"contents": [{}]}}))
            metadata = self.root / f"{role}.metadata.json"
            metadata.write_text(json.dumps({"document_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}))
            values.update({role: path, f"{role}_response": response, f"{role}_metadata": metadata})
        self.args = argparse.Namespace(**values)

    def test_hash_mismatch_fails_before_comparison_and_does_not_write_report(self):
        self.args.new_metadata.write_text('{"document_sha256":"wrong"}')
        with patch("cu_diff.document_tables.compare_document_tables") as compare:
            with self.assertRaisesRegex(ValueError, "hash does not match"):
                tables(self.args)
            compare.assert_not_called()
        self.assertFalse(self.args.output.exists())

    def test_failed_response_cannot_become_no_changes(self):
        self.args.old_response.write_text('{"status":"Failed"}')
        with patch("cu_diff.document_tables.compare_document_tables") as compare:
            with self.assertRaisesRegex(ValueError, "did not succeed"):
                tables(self.args)
            compare.assert_not_called()
        self.assertFalse(self.args.output.exists())

    def test_success_retains_grid_versus_text_distinction_in_local_reports(self):
        item = {
            "id": "T001", "channel": "tables", "key": "Synthetic grid",
            "change": "table_grid_changed",
            "old": {"raw_text": "2 grid rows", "locations": []},
            "new": {"raw_text": "3 grid rows", "locations": []},
            "table_comparison": {"status": "complete"},
        }
        result = {"items": [item], "coverage": {"completeness": "not_guaranteed"},
                  "warnings": ["Synthetic evidence only."]}
        before = {role: getattr(self.args, role).read_bytes() for role in ("old", "new")}
        with patch("cu_diff.document_tables.compare_document_tables", return_value=result):
            tables(self.args)
        report = json.loads((self.args.output / "tables.json").read_text(encoding="utf-8"))
        self.assertEqual(report["items"], [item])
        self.assertEqual(report["provenance"]["azure_calls"], 0)
        text = (self.args.output / "tables.zh.md").read_text(encoding="utf-8")
        self.assertIn("不等于记录增删", text)
        self.assertIn("包含表头与空白行", text)
        self.assertIn("Synthetic evidence only.", text)
        for role in before:
            self.assertEqual(getattr(self.args, role).read_bytes(), before[role])


if __name__ == "__main__":
    unittest.main()
