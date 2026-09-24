"""Extraction-only Web contracts without Azure or generated domain fields."""

import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import pymupdf

from tests.test_analysis_options import config
from tests.test_web import BASE, pdf_bytes
from cu_diff.web import create_app


class LayoutClient:
    def __init__(self, config):
        self.config = config
        self.events = []

    def ensure_analyzer(self, *, allow_create=True):
        assert not allow_create
        return "synthetic-layout", {}

    def analyze(self, path, cache, identifier, analyzer, *, allow_submit=True):
        with pymupdf.open(path) as pdf:
            text = pdf[0].get_text().strip()
        word = {"content": text, "source": "D(1,1,1,3,0.3)", "confidence": .95}
        return {"status": "Succeeded", "result": {"contents": [{
            "unit": "inch", "pages": [{"pageNumber": 1, "width": 10, "height": 5,
                                       "lines": [copy.deepcopy(word)], "words": [word]}],
        }]}}, {"cache_hit": True, "extraction_profile": "layout"}


class LayoutPipelineTests(unittest.TestCase):
    def test_layout_requires_model_semantic_pairing_before_service_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for key in ("enabled", "text_pairing"):
                value = config()
                value["extraction_profile"] = "layout"
                value["model_comparison"][key] = False
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, "轻量CU"):
                    create_app(value, root / "data", root / "cache", client_factory=LayoutClient)

    def test_layout_wires_ocr_primary_bom_tables_and_profile_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = config()
            value["extraction_profile"] = "layout"
            app = create_app(value, root / "data", root / "cache", client_factory=LayoutClient)
            store = app.extensions["review_store"]
            browser = app.test_client()
            boot = browser.get("/api/bootstrap", base_url=BASE).get_json()
            self.assertEqual(boot["extraction_profile"], "layout")
            headers = {"Origin": BASE, "X-CSRF-Token": boot["csrf_token"]}
            for role in ("old", "new"):
                uploaded = browser.put("/api/documents/" + role, data=pdf_bytes(role),
                                       content_type="application/pdf", base_url=BASE, headers=headers)
            result = {"items": [], "coverage": {}, "warnings": []}
            try:
                with patch("cu_diff.model_compare.compare_with_model", return_value=result), patch(
                        "cu_diff.semantic_text.resolve_text_pairing", side_effect=lambda comparison, *a, **k: comparison
                ) as semantic, patch(
                        "cu_diff.document_tables.compare_document_tables", return_value=result
                ) as tables, patch("cu_diff.graphics.compare_graphics", return_value=result):
                    started = browser.post("/api/compare", base_url=BASE, headers=headers,
                                           json={"revision": uploaded.get_json()["revision"]})
                    self.assertEqual(started.status_code, 202)
                    identifier = started.get_json()["job_id"]
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        job = browser.get("/api/jobs/" + identifier, base_url=BASE).get_json()
                        if job["status"] not in ("queued", "running"):
                            break
                        time.sleep(.01)
                    self.assertEqual(job["status"], "succeeded", job.get("error"))
                    self.assertEqual(job["result"]["extraction_profile"], "layout")
                    self.assertEqual(job["result"]["primary_text_channel"], "ocr")
                    self.assertTrue(job["result"]["items"])
                    self.assertTrue(all(i["channel"] == "ocr" for i in job["result"]["items"]))
                    self.assertTrue(tables.call_args.kwargs["include_bom"])
                    self.assertEqual(semantic.call_args.args[0]["extraction_profile"], "layout")
                    self.assertFalse(any("no Items.valueArray" in w for w in job["result"]["warnings"]))
            finally:
                store.executor.shutdown(wait=True)
            audit = json.loads(next((root / "data").glob("web-session-*/*.json")).read_text(encoding="utf-8"))
            self.assertEqual(audit["extraction_profile"], "layout")
