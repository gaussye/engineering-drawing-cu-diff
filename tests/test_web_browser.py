"""Opt-in Chromium smoke/regression suite; generated PDFs only, no Azure calls."""

import importlib.util
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest

if os.environ.get("CU_BROWSER_TESTS") != "1":
    raise unittest.SkipTest("Set CU_BROWSER_TESTS=1 to run Chromium tests")
if not all(importlib.util.find_spec(name) for name in ("flask", "playwright", "waitress")):
    raise unittest.SkipTest("Install .[web,test] and Playwright Chromium")

from playwright.sync_api import sync_playwright
from waitress import create_server

from cu_diff.web import create_app
from test_web import FakeClient, pdf_bytes


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        cls.app = create_app({"completion_model": "synthetic"},
                             cls.root / "sessions", cls.root / "cache",
                             port=cls.port, client_factory=FakeClient)
        cls.server = create_server(cls.app, host="127.0.0.1", port=cls.port, threads=4)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)
        cls.old = cls.root / "old.pdf"
        cls.new = cls.root / "new.pdf"
        cls.old.write_bytes(pdf_bytes("SYNTHETIC OLD", pages=2))
        cls.new.write_bytes(pdf_bytes("SYNTHETIC NEW", pages=2))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.close()
        cls.server.task_dispatcher.shutdown()
        cls.thread.join(timeout=3)
        cls.app.extensions["review_store"].executor.shutdown(wait=True)
        cls.temp.cleanup()

    def setUp(self):
        FakeClient.fail, FakeClient.gate = False, None
        self.context = self.browser.new_context(viewport={"width": 1600, "height": 1000},
                                                device_scale_factor=2)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(f"http://127.0.0.1:{self.port}")
        self.page.wait_for_load_state("networkidle")

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])

    def upload_pair(self):
        self.page.locator("#old-upload").set_input_files(self.old)
        self.page.wait_for_function(
            "() => document.querySelector('#old-stage img')?.naturalWidth > 0")
        self.page.locator("#new-upload").set_input_files(self.new)
        self.page.wait_for_function(
            "() => document.querySelector('#new-stage img')?.naturalWidth > 0")
        self.page.wait_for_function("() => !document.querySelector('#compare-button').disabled")

    def wait_result(self, review_filter=None):
        self.page.locator("#compare-button").click()
        if review_filter:
            self.page.wait_for_function(
                "() => document.querySelector('#job-status').textContent.includes('对比完成')",
                timeout=15000)
            self.assertEqual(self.page.locator("#results-list .result-item").count(), 0)
            self.assertEqual(self.page.locator("rect.evidence-box").count(), 0)
            self.page.locator("#review-filter").select_option(review_filter)
        self.page.wait_for_selector("#results-list .result-item", timeout=15000)

    def alignment(self, role):
        metrics = self.page.evaluate("""role => {
          const image = document.querySelector(`#${role}-stage img`).getBoundingClientRect();
          const box = document.querySelector(`#${role}-stage rect.evidence-box`).getBoundingClientRect();
          return {dx: box.x-image.x,dy:box.y-image.y,w:box.width,h:box.height,iw:image.width,ih:image.height};
        }""", role)
        self.assertAlmostEqual(metrics["dx"], metrics["iw"] * 0.1, delta=2)
        self.assertAlmostEqual(metrics["dy"], metrics["ih"] * 0.2, delta=2)
        self.assertAlmostEqual(metrics["w"], metrics["iw"] * 0.1, delta=2)
        self.assertAlmostEqual(metrics["h"], metrics["ih"] * 0.1, delta=2)

    def test_upload_preview_compare_multipage_zoom_and_replacement(self):
        self.assertTrue(self.page.locator("#compare-button").is_disabled())
        self.upload_pair()
        self.assertEqual(self.page.locator("rect.evidence-box").count(), 0)
        self.wait_result()
        self.page.locator("#results-list .result-item").first.click()
        self.page.wait_for_load_state("networkidle")
        self.assertEqual(self.page.locator("#results-list .result-button").first.get_attribute("aria-pressed"), "true")
        for role in ("old", "new"):
            self.page.wait_for_function(
                "role => document.querySelector(`#${role}-page`).value === '2'", arg=role)
            self.page.wait_for_selector(f"#{role}-stage rect.evidence-box")
            self.assertGreater(self.page.locator(f"#{role}-stage rect.evidence-box.selected").count(), 0)
            self.page.locator(f"#{role}-fit").click()
            self.alignment(role)
            fit_width = self.page.locator(f"#{role}-stage img").bounding_box()["width"]
            self.page.locator(f"#{role}-zoom").select_option("150")
            self.alignment(role)
            zoom_width = self.page.locator(f"#{role}-stage img").bounding_box()["width"]
            self.assertGreater(zoom_width, fit_width * 1.1)
            self.page.locator(f"#{role}-fit").click()
            self.alignment(role)
            self.assertAlmostEqual(self.page.locator(f"#{role}-stage img").bounding_box()["width"],
                                   fit_width, delta=2)
        self.page.locator("#old-upload").set_input_files(self.new)
        self.page.wait_for_function("() => document.querySelectorAll('rect.evidence-box').length === 0")
        self.assertEqual(self.page.locator("#results-list .result-item").count(), 0)

    def test_failed_job_is_visible(self):
        self.upload_pair()
        FakeClient.fail = True
        self.page.locator("#compare-button").click()
        self.page.wait_for_function(
            "() => document.body.textContent.includes('Synthetic CU failure')", timeout=15000)
        self.assertEqual(self.page.locator("rect.evidence-box").count(), 0)

    def test_identical_uploads_are_visible_and_comparison_is_disabled(self):
        self.page.locator("#old-upload").set_input_files(self.old)
        self.page.wait_for_function(
            "() => document.querySelector('#old-stage img')?.naturalWidth > 0")
        self.page.locator("#new-upload").set_input_files(self.old)
        self.page.wait_for_function(
            "() => document.querySelector('#new-stage img')?.naturalWidth > 0")
        self.assertTrue(self.page.locator("#compare-button").is_disabled())
        self.assertIn("完全相同", self.page.locator("#file-identity-status").inner_text())
        self.assertIn("请更换其中一份", self.page.locator("#job-status").inner_text())
        self.assertEqual(self.page.locator("#old-identity").get_attribute("title"),
                         self.page.locator("#new-identity").get_attribute("title"))
        self.assertEqual(self.page.locator("rect.evidence-box").count(), 0)

    def test_wrong_source_results_are_rejected(self):
        self.upload_pair()
        for field in ("id", "sha256"):
            with self.subTest(field=field):
                documents = self.page.evaluate(
                    "async () => (await (await fetch('/api/bootstrap')).json()).documents")
                documents["new"][field] = "wrong-source"
                result = {"items": [], "documents": documents}
                self.page.route("**/api/jobs/*", lambda route: route.fulfill(
                    json={"status": "succeeded", "result": result}))
                self.page.locator("#compare-button").click()
                self.page.wait_for_function(
                    "() => document.querySelector('#error-message').textContent.includes('结果文件与当前上传文件不一致')")
                self.assertEqual(self.page.locator("#results-list .result-item").count(), 0)
                self.assertEqual(self.page.locator("rect.evidence-box").count(), 0)
                self.page.unroute("**/api/jobs/*")

    def test_replacement_during_analysis_discards_late_results(self):
        self.upload_pair()
        FakeClient.gate = threading.Event()
        self.page.locator("#compare-button").click()
        self.page.wait_for_function("() => document.querySelector('#compare-button').disabled")
        self.page.locator("#old-upload").set_input_files(self.new)
        self.page.wait_for_function(
            "() => document.querySelector('#old-stage img')?.naturalWidth > 0")
        FakeClient.gate.set()
        self.page.wait_for_timeout(500)
        self.assertEqual(self.page.locator("rect.evidence-box").count(), 0)
        self.assertEqual(self.page.locator("#results-list .result-item").count(), 0)

    def test_single_side_and_missing_sources_never_invent_frames(self):
        self.upload_pair()
        item = {
            "id": "D001", "channel": "schema", "region": "synthetic", "key": "single side",
            "change": "unpaired_new", "review_required": True, "review_reasons": [],
            "match": {"method": "unpaired", "score": 0, "certainty": "unpaired"},
            "old": None, "new": {
                "raw_text": "SYNTHETIC ONLY", "confidence": None, "source": None,
                "locations": [], "location_error": "CU未提供来源坐标，无法定位",
            },
        }
        documents = self.page.evaluate("async () => (await (await fetch('/api/bootstrap')).json()).documents")
        result = {"items": [item], "coverage": {}, "warnings": [], "documents": documents,
                  "metadata": {"old": {"cache_hit": True}, "new": {"cache_hit": True}}}
        self.page.route("**/api/jobs/*", lambda route: route.fulfill(
            json={"status": "succeeded", "phase": "synthetic", "result": result}))
        self.wait_result(review_filter="unpaired")
        self.page.locator("#results-list .result-item").first.click()
        self.assertEqual(self.page.locator("rect.evidence-box").count(), 0)
        self.assertIn("无法定位", self.page.locator("body").inner_text())

    def test_one_sided_evidence_draws_only_one_side(self):
        self.upload_pair()
        documents = self.page.evaluate("async () => (await (await fetch('/api/bootstrap')).json()).documents")
        item = {
            "id": "D001", "channel": "schema", "region": "synthetic", "key": "new evidence",
            "change": "unpaired_new", "review_required": True, "review_reasons": [],
            "match": {"method": "unpaired", "score": 0, "certainty": "unpaired"},
            "old": None, "new": {
                "raw_text": "SYNTHETIC ONLY", "confidence": 0.8,
                "source": "D(1,1,1,2,1,2,1.5,1,1.5)",
                "locations": [{"page": 1, "x": 0.1, "y": 0.2, "width": 0.1, "height": 0.1,
                               "polygon": [[0.1, 0.2], [0.2, 0.2], [0.2, 0.3], [0.1, 0.3]]}],
                "location_error": None,
            },
        }
        result = {"items": [item], "coverage": {}, "warnings": [], "documents": documents,
                  "metadata": {"old": {"cache_hit": True}, "new": {"cache_hit": True}}}
        self.page.route("**/api/jobs/*", lambda route: route.fulfill(
            json={"status": "succeeded", "phase": "synthetic", "result": result}))
        self.wait_result(review_filter="unpaired")
        self.page.locator("#results-list .result-item").first.click()
        self.page.wait_for_selector("#new-stage rect.evidence-box")
        self.assertEqual(self.page.locator("#old-stage rect.evidence-box").count(), 0)
        self.assertEqual(self.page.locator("#new-stage rect.evidence-box").count(), 1)
        self.assertEqual(self.page.locator("#new-stage rect.evidence-box.review-evidence").count(), 1)
        self.assertNotEqual(self.page.locator("#new-stage rect.evidence-box").evaluate(
            "node => getComputedStyle(node).strokeDasharray"), "none")
        self.assertIn("待核", self.page.locator("#new-stage .evidence-label").text_content())
        self.assertIn("不是已确认差异", self.page.locator("#detail-content").inner_text())
        self.assertIn("不代表本侧图纸没有", self.page.locator("#old-evidence-note").inner_text())
        self.alignment("new")


if __name__ == "__main__":
    unittest.main()
