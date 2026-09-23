import unittest
from unittest.mock import patch

import pymupdf

from cu_diff.client import CUError, digest
from cu_diff.model_visual import localize, _subfeature_registration
from cu_diff.graphics import _libraries


def box(x0, y0, x1, y1):
    return dict(zip(("x0", "y0", "x1", "y1"), [round(x0/240*1000), round(y0/180*1000),
                                               round(x1/240*1000), round(y1/180*1000)]))


def fixture(*, fill=True, dx=0, scale=1, outline=True, leader=False):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=240, height=180)
        def point(x, y):
            return pymupdf.Point(x*scale+dx, y*scale)
        if outline:
            page.draw_circle(point(100, 90), 50*scale, width=.7)
            page.draw_circle(point(100, 90), 43*scale, width=.7)
            for y in (70, 110):
                page.draw_circle(point(100, y), 4*scale, width=.7)
        if fill:
            for x in (63, 117):
                for offset in range(0, 38, 3):
                    page.draw_line(point(x, 70+offset), point(x+18, 62+offset), width=.6)
        if leader:
            page.draw_line(point(148, 90), point(159, 90), width=.6)
        page.insert_text((175, 90), "SYN 8 +/- 1", fontsize=8)
        data = page.get_pixmap(matrix=pymupdf.Matrix(3, 3), alpha=False).tobytes("png")
    mapping = {"page": 1, "rect": [0, 0, 240, 180], "width": 240, "height": 180,
               "page_width": 240, "page_height": 180, "actual_dpi": 216,
               "source_sha256": digest(data)}
    return None, data, mapping


def proposal():
    return {"views": [{
        "label": "Synthetic end view", "old_box": box(45, 35, 155, 145), "new_box": box(45, 35, 155, 145),
        "features": [{
            "label": side+" fill", "kind": "fill", "assessment": "changed",
            "old_description": "Hatched area", "new_description": "No hatch observed",
            "rationale": "Common outer rings retained",
            "old_box": box(x0, 60, x1, 114), "new_box": box(x0, 60, x1, 114),
        } for side, x0, x1 in (("Left", 61, 83), ("Right", 115, 138))],
    }], "limitations": []}


def strip_fixture(*, fill, short_body):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=420, height=220)
        page.draw_rect(pymupdf.Rect(90 if short_body else 20, 40, 230, 180), width=.7)
        for y in (70, 150):
            page.draw_line((230, y), (380, y), width=.7)
        if fill:
            for y in range(44, 174, 3):
                page.draw_line((232, y+3), (240, y), width=.6)
        data = page.get_pixmap(matrix=pymupdf.Matrix(3, 3), alpha=False).tobytes("png")
    mapping = {"page": 1, "rect": [0, 0, 420, 220], "width": 420, "height": 220,
               "page_width": 420, "page_height": 220, "actual_dpi": 216,
               "source_sha256": digest(data)}
    return None, data, mapping


def strip_proposal():
    def grid(rect):
        return dict(zip(("x0", "y0", "x1", "y1"),
                        [round(v/size*1000) for v, size in zip(rect, (420, 220, 420, 220))]))
    return {"views": [{
        "label": "Synthetic side view",
        "old_box": grid((10, 20, 395, 195)), "new_box": grid((10, 20, 395, 195)),
        "features": [{
            "label": "Synthetic narrow fill", "kind": "fill", "assessment": "changed",
            "old_description": "Hatching", "new_description": "No hatching observed",
            "rationale": "Local common body edge and two leader origins",
            "old_box": grid((231, 41, 242, 179)), "new_box": grid((231, 41, 242, 179)),
        }],
    }], "limitations": []}


