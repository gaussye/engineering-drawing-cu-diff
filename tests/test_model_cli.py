import json
from unittest.mock import Mock, patch

from cu_diff.cli import model_compare
from cu_diff.client import CUError
from test_tables_cli import TablesCliTests


class ModelCliTests(TablesCliTests):
    def setUp(self):
        super().setUp()
        self.args.config = self.root / "config.json"
        self.args.config.write_text(json.dumps({"model_comparison": {"enabled": True}}))
        self.args.cache_dir = self.root / "cache"
        self.args.allow_azure_upload = False
        self.client = Mock()
        self.client.ensure_analyzer.return_value = ("synthetic", {})
        self.client.events = [{"method": "GET", "status": 200}]

    def test_model_hash_guard_and_disabled_config_prevent_requests(self):
        self.args.new_metadata.write_text('{"document_sha256":"wrong"}')
        with patch("cu_diff.cli.Client") as client:
            with self.assertRaisesRegex(ValueError, "hash"):
                model_compare(self.args)
            client.assert_not_called()
        self.args.config.write_text("{}")
        with self.assertRaisesRegex(ValueError, "Enable"):
            model_compare(self.args)

    def test_model_outputs_and_cache_only_permission(self):
        result = {"items": [], "coverage": {"enabled": True, "unprocessed": 2},
                  "warnings": ["Synthetic incomplete coverage."]}
        with patch("cu_diff.cli.Client", return_value=self.client), patch(
                "cu_diff.model_compare.compare_with_model", return_value=result) as compare:
            model_compare(self.args)
        self.assertFalse(compare.call_args.kwargs["allow_submit"])
        self.client.ensure_analyzer.assert_called_once_with(allow_create=False)
        data = json.loads((self.args.output / "model-comparison.json").read_text(encoding="utf-8"))
        self.assertEqual(data, result)
        report = (self.args.output / "model-comparison.zh.md").read_text(encoding="utf-8")
        self.assertIn("不等于已证明无变化", report)
        self.assertIn("Synthetic incomplete coverage.", report)

    def test_model_failure_preserves_audit_not_success_report(self):
        with patch("cu_diff.cli.Client", return_value=self.client), patch(
                "cu_diff.model_compare.compare_with_model", side_effect=CUError("Synthetic failure")):
            with self.assertRaises(CUError):
                model_compare(self.args)
        self.assertFalse((self.args.output / "model-comparison.json").exists())
        self.assertEqual(json.loads((self.args.output / "model-api-events.json").read_text())["events"],
                         self.client.events)

    def test_semantic_pairing_writes_real_text_channel_outputs_with_cache_only_permission(self):
        self.args.config.write_text(json.dumps({"model_comparison": {"enabled": True, "text_pairing": True}}))
        result = {"items": [], "coverage": {"enabled": True}, "warnings": []}
        text_result = {"differences": [], "ocr_differences": [], "unchanged": [], "ocr_unchanged": [],
                       "coverage": {"semantic_text": {"enabled": True, "status": "complete"}},
                       "warnings": []}
        with patch("cu_diff.cli.Client", return_value=self.client), patch(
                "cu_diff.model_compare.compare_with_model", return_value=result), patch(
                "cu_diff.semantic_text.resolve_text_pairing", return_value=text_result) as pairing:
            model_compare(self.args)
        self.assertFalse(pairing.call_args.kwargs["allow_submit"])
        self.assertEqual(pairing.call_args.args[0]["coverage"]["changed_text_pairing"], "semantic_pending")
        self.assertEqual(pairing.call_args.args[2], {"old": self.args.old, "new": self.args.new})
        self.assertEqual(json.loads((self.args.output / "text-comparison.json").read_text(encoding="utf-8")), text_result)
        self.assertTrue((self.args.output / "text-comparison.zh.md").exists())
        saved = json.loads((self.args.output / "model-comparison.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["coverage"]["semantic_text"], text_result["coverage"]["semantic_text"])
