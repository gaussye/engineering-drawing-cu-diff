"""Resize the review workspace using real browser input and synthetic evidence only."""

import unittest

from tests import test_graphics_browser as graphics

expect = graphics.expect


class ReviewLayoutBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        graphics.GraphicsBrowserTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        graphics.GraphicsBrowserTests.tearDownClass.__func__(cls)

    setUp = graphics.GraphicsBrowserTests.setUp
    tearDown = graphics.GraphicsBrowserTests.tearDown
    open_context = graphics.GraphicsBrowserTests.open_context
    route = graphics.GraphicsBrowserTests.route
    compare = graphics.GraphicsBrowserTests.compare
    select = graphics.GraphicsBrowserTests.select

    def height(self, selector):
        return self.page.locator(selector).bounding_box()["height"]

    def begin_drag(self):
        handle = self.page.get_by_role("separator", name="调整图纸与详情面板高度")
        handle.scroll_into_view_if_needed()
        box = handle.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        self.page.mouse.move(x, y)
        self.page.mouse.down()
        return x, y

    def drag(self, upward):
        x, y = self.begin_drag()
        self.page.mouse.move(x + 20, y - upward, steps=8)
        self.page.mouse.up()

    def assert_bounds(self):
        handle = self.page.locator("#review-splitter")
        detail = self.height("#detail-panel")
        self.assertGreaterEqual(detail + 1, float(handle.get_attribute("aria-valuemin")))
        self.assertLessEqual(detail - 1, float(handle.get_attribute("aria-valuemax")))
        self.assertAlmostEqual(detail, float(handle.get_attribute("aria-valuenow")), delta=1)
        drawing_minimum = self.page.locator("#drawing-grid").evaluate(
            "node => parseFloat(getComputedStyle(node).minHeight)")
        self.assertGreaterEqual(self.height("#drawing-grid") + 1, drawing_minimum)
        bottom = self.page.locator("#detail-panel").bounding_box()
        area = self.page.locator(".review-area").bounding_box()
        self.assertLessEqual(bottom["y"] + bottom["height"], area["y"] + area["height"] + 1)

    def assert_evidence_geometry(self):
        for side, x in (("old", .1), ("new", .15)):
            image = self.page.locator(f"#{side}-stage img").bounding_box()
            svg = self.page.locator(f"#{side}-stage svg").bounding_box()
            marker = self.page.locator(f'#{side}-stage rect[data-id="D001"]').bounding_box()
            self.assertAlmostEqual(image["width"], svg["width"], delta=1)
            self.assertAlmostEqual(image["height"], svg["height"], delta=1)
            self.assertAlmostEqual(marker["x"] + marker["width"] / 2,
                                   image["x"] + image["width"] * (x + .05), delta=.1)
            self.assertAlmostEqual(marker["y"] + marker["height"] / 2,
                                   image["y"] + image["height"] * .25, delta=.1)

    def test_drag_adjusts_both_windows_and_preserves_evidence_and_tabs_without_analysis(self):
        self.compare()
        self.select("D001")
        self.assert_evidence_geometry()
        detail, drawing = self.height("#detail-panel"), self.height("#drawing-grid")
        viewport = self.height("#old-viewport")
        self.drag(140)
        self.assertAlmostEqual(self.height("#detail-panel"), detail + 140, delta=1)
        self.assertAlmostEqual(self.height("#drawing-grid"), drawing - 140, delta=1)
        self.assertAlmostEqual(self.height("#old-viewport"), viewport - 140, delta=1)
        self.assert_bounds()
        self.assert_evidence_geometry()
        for tab in ("分析耗时", "用量与费用", "证据详情"):
            self.page.get_by_role("tab", name=tab).click()
            self.assertAlmostEqual(self.height("#detail-panel"), detail + 140, delta=1)
        self.drag(-80)
        self.assertAlmostEqual(self.height("#detail-panel"), detail + 60, delta=1)
        self.assert_evidence_geometry()
        self.assertEqual(self.compare_requests, 1)
        expect(self.page.locator("body")).not_to_have_class("resizing-review")

    def test_keyboard_extremes_default_reset_and_resize_keep_both_windows_accessible(self):
        handle = self.page.locator("#review-splitter")
        expect(handle).to_have_attribute("aria-orientation", "horizontal")
        original = self.height("#detail-panel")
        handle.focus()
        self.page.keyboard.press("ArrowUp")
        self.assertAlmostEqual(self.height("#detail-panel"), original + 24, delta=1)
        self.page.keyboard.press("Shift+ArrowUp")
        self.assertAlmostEqual(self.height("#detail-panel"), original + 104, delta=1)
        self.page.keyboard.press("PageDown")
        self.assertAlmostEqual(self.height("#detail-panel"), original + 24, delta=1)
        self.page.keyboard.press("End")
        self.assert_bounds()
        preferred = self.height("#detail-panel")
        self.page.set_viewport_size({"width": 1600, "height": 650})
        self.page.wait_for_function(
            "Number(document.querySelector('#review-splitter').getAttribute('aria-valuenow')) < 400")
        self.assert_bounds()
        self.page.set_viewport_size({"width": 1600, "height": 1050})
        expect(handle).to_have_attribute("aria-valuenow", str(round(preferred)))
        self.assert_bounds()
        self.page.keyboard.press("Home")
        self.assertAlmostEqual(self.height("#detail-panel"), 120, delta=1)
        handle.dblclick()
        self.assertAlmostEqual(self.height("#detail-panel"), original, delta=1)
        self.assertEqual(self.compare_requests, 0)

    def test_escape_and_pointer_cancel_restore_prior_size_and_release_drag(self):
        original = self.height("#detail-panel")
        for cancel in ("escape", "pointercancel"):
            with self.subTest(cancel=cancel):
                x, y = self.begin_drag()
                self.page.mouse.move(x, y - 110, steps=5)
                self.assertAlmostEqual(self.height("#detail-panel"), original + 110, delta=1)
                if cancel == "escape":
                    self.page.keyboard.press("Escape")
                else:
                    self.page.locator("#review-splitter").dispatch_event(
                        "pointercancel", {"pointerId": 1, "pointerType": "mouse", "isPrimary": True})
                self.page.mouse.up()
                self.assertAlmostEqual(self.height("#detail-panel"), original, delta=1)
                expect(self.page.locator("body")).not_to_have_class("resizing-review")
                self.page.mouse.move(x, y - 200)
                self.assertAlmostEqual(self.height("#detail-panel"), original, delta=1)

    def test_mobile_touch_drag_and_three_tabs_fit_without_horizontal_overflow(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        handle = self.page.locator("#review-splitter")
        handle.scroll_into_view_if_needed()
        expect(handle).to_have_attribute("aria-valuenow", "250")
        box = handle.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        cdp = self.context.new_cdp_session(self.page)
        cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": True})
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y - 100}]})
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        cdp.detach()
        expect(handle).to_have_attribute("aria-valuenow", "350")
        self.assert_bounds()
        for name in ("证据详情", "分析耗时", "用量与费用"):
            tab = self.page.get_by_role("tab", name=name)
            tab.click()
            bounds = tab.bounding_box()
            self.assertGreaterEqual(bounds["x"], 0)
            self.assertLessEqual(bounds["x"] + bounds["width"], 390)
            expect(self.page.get_by_role("tabpanel", name=name)).to_be_visible()
        self.assertEqual(self.compare_requests, 0)