class ModelVisualTests(unittest.TestCase):
    def setUp(self):
        self.crops = {"old": fixture(), "new": fixture(fill=False)}
        self.words = {s: [{"locations": [{"page": 1, "x": 175/240, "y": 81/180,
                                         "width": 55/240, "height": 12/180}]}] for s in ("old", "new")}

    def run_local(self, result=None):
        return localize(result or proposal(), self.crops, self.words, {"label": "Synthetic component"})

    def test_disconnected_hatches_are_separate_raster_evidence_not_dimension_text(self):
        items = self.run_local()
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertEqual(item["change"], "model_visual_modified")
            self.assertEqual(item["old"]["raw_text"], "")
            self.assertIsNone(item["old"]["confidence"])
            self.assertEqual(len(item["old"]["locations"]), 1)
            self.assertEqual(item["new"]["locations"], [])
            self.assertTrue(item["new"]["context_locations"])
            self.assertEqual(item["old"]["source"][0]["kind"], "pdf_raster")
            self.assertLess(item["old"]["locations"][0]["x"]+item["old"]["locations"][0]["width"], 175/240)
        left, right = (i["old"]["locations"][0] for i in items)
        self.assertLess(left["x"]+left["width"], right["x"])

    def test_identical_images_do_not_become_differences_from_model_claims(self):
        self.crops["new"] = self.crops["old"]
        for item in self.run_local():
            self.assertEqual(item["change"], "model_visual_review")
            self.assertEqual(item["old"]["locations"], [])
            self.assertEqual(item["new"]["locations"], [])

    def test_cu_text_is_excluded_even_when_model_calls_it_fill(self):
        for s in self.words:
            self.words[s].append({"locations": [{"page": 1, "x": 60/240, "y": 58/180,
                                                 "width": 24/240, "height": 60/180}]})
        left, right = self.run_local()
        self.assertEqual(left["old"]["locations"], [])
        self.assertEqual(left["change"], "model_visual_review")
        self.assertEqual(right["change"], "model_visual_modified")

    def test_model_uncertainty_does_not_create_change_frames(self):
        result = proposal()
        result["views"][0]["features"][0]["assessment"] = "uncertain"
        item = self.run_local(result)[0]
        self.assertEqual(item["old"]["locations"], [])
        self.assertEqual(item["visual_comparison"]["measurement_status"], "unmeasured")
        self.assertEqual(item["visual_comparison"]["changed_pixels"], {"old": None, "new": None})

    def test_bad_bounds_wrong_containment_and_budgets_fail_explicitly(self):
        for modify in (
            lambda p: p["views"][0]["old_box"].update(x0=-1),
            lambda p: p["views"][0]["new_box"].update(x1=1001),
            lambda p: p["views"][0]["features"][0]["old_box"].update(x0=0),
            lambda p: p["views"][0]["old_box"].update(x0=True),
            lambda p: p["views"][0].update(features=[]),
            lambda p: p.update(views=p["views"]*5),
            lambda p: p["views"][0].update(features=p["views"][0]["features"]*2),
        ):
            result = proposal()
            modify(result)
            with self.assertRaises(CUError):
                self.run_local(result)

    def test_missing_common_geometry_stays_review_only(self):
        self.crops = {"old": fixture(outline=False), "new": fixture(outline=False, fill=False)}
        for item in self.run_local():
            self.assertEqual(item["change"], "model_visual_review")
            self.assertFalse(item["visual_comparison"]["alignment"]["accepted"])

    def test_crop_mapping_is_back_to_original_page(self):
        before = self.run_local()
        for crop in self.crops.values():
            crop[2].update(page=2, rect=[30, 40, 270, 220], page_width=480, page_height=360)
        self.words = {"old": [], "new": []}
        after = self.run_local()
        for previous, current in zip(before, after):
            a, b = previous["old"]["locations"][0], current["old"]["locations"][0]
            self.assertEqual(b["page"], 2)
            self.assertAlmostEqual(b["x"], a["x"]/2+30/480)
            self.assertAlmostEqual(b["y"], a["y"]/2+40/360)

    def test_translation_does_not_shift_evidence_out_of_original_coordinates(self):
        self.crops["new"] = fixture(fill=False, dx=12)
        result = proposal()
        view = result["views"][0]
        view["new_box"] = box(57, 35, 167, 145)
        for feature in view["features"]:
            for key in ("x0", "x1"):
                feature["new_box"][key] += 50
        for item in self.run_local(result):
            self.assertEqual(item["change"], "model_visual_modified")
            self.assertEqual(item["new"]["locations"], [])

    def test_uniform_drawing_scale_is_not_itself_a_content_change(self):
        self.crops["new"] = fixture(fill=False, scale=1.08)
        result = proposal()
        view = result["views"][0]
        for container in (view, *view["features"]):
            container["new_box"] = {k: round(v*1.08) for k, v in container["new_box"].items()}
        for item in self.run_local(result):
            self.assertEqual(item["change"], "model_visual_modified")
            self.assertEqual(item["new"]["locations"], [])

    def test_view_extrema_contaminated_by_leader_use_distributed_common_contour(self):
        self.crops["old"] = fixture(leader=True)
        result = proposal()
        for s in ("old", "new"):
            result["views"][0][s+"_box"] = box(45, 35, 162, 145)
        for item in self.run_local(result):
            self.assertEqual(item["change"], "model_visual_modified")
            self.assertEqual(item["new"]["locations"], [])
            self.assertTrue(item["visual_comparison"]["alignment"]["distributed_contour_support"])

    def test_failed_whole_view_uses_only_subfeature_neighborhood_evidence(self):
        self.crops = {"old": strip_fixture(fill=True, short_body=False),
                      "new": strip_fixture(fill=False, short_body=True)}
        self.words = {"old": [], "new": []}
        item, = self.run_local(strip_proposal())
        self.assertEqual(item["change"], "model_visual_modified")
        alignment = item["visual_comparison"]["alignment"]
        self.assertEqual(alignment["scope"], "subfeature_neighborhood")
        self.assertFalse(alignment["whole_view_alignment"]["accepted"])
        self.assertTrue(alignment["accepted"])
        self.assertGreaterEqual(alignment["feature_overlap"], .5)
        self.assertLessEqual(len(alignment["fallback_attempts"]), 3)
        self.assertEqual(item["new"]["locations"], [])
        box, = item["old"]["locations"]
        self.assertGreaterEqual(box["x"], 231/420-.001)
        self.assertLessEqual(box["x"]+box["width"], 242/420+.001)
        self.assertLess(box["width"], .04)
        self.assertEqual(item["visual_comparison"]["measurement_status"], "measured")

    def test_local_retry_does_not_turn_unrelated_body_redraw_into_fill_change(self):
        self.crops = {"old": strip_fixture(fill=False, short_body=False),
                      "new": strip_fixture(fill=False, short_body=True)}
        self.words = {"old": [], "new": []}
        item, = self.run_local(strip_proposal())
        self.assertEqual(item["change"], "model_visual_review")
        self.assertEqual(item["old"]["locations"], [])
        self.assertEqual(item["new"]["locations"], [])
        self.assertEqual(item["visual_comparison"]["changed_pixels"], {"old": 0, "new": 0})
        self.assertEqual(item["visual_comparison"]["measurement_status"], "measured")

    def test_local_retry_rejects_one_dimensional_anchor_and_neighbor_jump(self):
        _, np = _libraries()
        ink = {s: np.zeros((120, 120), np.uint8) for s in ("old", "new")}
        masks = {s: np.zeros((120, 120), np.uint8) for s in ink}
        for s in ink:
            masks[s][40:80, 50:60] = 1
            ink[s][30, :] = 1
        args = (ink, masks, masks, {s: (120, 120) for s in ink}, 1,
                {"accepted": False, "reason": "Synthetic whole-view failure"})
        residuals, alignment = _subfeature_registration(*args)
        self.assertIsNone(residuals)
        self.assertFalse(alignment["accepted"])
        self.assertEqual(len(alignment["fallback_attempts"]), 3)
        for s in ink:
            ink[s][90, :] = 1
        with patch("cu_diff.model_visual._registration", return_value=(None, {
                "accepted": True, "scale_ratio": 1., "dx_pixels": 40., "dy_pixels": 0.})):
            residuals, alignment = _subfeature_registration(*args)
        self.assertIsNone(residuals)
        self.assertFalse(alignment["accepted"])
        self.assertTrue(any("相邻图形" in a.get("reason", "") for a in alignment["fallback_attempts"]))


if __name__ == "__main__":
    unittest.main()
