"""Current-view export contracts; synthetic mocked services, no Azure calls."""

from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlparse

import pymupdf

from tests import test_graphics_browser as graphics
from tests import test_model_browser as model

expect = graphics.expect


class PdfExportBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        graphics.GraphicsBrowserTests.setUpClass.__func__(cls)
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((72, 72), "SYNTHETIC EXPORT")
            cls.pdf = pdf.tobytes()

    @classmethod
    def tearDownClass(cls):
        graphics.GraphicsBrowserTests.tearDownClass.__func__(cls)

    open_context = graphics.GraphicsBrowserTests.open_context
    tearDown = graphics.GraphicsBrowserTests.tearDown
    compare = graphics.GraphicsBrowserTests.compare
    select = graphics.GraphicsBrowserTests.select

    def setUp(self):
        self.exports = []
        self.held_exports = []
        self.hold_exports = False
        self.export_error = None
        self.invalid_pdf = False
        self.downloads = []
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        model.ModelBrowserTests.setUp(self)
        self.page.on("download", lambda download: self.downloads.append(download))

    def route(self, route):
        url = urlparse(route.request.url)
        if url.netloc == "graphics-ui.test" and url.path == "/api/export/pdf":
            self.assertEqual(route.request.headers.get("x-csrf-token"), "synthetic-csrf")
            self.exports.append(route.request.post_data_json)
            if self.hold_exports:
                self.held_exports.append(route)
            elif self.export_error:
                route.fulfill(status=409, json={"error": self.export_error})
            else:
                route.fulfill(body=b"NOT PDF" if self.invalid_pdf else self.pdf, content_type="application/pdf")
        else:
            model.ModelBrowserTests.route(self, route)

    def download(self):
        with self.page.expect_download() as pending:
            self.page.locator("#export-button").click()
        download = pending.value
        self.assertTrue(download.suggested_filename.endswith(".pdf"))
        target = Path(self.temp.name) / f"export-{len(self.exports)}.pdf"
        download.save_as(target)
        self.assertEqual(target.read_bytes(), self.pdf)
        expect(self.page.locator("#export-button")).to_be_enabled()
        return self.exports[-1]

    def test_current_filter_boxes_ids_and_all_details_without_changing_selection(self):
        expect(self.page.locator("#export-button")).to_be_disabled()
        self.compare()
        self.page.locator("#channel-filter").select_option("model")
        self.select("M001")
        self.page.locator("#old-zoom").select_option("150")
        before = self.page.locator("#detail-content").text_content()
        geometry = {
            side: self.page.locator(f"#{side}-stage rect").evaluate_all(
                "nodes => nodes.map(n => ({id:n.dataset.id,x:+n.getAttribute('x'),y:+n.getAttribute('y'),width:+n.getAttribute('width'),height:+n.getAttribute('height')}))")
            for side in ("old", "new")
        }
        self.page.get_by_role("tab", name="用量与费用").click()
        payload = self.download()
        self.assertEqual(payload["job_id"], "synthetic-job")
        self.assertEqual(payload["revision"], self.revision)
        self.assertEqual([i["id"] for i in payload["items"]], ["M001", "M002"])
        first = "\n".join(block.get("text", "") for block in payload["items"][0]["blocks"])
        for text in ("3A 125V", "8A 125V", "可能对应同一额定值标注", "CU局部复读说明"):
            self.assertIn(text, first)
        self.assertIn("局部预算已用完", str(payload["items"][1]["blocks"]))
        for side in ("old", "new"):
            pane = payload["panes"][side]
            self.assertEqual(pane["page"], 1)
            self.assertEqual(pane["document_id"], self.documents[side]["id"])
            self.assertEqual([{k: rect[k] for k in ("id", "x", "y", "width", "height")} for rect in pane["rects"]], geometry[side])
            review = next(r for r in pane["rects"] if r["id"] == "M002")
            self.assertEqual(review["kind"], "review")
            self.assertEqual(review["stroke"]["dash"], [5, 3])
            self.assertEqual([label["id"] for label in pane["labels"]], ["M001"])
        self.assertEqual(self.page.locator("#detail-content").text_content(), before)
        expect(self.page.get_by_role("tab", name="用量与费用")).to_have_attribute("aria-selected", "true")
        self.assertEqual(self.compare_requests, 1)

    def test_blank_counterpart_stays_blank_and_other_page_details_are_included(self):
        self.result["items"].append(model.visual_item("M003"))
        self.compare()
        self.page.locator("#channel-filter").select_option("model")
        self.select("M003")
        expect(self.page.locator("#export-button")).to_be_enabled()
        payload = self.download()
        self.assertEqual([p["page"] for p in payload["panes"].values()], [2, 2])
        self.assertEqual([r["id"] for r in payload["panes"]["old"]["rects"]], ["M003"])
        self.assertEqual(payload["panes"]["new"]["rects"], [])
        self.assertEqual([i["id"] for i in payload["items"]], ["M001", "M002", "M003"])
        self.assertIn("不推断部件删除", str(payload["items"][2]["blocks"]))

    def test_optional_filters_and_empty_selection_are_exported_without_reanalysis(self):
        self.compare()
        self.page.locator("#display-options > summary").click()
        self.page.locator("#show-scaling").check()
        payload = self.download()
        self.assertTrue(payload["filters"]["scaling"])
        self.assertIn("G008", [i["id"] for i in payload["items"]])
        self.assertNotIn("G003", [i["id"] for i in payload["items"]])
        table = next(item for item in payload["items"] if item["id"] == "D001")
        self.assertTrue(any(block["kind"] == "table" for block in table["blocks"]))
        self.assertIn("OLD-PART", str(table["blocks"]))
        self.page.locator("#channel-filter").select_option("model")
        self.page.locator("#review-filter").select_option("formatting")
        payload = self.download()
        self.assertEqual(payload["items"], [])
        self.assertTrue(all(p["rects"] == [] and p["labels"] == [] for p in payload["panes"].values()))
        self.assertEqual(self.compare_requests, 1)

    def test_service_error_or_invalid_pdf_does_not_clear_comparison(self):
        self.compare()
        self.select("M001")
        before = self.page.locator("#detail-content").text_content()
        for error, invalid in (("当前结果已变化", False), (None, True)):
            self.export_error, self.invalid_pdf = error, invalid
            self.page.locator("#export-button").click()
            expect(self.page.locator("#error-message")).to_contain_text("PDF导出失败")
            expect(self.page.locator("#export-button")).to_be_enabled()
            self.assertEqual(self.page.locator("#detail-content").text_content(), before)
            self.assertEqual(self.downloads, [])
        self.assertEqual(self.compare_requests, 1)

    def test_export_controls_fit_narrow_and_wide_headers(self):
        self.compare()
        for width in (390, 700, 1280):
            self.page.set_viewport_size({"width": width, "height": 900})
            for name in ("export-button", "compare-button"):
                button = self.page.locator("#"+name)
                expect(button).to_be_visible()
                box = button.bounding_box()
                self.assertGreaterEqual(box["x"], 0)
                self.assertLessEqual(box["x"]+box["width"], width)

    def test_export_disabled_while_preview_is_loading(self):
        self.compare()
        held = []
        self.page.route("**/api/documents/*/pages/2?*", lambda route: held.append(route))
        with self.page.expect_request("**/api/documents/*/pages/2?*"):
            self.page.locator("#old-page").select_option("2")
        expect(self.page.locator("#export-button")).to_be_disabled()
        held.pop().fulfill(body=self.png, content_type="image/png")
        expect(self.page.locator("#export-button")).to_be_enabled()

    def test_replacement_cancels_pending_export_and_rejects_duplicate_clicks(self):
        self.compare()
        self.hold_exports = True
        with self.page.expect_request("**/api/export/pdf"):
            self.page.locator("#export-button").click()
        expect(self.page.locator("#export-button")).to_be_disabled()
        expect(self.page.locator("#compare-button")).to_be_disabled()
        self.page.locator("#export-button").evaluate("node => node.click()")
        self.assertEqual(len(self.exports), 1)
        self.page.locator("#new-upload").set_input_files({
            "name": "replacement.pdf", "mimeType": "application/pdf", "buffer": b"synthetic"})
        expect(self.page.locator("#result-count")).to_have_text("—")
        self.held_exports.pop().fulfill(body=self.pdf, content_type="application/pdf")
        self.page.wait_for_load_state("networkidle")
        self.assertEqual(self.downloads, [])
        expect(self.page.locator("#export-button")).to_be_disabled()


if __name__ == "__main__":
    unittest.main()
