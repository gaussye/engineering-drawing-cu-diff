import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cu_diff.client import CUError, Client
from cu_diff.schema import definition
from cu_diff.report import cell, write_report


def config():
    return {
        "endpoint": "https://synthetic.services.ai.azure.com",
        "completion_model": "gpt-5.4",
        "model_deployments": {"gpt-5.4": "synthetic"},
        "deployment_versions": {"synthetic": "test-v1"},
        "processing_location": "geography",
    }


class ClientTests(unittest.TestCase):
    def test_reject_non_azure(self):
        with self.assertRaises(ValueError):
            Client(dict(config(), endpoint="https://example.com"))

    def test_reject_poll_exfiltration(self):
        with self.assertRaises(CUError):
            Client(config()).request("GET", "https://example.com/job")

    def test_full_extraction_contract(self):
        schema = definition("gpt-5.4")
        for flag in ("enableOcr", "enableLayout", "returnDetails",
                     "estimateFieldSourceAndConfidence"):
            self.assertIs(schema["config"][flag], True)
        raw = schema["fieldSchema"]["fields"]["Items"]["items"]["properties"]["RawText"]
        self.assertEqual(raw["type"], "string")
        self.assertEqual(raw["method"], "extract")

    def test_unsupported_model_stops_before_creation(self):
        client = Client(dict(config(), completion_model="unsupported-model"))
        with patch.object(client, "request", return_value=(
            {"supportedModels": {"completion": ["gpt-5.4"]}}, {}
        )) as request:
            with self.assertRaisesRegex(CUError, "No model substitution"):
                client.ensure_analyzer()
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[0], "GET")

    def test_web_setup_never_creates_missing_analyzer(self):
        client = Client(config())
        with patch.object(client, "request", side_effect=[
            ({"supportedModels": {"completion": ["gpt-5.4"]}}, {}),
            CUError("missing", 404),
        ]) as request:
            with self.assertRaisesRegex(CUError, "Web mode never creates"):
                client.ensure_analyzer(allow_create=False)
            self.assertEqual([call.args[0] for call in request.call_args_list], ["GET", "GET"])

    def test_cache_only_miss_does_not_submit(self):
        client = Client(config())
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "synthetic.pdf"
            pdf.write_bytes(b"synthetic")
            with patch.object(client, "request") as request:
                with self.assertRaisesRegex(CUError, "cache-only"):
                    client.analyze(pdf, Path(tmp) / "cache", "test", {}, allow_submit=False)
                request.assert_not_called()

    def test_cache_avoids_post_and_invalidates_model_version(self):
        client = Client(config())
        response = {"status": "Succeeded", "result": {"contents": [{"markdown": "synthetic"}]},
                    "usage": {"documentPagesStandard": 1}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "synthetic.pdf"
            pdf.write_bytes(b"synthetic fixture")
            with patch.object(client, "request", return_value=(
                {}, {"Operation-Location": client.url("analyzerResults/test")}
            )) as request, patch.object(client, "poll", return_value=response):
                _, first = client.analyze(pdf, root / "cache", "test", {})
                _, second = client.analyze(pdf, root / "cache", "test", {})
                self.assertEqual(request.call_count, 1)
                self.assertFalse(first["cache_hit"])
                self.assertTrue(second["cache_hit"])
                client.config["deployment_versions"] = {"synthetic": "test-v2"}
                _, third = client.analyze(pdf, root / "cache", "test", {})
                self.assertEqual(request.call_count, 2)
                self.assertNotEqual(first["cache_key"], third["cache_key"])

    def test_timeout_resumes_operation_without_rebilling(self):
        client = Client(config())
        response = {"status": "Succeeded", "result": {"contents": [{"markdown": "test"}]}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "synthetic.pdf"
            pdf.write_bytes(b"synthetic")
            with patch.object(client, "request", return_value=(
                {}, {"Operation-Location": client.url("analyzerResults/test")}
            )) as request, patch.object(client, "poll", side_effect=CUError("timeout")):
                with self.assertRaises(CUError):
                    client.analyze(pdf, root / "cache", "test", {})
                self.assertEqual(request.call_count, 1)
            with patch.object(client, "request") as request, patch.object(
                client, "poll", return_value=response
            ):
                client.analyze(pdf, root / "cache", "test", {})
                request.assert_not_called()

    def test_empty_success_is_failure(self):
        client = Client(config())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "synthetic.pdf"
            pdf.write_bytes(b"synthetic")
            with patch.object(client, "request", return_value=(
                {}, {"Operation-Location": client.url("analyzerResults/test")}
            )), patch.object(client, "poll", return_value={"status": "Succeeded"}):
                with self.assertRaises(CUError):
                    client.analyze(pdf, root / "cache", "test", {})

    def test_markdown_preserves_cells(self):
        self.assertEqual(cell("A|B\nC"), "A\\|B<br>C")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "report.md"
            write_report({"differences": [], "coverage": {}, "warnings": ["synthetic"]}, target)
            self.assertIn("synthetic", target.read_text(encoding="utf-8"))

    def test_report_distinguishes_generated_interpretation(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "report.md"
            write_report({"differences": [{
                "old": {"raw_text": "SYNTHETIC"},
                "new": {"raw_text": "SYNTHETIC"},
                "change": "interpretation_only", "review_required": True,
            }]}, target)
            self.assertIn("interpretation_only", target.read_text(encoding="utf-8"))
            self.assertNotIn("| modified |", target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
