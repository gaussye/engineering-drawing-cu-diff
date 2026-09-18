"""Offline graphical-review UI regressions: mocked API, synthetic pixels, no backend imports.

Run with CU_BROWSER_TESTS=1 and Playwright Chromium installed. All requests are
fulfilled in-process; no server, Azure credentials, or customer PDFs are used.
"""

import copy
import importlib.util
import os
from pathlib import Path
import struct
import unittest
from urllib.parse import urlparse
import zlib

if os.environ.get("CU_BROWSER_TESTS") != "1":
    raise unittest.SkipTest("Set CU_BROWSER_TESTS=1 to run Chromium tests")
if not importlib.util.find_spec("playwright"):
    raise unittest.SkipTest("Install the test extra and Playwright Chromium")

from playwright.sync_api import expect, sync_playwright


STATIC = Path(__file__).resolve().parents[1] / "cu_diff" / "static"
ORIGIN = "http://graphics-ui.test"


def synthetic_png():
    """Produce a 1000x700 drawing entirely in memory using only the standard library."""
    width, height = 1000, 700
    rows = []
    for y in range(height):
        row = bytearray(b"\xff\xff\xff" * width)
        if 140 <= y < 210:
            row[300:600] = b"\x24\x24\x24" * 100
        rows.append(b"\x00" + row)

    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows)))
            + chunk(b"IEND", b""))


def location(page=1, x=0.1, y=0.2, width=0.1, height=0.1, **extra):
    return {
        "page": page, "x": x, "y": y, "width": width, "height": height,
        "polygon": [[x, y], [x + width, y], [x + width, y + height], [x, y + height]],
        **extra,
    }


def source(locations=None, context=None, raw_text="本地渲染：存在局部外观残差"):
    return {
        "raw_text": raw_text, "detail": "依据本地渲染像素，不是 OCR 识别原文。",
        "confidence": None, "source": {"kind": "local-render", "page": 1, "dpi": 144},
        "locations": [location()] if locations is None else locations,
        "context_locations": context or [], "location_error": None,
    }


def graphical(identifier, change="visual_modified", old=None, new=None):
    return {
        "id": identifier, "channel": "graphics", "region": "合成图形区域", "key": identifier,
        "change": change, "review_required": True,
        "review_reasons": ["本地像素差异不代表真实材质或尺寸变化"],
        "match": {"method": "local-registration", "score": 0.91,
                  "certainty": "uncertain" if change == "visual_uncertain" else "high"},
        "old": source() if old is None else old, "new": source() if new is None else new,
        "graphics": {
            "method": "synthetic-residual", "classification": "外观变化候选",
            "dpi": 144, "translation_pt": {"dx": 12, "dy": -6.25},
            "alignment": {"method": "phase", "accepted": change != "visual_uncertain",
                          "response": 0.91, "inlier_count": 0},
            "changed_pixels": {"old": 24, "new": 36},
            "residual_fraction": {"old": 0.0125, "new": 0.025},
            "region_source": "cu_figure",
            "limitations": ["渲染误差仍需人工复核", "<img src=x onerror=alert(1)>"],
            # Region metadata must never become inferred residual rectangles.
            "original_region": location(2, 0.05, 0.05, 0.9, 0.9),
            "new_region": location(2, 0.05, 0.05, 0.9, 0.9),
        },
    }


def document(side):
    return {
        "id": f"synthetic-{side}", "name": f"合成-{side}.pdf",
        "sha256": ("a" if side == "old" else "b") * 64, "page_count": 2,
        "pages": [{"number": number, "width_pt": 750, "height_pt": 525,
                   "rotation": 0, "analysis_rotation": 0} for number in (1, 2)],
    }


