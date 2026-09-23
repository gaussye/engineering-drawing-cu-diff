import copy
import hashlib
import unittest
from unittest.mock import patch

import pymupdf

from tests import test_web as web


class PdfExportWebTests(unittest.TestCase):
    upload = web.WebTests.upload
    compare = web.WebTests.compare
    wait_job = web.WebTests.wait_job
    tearDown = web.WebTests.tearDown

    def setUp(self):
        web.WebTests.setUp(self)
        self.old = self.upload("old", web.pdf_bytes("SYNTHETIC OLD")).get_json()["document"]
        uploaded = self.upload("new", web.pdf_bytes("SYNTHETIC NEW")).get_json()
        self.new, self.revision = uploaded["document"], uploaded["revision"]
        self.identifier = self.compare(self.revision).get_json()["job_id"]
        self.job = self.wait_job(self.identifier)
        self.assertEqual(self.job["status"], "succeeded")
        self.snapshot = {
            "revision": self.revision, "job_id": self.identifier,
            "filters": {"channel": "全部通道", "review": "全部证据", "interpretation": False,
                        "translation": False, "scaling": False},
            "panes": {side: {"document_id": document["id"], "page": 1, "width": 800, "height": 400,
                             "rects": [], "labels": []}
                      for side, document in (("old", self.old), ("new", self.new))},
            "items": [{"id": self.job["result"]["items"][0]["id"], "title": "合成差异",
                       "meta": "需人工复核", "blocks": [
                           {"kind": "heading", "text": "旧版 / 新版"},
                           {"kind": "paragraph", "text": "旧版参数甲；新版参数乙。仅为候选。"}]}],
        }

    def export(self, snapshot=None, headers=None):
        return self.client.post("/api/export/pdf", json=self.snapshot if snapshot is None else snapshot,
                                base_url=web.BASE, headers=self.headers if headers is None else headers)

    def test_download_pdf_preserves_sources_and_makes_no_analysis_calls(self):
        store = self.app.extensions["review_store"]
        sources = list(self.root.rglob("*.pdf"))
        hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
        before = copy.deepcopy(store.jobs[self.identifier])
        with patch("cu_diff.client.Client.request", side_effect=AssertionError("Export must stay local")):
            response = self.export()
        self.assertEqual(response.status_code, 200, response.get_json(silent=True))
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        with pymupdf.open(stream=response.data, filetype="pdf") as pdf:
            self.assertGreaterEqual(len(pdf), 2)
            text = "".join(page.get_text() for page in pdf)
            self.assertIn("合成差异", text)
            self.assertIn("新版参数乙", text)
            self.assertIn(self.snapshot["items"][0]["id"], text)
        self.assertEqual(hashes, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
        self.assertEqual(store.jobs[self.identifier], before)

    def test_csrf_and_cross_session_are_rejected(self):
        self.assertEqual(self.export(headers={**self.headers, "X-CSRF-Token": "wrong"}).status_code, 403)
        stranger = self.app.test_client()
        boot = stranger.get("/api/bootstrap", base_url=web.BASE).get_json()
        response = stranger.post("/api/export/pdf", json=self.snapshot, base_url=web.BASE,
                                 headers={"Origin": web.BASE, "X-CSRF-Token": boot["csrf_token"]})
        self.assertEqual(response.status_code, 404)

    def test_stale_revision_and_replaced_document_are_rejected(self):
        stale = copy.deepcopy(self.snapshot)
        stale["revision"] += 1
        self.assertEqual(self.export(stale).status_code, 409)
        self.upload("new", web.pdf_bytes("REPLACEMENT"))
        self.assertEqual(self.export().status_code, 409)

    def test_running_or_failed_result_cannot_be_exported(self):
        job = self.app.extensions["review_store"].jobs[self.identifier]
        for status in ("running", "failed", "stale"):
            job["status"] = status
            self.assertEqual(self.export().status_code, 409)
        job["status"] = "succeeded"

    def test_result_document_identity_is_checked_independently(self):
        self.app.extensions["review_store"].jobs[self.identifier]["result"]["documents"]["old"]["sha256"] = "wrong"
        self.assertEqual(self.export().status_code, 409)

    def test_unknown_item_and_document_are_not_exported(self):
        for mutate in (lambda s: s["items"][0].update(id="UNKNOWN"),
                       lambda s: s["panes"]["old"].update(document_id="unknown"),
                       lambda s: s["panes"]["new"].update(page=99)):
            value = copy.deepcopy(self.snapshot)
            mutate(value)
            self.assertEqual(self.export(value).status_code, 400)

    def test_bad_payload_and_explicit_renderer_failure_return_json_errors(self):
        for payload in (None, [], {"revision": True, "job_id": self.identifier}):
            response = self.client.post("/api/export/pdf", json=payload, base_url=web.BASE, headers=self.headers)
            self.assertIn(response.status_code, (400, 415))
        from cu_diff.pdf_export import PdfExportError
        with patch("cu_diff.pdf_export.render_comparison_pdf", side_effect=PdfExportError("合成导出限制")):
            response = self.export()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "合成导出限制")

    def test_empty_filter_exports_without_claiming_complete_no_change(self):
        self.snapshot["items"] = []
        response = self.export()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b"%PDF-"))


if __name__ == "__main__":
    unittest.main()