def result_fixture(documents):
    schema = {
        "id": "D001", "channel": "schema", "region": "BOM", "key": "合成部件",
        "change": "modified", "review_required": True, "review_reasons": [],
        "match": {"method": "cell", "score": 0.95, "certainty": "high"},
        "old": source([location(field="part_number", label="部件号")], raw_text="OLD-PART / SAME"),
        "new": source([location(x=0.15, field="part_number", label="部件号")], raw_text="NEW-PART / SAME"),
        "cell_comparison": {
            "status": "complete", "fields": [
                {"key": "part_number", "label": "部件号", "change": "modified",
                 "old": {"raw_text": "OLD-PART"}, "new": {"raw_text": "NEW-PART"}},
                {"key": "description", "label": "描述", "change": "unchanged",
                 "old": {"raw_text": "SAME"}, "new": {"raw_text": "SAME"}},
            ],
        },
    }
    ocr = {**copy.deepcopy(schema), "id": "D002", "channel": "ocr"}
    ocr.pop("cell_comparison")
    formatting = {**copy.deepcopy(ocr), "id": "D003", "channel": "schema", "change": "formatting_only"}
    formatting["old"]["raw_text"] = "2026. 09. 18"
    formatting["new"]["raw_text"] = "2026.09.18"
    interpretation = {**copy.deepcopy(ocr), "id": "D004", "channel": "schema", "change": "interpretation_only"}
    modified = graphical("G001", old=source([location(x=0.25, y=0.35, width=0.08, height=0.12)]),
                         new=source([location(x=0.4, y=0.2, width=0.09, height=0.14)]))
    annotation = graphical("G002", "visual_annotation",
                           old=source([location(2), location(1, 0.2, 0.5)]),
                           new=source([location(2, 0.6, 0.55, 0.08, 0.1)]))
    annotation["graphics"]["classification"] = "文字/引线布局候选"
    moved = graphical("G003", "visual_moved",
                      old=source([location(x=0.65, y=0.5)]),
                      new=source([location(x=0.7, y=0.6)]))
    moved["graphics"]["classification"] = "刚性位移候选"
    uncertain = graphical("G004", "visual_uncertain",
                          old=source([], [location(2, 0.65, 0.7, 0.2, 0.2)]),
                          new=source([], [location(2, 0.6, 0.7, 0.2, 0.2)]))
    uncertain["graphics"]["classification"] = "图形对应不确定"
    uncertain["graphics"]["region_source"] = "page_fallback"
    unpaired_old = graphical("G005", "unpaired_old")
    unpaired_old["new"] = None
    unpaired_new = graphical("G006", "unpaired_new")
    unpaired_new["old"] = None
    context_only = graphical("G007", old=source([], [location(2, 0.7, 0.65, 0.2, 0.25)]),
                             new=source([location(2, 0.35, 0.5, 0.08, 0.1)]))
    context_only["graphics"]["changed_pixels"]["old"] = 0
    return {
        "items": [schema, ocr, formatting, interpretation, modified, annotation,
                  moved, uncertain, unpaired_old, unpaired_new, context_only],
        "documents": copy.deepcopy(documents), "coverage": {"schema": 1}, "warnings": [],
        "metadata": {"old": {"cache_hit": True, "usage": {"tokens": 10}},
                     "new": {"cache_hit": True, "usage": {"tokens": 12}}},
        "graphics_coverage": {
            "page_counts": {"old": 2, "new": 2}, "region_counts": {"old": 6, "new": 6},
            "paired": 5, "unpaired_old": 1, "unpaired_new": 1,
            "warnings": ["合成页面回退，不代表对应可靠"],
            "limits": {"max_pages": 20, "dpi": 144, "max_pixels": 4000000},
        },
    }


class GraphicsBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception:
            cls.playwright.stop()
            raise
        cls.png = synthetic_png()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.documents = {side: document(side) for side in ("old", "new")}
        self.result = result_fixture(self.documents)
        self.revision = 2
        self.errors = []
        self.unexpected_requests = []
        self.held_uploads = []
        self.held_jobs = []
        self.hold_uploads = False
        self.hold_jobs = False
        self.graphics_enabled = True
        self.open_context()

    def open_context(self, dpr=2):
        self.context = self.browser.new_context(
            viewport={"width": 1600, "height": 1050}, device_scale_factor=dpr)
        self.context.route("**/*", self.route)
        self.page = self.context.new_page()
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(ORIGIN)
        self.page.wait_for_load_state("networkidle")
        expect(self.page.locator("#compare-button")).to_be_enabled()

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.unexpected_requests, [])

    def route(self, route):
        request = route.request
        url = urlparse(request.url)
        path = url.path
        if url.netloc != "graphics-ui.test":
            self.unexpected_requests.append(request.url)
            route.abort()
        elif path in ("/", "/static/app.js", "/static/app.css"):
            name, content_type = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
                "/static/app.css": ("app.css", "text/css; charset=utf-8"),
            }[path]
            route.fulfill(body=(STATIC / name).read_bytes(), content_type=content_type)
        elif path == "/api/bootstrap":
            payload = {
                "csrf_token": "synthetic-csrf", "revision": self.revision,
                "azure_enabled": False, "model": "synthetic-local",
                "documents": self.documents, "storage_notice": "合成测试会话",
                "limits": {"max_bytes": 20971520, "max_pages": 20, "session_ttl_hours": 24},
            }
            if self.graphics_enabled is not None:
                payload["graphics_enabled"] = self.graphics_enabled
            route.fulfill(json=payload)
        elif path.startswith("/api/documents/") and "/pages/" in path:
            route.fulfill(body=self.png, content_type="image/png")
        elif path == "/api/compare":
            self.assertEqual(request.headers.get("x-csrf-token"), "synthetic-csrf")
            self.assertEqual(request.post_data_json, {"revision": self.revision})
            route.fulfill(json={"job_id": "synthetic-job", "status": "queued", "revision": self.revision})
        elif path == "/api/jobs/synthetic-job":
            if self.hold_jobs:
                self.held_jobs.append(route)
            else:
                route.fulfill(json={"status": "succeeded", "result": self.result})
        elif path in ("/api/documents/old", "/api/documents/new") and request.method == "PUT":
            self.assertEqual(request.headers.get("x-csrf-token"), "synthetic-csrf")
            side = path.rsplit("/", 1)[1]
            self.revision += 1
            self.documents[side] = {**document(side), "id": f"replacement-{self.revision}",
                                    "sha256": "c" * 64, "name": "替换合成图.pdf"}
            payload = {"document": self.documents[side], "revision": self.revision}
            if self.hold_uploads:
                self.held_uploads.append((route, payload))
            else:
                route.fulfill(json=payload)
        else:
            self.unexpected_requests.append(request.url)
            route.fulfill(status=404, body="Unexpected synthetic request")

    def compare(self):
        self.page.locator("#compare-button").click()
        expect(self.page.locator("#job-status")).to_contain_text("对比完成")
        self.page.wait_for_load_state("networkidle")

    def visible_ids(self):
        return self.page.locator("#results-list .result-item").evaluate_all(
            "nodes => nodes.map(node => node.dataset.id)")

    def select(self, identifier):
        self.page.locator(f'.result-item[data-id="{identifier}"] button').click()
        self.page.wait_for_load_state("networkidle")

    def assert_geometry(self, side, identifier, expected):
        rect = self.page.locator(f'#{side}-stage rect.evidence-box[data-id="{identifier}"]')
        expect(rect).to_have_count(1)
        metrics = rect.evaluate("""node => {
          const r = node.getBoundingClientRect();
          const image = node.closest('.drawing-stage').querySelector('img').getBoundingClientRect();
          const svg = node.ownerSVGElement.getBoundingClientRect();
          return {x:r.x-image.x,y:r.y-image.y,width:r.width,height:r.height,
                  iw:image.width,ih:image.height,sw:svg.width,sh:svg.height};
        }""")
        for axis, size in (("x", "iw"), ("width", "iw"), ("y", "ih"), ("height", "ih")):
            self.assertAlmostEqual(metrics[axis], metrics[size] * expected[axis], delta=1)
        self.assertAlmostEqual(metrics["sw"], metrics["iw"], delta=0.1)
        self.assertAlmostEqual(metrics["sh"], metrics["ih"], delta=0.1)
        self.assertEqual(rect.get_attribute("vector-effect"), "non-scaling-stroke")

    def test_primary_default_and_graphics_review_filters(self):
        expect(self.page.locator("#channel-filter")).to_have_value("primary")
        expect(self.page.locator('#channel-filter option[value="graphics"]')).to_be_enabled()
        expect(self.page.locator("#graphics-status")).to_contain_text("本地图形检测已启用")
        expect(self.page.locator("#review-filter")).to_have_value("paired")
        expect(self.page.locator("rect.evidence-box")).to_have_count(0)
        self.compare()
        self.assertEqual(set(self.visible_ids()), {"D001", "G001", "G002", "G003", "G007"})
        self.page.locator("#channel-filter").select_option("graphics")
        self.assertEqual(set(self.visible_ids()), {"G001", "G002", "G003", "G007"})
        self.page.locator("#review-filter").select_option("review")
        self.assertEqual(set(self.visible_ids()), {f"G{i:03}" for i in range(1, 8)})
        self.page.locator("#review-filter").select_option("uncertain")
        self.assertEqual(self.visible_ids(), ["G004"])
        self.select("G004")
        expect(self.page.locator(".graphics-detail")).to_contain_text("配准未接受 / 不可靠")
        expect(self.page.locator(".graphics-detail")).to_contain_text("无法可靠建立图形对应关系")
        expect(self.page.locator("rect.evidence-box")).to_have_count(0)
        self.page.locator("#review-filter").select_option("all")
        self.assertIn("G004", self.visible_ids())
        self.page.locator("#review-filter").select_option("unpaired")
        self.assertEqual(set(self.visible_ids()), {"G005", "G006"})
        self.select("G005")
        expect(self.page.locator("#new-evidence-note")).to_contain_text("未配对到证据")
        expect(self.page.locator('#new-stage rect[data-id="G005"]')).to_have_count(0)
        expect(self.page.locator('#old-stage rect[data-id="G005"]')).to_have_class("evidence-box review-evidence selected")

    def test_residual_geometry_tracks_fit_zoom_resize_and_dpr(self):
        for dpr in (2, 1):
            with self.subTest(dpr=dpr):
                if dpr == 1:
                    self.context.close()
                    self.open_context(dpr=1)
                self.compare()
                self.select("G001")
                item = next(entry for entry in self.result["items"] if entry["id"] == "G001")
                for side in ("old", "new"):
                    for zoom in ("fit", "150", "200", "75", "fit"):
                        self.page.locator(f"#{side}-zoom").select_option(zoom)
                        self.assert_geometry(side, "G001", item[side]["locations"][0])
                self.page.set_viewport_size({"width": 1280, "height": 900})
                self.page.wait_for_function("""() => {
                  const pane = document.querySelector('#old-viewport');
                  const style = getComputedStyle(pane);
                  return Math.abs(document.querySelector('#old-stage img').getBoundingClientRect().width
                    - pane.clientWidth + parseFloat(style.paddingLeft) + parseFloat(style.paddingRight)) < 1;
                }""")
                for side in ("old", "new"):
                    self.assert_geometry(side, "G001", item[side]["locations"][0])

    def test_movement_has_amber_dashes_and_is_not_content_modification(self):
        self.compare()
        self.select("G003")
        expect(self.page.locator("#detail-meta")).to_contain_text("位移候选（非内容变更）")
        expect(self.page.locator(".graphics-detail")).to_contain_text("不计为内容修改")
        expect(self.page.locator(".graphics-detail")).to_contain_text("Δx 12 pt / Δy -6.25 pt")
        expect(self.page.locator("#job-status")).to_contain_text("仅位移，非内容修改")
        for side in ("old", "new"):
            moved = self.page.locator(f'#{side}-stage rect[data-id="G003"]')
            changed = self.page.locator(f'#{side}-stage rect[data-id="G001"]')
            self.assertIn("movement-evidence", moved.get_attribute("class"))
            self.assertNotIn("review-evidence", changed.get_attribute("class"))
            colors = moved.evaluate("""node => {
              const swatch = document.createElement('span');
              swatch.style.color = 'var(--cp-warning)'; document.body.append(swatch);
              const expected = getComputedStyle(swatch).color; swatch.remove();
              const style = getComputedStyle(node);
              return {stroke:style.stroke, dash:style.strokeDasharray, expected};
            }""")
            self.assertEqual(colors["stroke"], colors["expected"])
            self.assertNotEqual(colors["dash"], "none")
            expect(self.page.locator(f"#{side}-stage .evidence-label")).to_have_text("G003 位移")

    def test_context_navigates_without_inventing_residual_boxes(self):
        self.compare()
        self.page.locator("#old-zoom").select_option("150")
        self.select("G007")
        for side in ("old", "new"):
            expect(self.page.locator(f"#{side}-page")).to_have_value("2")
        expect(self.page.locator('#old-stage rect[data-id="G007"]')).to_have_count(0)
        expect(self.page.locator('#new-stage rect[data-id="G007"]')).to_have_count(1)
        expect(self.page.locator("#old-evidence-note")).to_contain_text("仅导航，不是变化证据，不绘框")
        self.assertGreater(self.page.locator("#old-viewport").evaluate("node => node.scrollTop"), 0)
        self.page.locator("#old-zoom").select_option("200")
        expect(self.page.locator('#old-stage rect[data-id="G007"]')).to_have_count(0)
        self.page.locator("#old-page").select_option("1")
        self.page.wait_for_load_state("networkidle")
        expect(self.page.locator('#old-stage rect[data-id="G007"]')).to_have_count(0)
        expect(self.page.locator("#old-evidence-note")).to_contain_text("第 2 页")
        self.page.locator("#old-fit").click()
        expect(self.page.locator('#old-stage rect[data-id="G007"]')).to_have_count(0)

    def test_crossed_subviews_show_independent_locations_and_identity_not_accuracy(self):
        item = next(entry for entry in self.result["items"] if entry["id"] == "G003")
        item["old"]["locations"] = [location(x=.1, y=.2)]
        item["new"]["locations"] = [location(x=.7, y=.2)]
        item["graphics"].update({
            "region_source": "local_subview",
            "center_displacement_pt": {"dx": 450, "dy": 0},
            "subview": {"old_index": 1, "new_index": 2,
                        "identity": {"score": .93, "old_margin": .21, "new_margin": .23},
                        "order_reversal": [{"axis": "horizontal", "old_index": 2, "new_index": 1}],
                        "drawing_size_changed": True},
        })
        self.result["graphics_coverage"]["subview_matching"] = {
            "old_candidates": 2, "new_candidates": 2, "matched": 2, "moved": 2,
            "unresolved": 0, "omitted": 0, "resolved_residual_pairs": 2, "order_reversal_pairs": 1,
        }
        self.compare()
        self.select("G003")
        detail = self.page.locator(".graphics-detail")
        for text in ("旧子图 1 → 新子图 2", "子图中心位移", "Δx 450 pt", "0.21 / 0.23",
                     "相对顺序反转", "非语义零件识别", "不等于实物尺寸变化", "但不表示内容相同"):
            expect(detail).to_contain_text(text)
        for side in ("old", "new"):
            for zoom in ("fit", "150", "200"):
                self.page.locator(f"#{side}-zoom").select_option(zoom)
                self.assert_geometry(side, "G003", item[side]["locations"][0])
            expect(self.page.locator(f"#{side}-evidence-note")).to_contain_text("非残差框")
        self.select("G001")
        expect(self.page.locator(".graphics-detail")).to_contain_text("实际局部残差像素")
        expect(self.page.locator(".graphics-detail")).not_to_contain_text("子图中心位移")
        self.page.locator(".coverage-panel > summary").click()
        expect(self.page.locator(".graphics-coverage")).to_contain_text("相对顺序反转对数")
        self.page.locator("#new-upload").set_input_files({
            "name": "replacement.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4\n%%EOF"})
        expect(self.page.locator("rect.evidence-box")).to_have_count(0)
        expect(self.page.locator(".graphics-detail")).to_have_count(0)

    def test_multisource_navigation_and_keyboard_selection(self):
        self.compare()
        button = self.page.locator('.result-item[data-id="G002"] button')
        button.focus()
        button.press("Enter")
        self.page.wait_for_load_state("networkidle")
        expect(button).to_have_attribute("aria-pressed", "true")
        for side in ("old", "new"):
            expect(self.page.locator(f"#{side}-page")).to_have_value("2")
        self.page.locator("#old-page").select_option("1")
        self.page.wait_for_load_state("networkidle")
        expect(self.page.locator('#old-stage rect[data-id="G002"]')).to_have_count(1)
        expect(self.page.locator("#new-page")).to_have_value("2")
        rect = self.page.locator('#old-stage rect[data-id="G001"]')
        rect.focus()
        rect.press("Enter")
        self.page.wait_for_load_state("networkidle")
        expect(self.page.locator('.result-item[data-id="G001"] button')).to_have_attribute("aria-pressed", "true")
        expect(self.page.locator("#new-page")).to_have_value("1")

    def test_graphics_provenance_coverage_and_safe_text(self):
        self.compare()
        self.select("G001")
        headings = self.page.locator(".source-detail h3").all_text_contents()
        self.assertTrue(all("本地渲染证据描述（非 OCR 原文）" in value for value in headings))
        expect(self.page.locator(".graphics-provenance").first).to_contain_text('"kind": "local-render"')
        expect(self.page.locator(".graphics-detail")).to_contain_text("1.25% / 2.50%")
        expect(self.page.locator(".graphics-detail")).to_contain_text("配准内点数")
        expect(self.page.locator(".graphics-detail")).to_contain_text("<img src=x onerror=alert(1)>")
        expect(self.page.locator("#detail-content img")).to_have_count(0)
        self.page.locator(".coverage-panel > summary").click()
        expect(self.page.locator(".graphics-coverage")).to_contain_text("页数统计")
        expect(self.page.locator(".graphics-coverage")).to_contain_text("区域统计")
        expect(self.page.locator(".graphics-coverage")).to_contain_text("像素上限")
        expect(self.page.locator(".graphics-coverage")).to_contain_text("合成页面回退")
        expect(self.page.locator("#coverage-content")).to_contain_text("历史分析用量（非本次新增计费）")

    def test_schema_cells_dates_and_interpretation_semantics_preserved(self):
        self.compare()
        self.select("D001")
        expect(self.page.locator(".cell-diff tbody tr")).to_have_count(1)
        expect(self.page.locator(".cell-diff")).to_contain_text("OLD-PART")
        for side in ("old", "new"):
            expect(self.page.locator(f'#{side}-stage rect[data-id="D001"][data-field="part_number"]')).to_have_count(1)
        self.page.locator("#channel-filter").select_option("schema")
        self.page.locator("#review-filter").select_option("formatting")
        self.assertEqual(self.visible_ids(), ["D003"])
        self.select("D003")
        expect(self.page.locator("#detail-content")).to_contain_text("不计工程变更")
        expect(self.page.locator("#detail-content")).to_contain_text("2026. 09. 18")
        self.page.locator("#review-filter").select_option("all")
        self.assertNotIn("D004", self.visible_ids())
        self.page.locator("#show-interpretation").check()
        self.assertIn("D004", self.visible_ids())
        self.select("D004")
        expect(self.page.locator("#detail-content")).to_contain_text("不代表图纸发生变更")
        self.page.locator("#channel-filter").select_option("ocr")
        self.assertEqual(self.visible_ids(), ["D002"])
        expect(self.page.locator(".graphics-detail")).to_have_count(0)
        expect(self.page.locator('rect[data-channel="graphics"]')).to_have_count(0)

    def test_replacement_immediately_clears_graphics_context_and_coverage(self):
        self.compare()
        self.select("G007")
        self.hold_uploads = True
        with self.page.expect_request("**/api/documents/new"):
            self.page.locator("#new-upload").set_input_files({
                "name": "replacement.pdf", "mimeType": "application/pdf",
                "buffer": b"%PDF-1.4\nsynthetic mocked upload\n%%EOF",
            })
        expect(self.page.locator("rect.evidence-box")).to_have_count(0)
        expect(self.page.locator(".result-item")).to_have_count(0)
        expect(self.page.locator(".graphics-detail")).to_have_count(0)
        expect(self.page.locator(".graphics-coverage")).to_have_count(0)
        expect(self.page.locator("#old-evidence-note")).not_to_contain_text("上下文")
        expect(self.page.locator("#compare-button")).to_be_disabled()
        self.assertEqual(len(self.held_uploads), 1)
        route, payload = self.held_uploads.pop()
        route.fulfill(json=payload)
        self.page.wait_for_load_state("networkidle")
        expect(self.page.locator("#compare-button")).to_be_enabled()
        expect(self.page.locator("rect.evidence-box")).to_have_count(0)

    def test_late_job_and_wrong_file_identity_never_restore_graphics(self):
        self.hold_jobs = True
        with self.page.expect_request("**/api/jobs/synthetic-job"):
            self.page.locator("#compare-button").click()
        with self.page.expect_request("**/api/documents/new"):
            self.page.locator("#new-upload").set_input_files({
                "name": "replacement.pdf", "mimeType": "application/pdf",
                "buffer": b"%PDF-1.4\nanother mocked upload\n%%EOF",
            })
        self.assertEqual(len(self.held_jobs), 1)
        self.held_jobs.pop().fulfill(json={"status": "succeeded", "result": self.result})
        self.page.wait_for_load_state("networkidle")
        expect(self.page.locator(".result-item")).to_have_count(0)
        expect(self.page.locator("rect.evidence-box")).to_have_count(0)
        self.hold_jobs = False
        self.page.locator("#compare-button").click()
        expect(self.page.locator("#error-message")).to_contain_text("结果文件与当前上传文件不一致")
        expect(self.page.locator("rect.evidence-box")).to_have_count(0)

    def test_invalid_residuals_and_unpaired_context_are_not_evidence(self):
        unpaired = next(item for item in self.result["items"] if item["id"] == "G005")
        unpaired["old"]["locations"] = [location(99), location(width=0)]
        unpaired["old"]["context_locations"] = [location(2)]
        self.compare()
        self.page.locator("#channel-filter").select_option("graphics")
        self.page.locator("#review-filter").select_option("unpaired")
        self.select("G005")
        expect(self.page.locator("#old-page")).to_have_value("1")
        expect(self.page.locator('#old-stage rect[data-id="G005"]')).to_have_count(0)
        expect(self.page.locator("#old-evidence-note")).to_contain_text("不绘制推测框")
        expect(self.page.locator('#new-stage rect[data-id="G005"]')).to_have_count(0)

    def test_pure_movement_context_is_navigation_only_and_residuals_take_priority(self):
        moved = next(item for item in self.result["items"] if item["id"] == "G003")
        for side in ("old", "new"):
            moved[side]["locations"] = []
            moved[side]["context_locations"] = [location(2, 0.6, 0.6, 0.3, 0.3)]
        modified = next(item for item in self.result["items"] if item["id"] == "G001")
        for side in ("old", "new"):
            modified[side]["context_locations"] = [location(2)]
        self.compare()
        self.select("G003")
        for side in ("old", "new"):
            expect(self.page.locator(f"#{side}-page")).to_have_value("2")
            expect(self.page.locator(f'#{side}-stage rect[data-id="G003"]')).to_have_count(0)
        expect(self.page.locator(".graphics-detail")).to_contain_text("不计为内容修改")
        self.select("G001")
        for side in ("old", "new"):
            expect(self.page.locator(f"#{side}-page")).to_have_value("1")
            self.assert_geometry(side, "G001", modified[side]["locations"][0])

    def test_missing_or_false_graphics_capability_preserves_schema_and_hides_graphics(self):
        for capability in (None, False):
            with self.subTest(graphics_enabled=capability):
                self.context.close()
                self.graphics_enabled = capability
                self.open_context()
                expect(self.page.locator('#channel-filter option[value="graphics"]')).to_be_disabled()
                expect(self.page.locator('#channel-filter option[value="primary"]')).to_have_text("结构化字段（默认）")
                expect(self.page.locator("#graphics-status")).to_contain_text("图形检测未接入/未启用")
                expect(self.page.locator("#coverage-content")).to_contain_text("图形检测未接入/未启用")
                expect(self.page.locator("#filter-note")).not_to_contain_text("图形候选")
                self.compare()
                self.assertEqual(self.visible_ids(), ["D001"])
                self.select("D001")
                expect(self.page.locator(".cell-diff tbody tr")).to_have_count(1)
                expect(self.page.locator(".cell-diff")).to_contain_text("OLD-PART")
                for side in ("old", "new"):
                    expect(self.page.locator(f'#{side}-stage rect[data-id="D001"][data-field="part_number"]')).to_have_count(1)
                expect(self.page.locator("#job-status")).to_contain_text("图形检测未接入/未启用")
                expect(self.page.locator("#job-status")).not_to_contain_text("位移")
                expect(self.page.locator("#coverage-content")).to_contain_text("不包含图形检测")
                expect(self.page.locator(".graphics-coverage")).to_have_count(0)
                self.page.locator("#channel-filter").select_option("all")
                self.page.locator("#review-filter").select_option("all")
                self.assertEqual(set(self.visible_ids()), {"D001", "D002", "D003"})
                expect(self.page.locator('rect[data-channel="graphics"]')).to_have_count(0)
                expect(self.page.locator(".graphics-detail")).to_have_count(0)
                self.page.set_viewport_size({"width": 700, "height": 900})
                expect(self.page.locator("#graphics-status")).to_be_visible()


if __name__ == "__main__":
    unittest.main()
