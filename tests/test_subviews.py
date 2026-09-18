"""Generic generated views: no customer geometry, vocabulary, or response fixtures."""

import importlib.util
from pathlib import Path
import tempfile
import unittest

import pymupdf

if not all(importlib.util.find_spec(name) for name in ("cv2", "numpy")):
    raise unittest.SkipTest("Install .[graphics] for subview tests")

from cu_diff.graphics import compare_graphics


def document(path, parts, *, rotation=0, frame=False, extra_line=False, label="ONE"):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=500, height=300)
        if frame:
            page.draw_rect((5, 5, 495, 295), width=.7)
        for kind, x, y, scale, changed in parts:
            def point(a, b):
                return x + a * scale, y + b * scale
            def rect(a, b, c, d):
                page.draw_rect((*point(a, b), *point(c, d)), width=.7)
            rect(0, 0, 80, 100)
            if kind == "window":
                rect(15, 20, 65, 55)
                page.draw_circle(point(40, 70), 5 * scale, width=.7)
                page.insert_text(point(10, 90), label, fontsize=8 * scale)
            elif kind == "ribs":
                for row in range(15, 81, 13):
                    page.draw_line(point(10, row), point(70, row), width=.7)
            else:
                page.draw_line(point(10, 10), point(70, 90), width=.7)
                page.draw_line(point(70, 10), point(10, 90), width=.7)
            if changed:
                page.draw_line(point(17, 23), point(62, 52), width=1)
            page.insert_text((x, y + 112 * scale), "SYNTHETIC VIEW", fontsize=7)
        if extra_line:
            page.draw_line((330, 210), (450, 250), width=1)
        page.set_rotation(rotation)
        pdf.save(path)


def cu_layout(parts):
    def source(box):
        x0, y0, x1, y1 = box
        return f"D(1,{x0},{y0},{x1},{y0},{x1},{y1},{x0},{y1})"
    return {"result": {"contents": [{
        "unit": "pixel",
        "pages": [{"pageNumber": 1, "width": 500, "height": 300,
                   "words": [{"content": "SYNTHETIC", "source": source((x+8, y+80, x+55, y+94))}
                             for kind, x, y, _, _ in parts if kind == "window"]}],
        "figures": [{"source": source((20, 20, 470, 275))}],
    }]}}


OLD = [("window", 50, 50, 1, False), ("ribs", 240, 50, 1, False)]
SWAPPED = [("window", 240, 50, 1, False), ("ribs", 50, 50, 1, False)]


class SubviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old, self.new = (Path(self.temp.name) / name for name in ("old.pdf", "new.pdf"))

    def tearDown(self):
        self.temp.cleanup()

    def compare(self, old=OLD, new=SWAPPED, *, old_options=None, new_options=None, layout=True):
        document(self.old, old, **(old_options or {}))
        document(self.new, new, **(new_options or {}))
        return compare_graphics(self.old, self.new,
                                cu_layout(old) if layout else None, cu_layout(new) if layout else None)

    def moves(self, result):
        return [item for item in result["items"] if item["change"] == "visual_moved"
                and item["graphics"]["region_source"] == "local_subview"]

    def test_distinct_views_swap_by_appearance_not_same_position(self):
        result = self.compare()
        moves = self.moves(result)
        self.assertEqual(len(moves), 2)
        self.assertEqual(result["coverage"]["subview_matching"]["order_reversal_pairs"], 1)
        self.assertEqual({(m["graphics"]["subview"]["old_index"], m["graphics"]["subview"]["new_index"])
                          for m in moves}, {(1, 2), (2, 1)})
        for item in moves:
            self.assertEqual(item["match"]["method"], "mutual_subview_shape_and_ink")
            delta = item["graphics"]["center_displacement_pt"]
            self.assertAlmostEqual(abs(delta["dx"]), 190, delta=.5)
            self.assertAlmostEqual(delta["dy"], 0, delta=.5)
            self.assertEqual(item["graphics"]["subview"]["order_reversal"][0]["axis"], "horizontal")
            self.assertEqual(len(item["old"]["locations"]), 1)
            self.assertEqual(len(item["new"]["locations"]), 1)
        self.assertFalse(any(item["change"] == "visual_uncertain" for item in result["items"]))
        self.assertEqual(result["coverage"]["uncertain_regions"], 0)

    def test_one_moves_while_other_stays_and_order_is_not_fabricated(self):
        result = self.compare(new=[("window", 50, 160, 1, False), OLD[1]])
        move, = self.moves(result)
        self.assertAlmostEqual(move["graphics"]["center_displacement_pt"]["dy"], 110, delta=.5)
        self.assertEqual(move["graphics"]["subview"]["order_reversal"], [])
        self.assertEqual(result["coverage"]["subview_matching"]["matched"], 2)

    def test_movement_does_not_hide_changed_shape_or_invent_old_residual(self):
        result = self.compare(new=[("window", 240, 50, 1, True), SWAPPED[1]])
        self.assertEqual(len(self.moves(result)), 2)
        changes = [i for i in result["items"] if i["change"] == "visual_modified"
                   and i["graphics"]["region_source"] == "local_subview"]
        self.assertTrue(changes)
        self.assertTrue(any(item["new"]["locations"] for item in changes))
        self.assertTrue(all(not item["old"]["locations"] for item in changes))

    def test_moved_label_change_remains_annotation(self):
        result = self.compare(new_options={"label": "TWO"})
        self.assertEqual(len(self.moves(result)), 2)
        self.assertTrue(any(item["change"] == "visual_annotation"
                            and item["graphics"]["region_source"] == "local_subview"
                            for item in result["items"]))

    def test_repeated_identical_views_do_not_get_position_tie_break(self):
        repeated = [OLD[0], ("window", 240, 50, 1, False)]
        result = self.compare(old=repeated, new=[("window", 50, 160, 1, False), repeated[1]])
        self.assertEqual(self.moves(result), [])
        self.assertEqual(result["coverage"]["subview_matching"]["matched"], 0)
        self.assertEqual(result["coverage"]["subview_matching"]["unresolved"], 4)
        self.assertTrue(any(item["change"] == "visual_uncertain" for item in result["items"]))

    def test_identical_diagram_and_uniform_parent_motion_do_not_duplicate_moves(self):
        result = self.compare(new=OLD)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["coverage"]["unchanged_regions"], 1)
        translated = [(k, x+12, y+9, s, c) for k, x, y, s, c in OLD]
        result = self.compare(new=translated)
        self.assertEqual(self.moves(result), [])
        self.assertEqual(len([i for i in result["items"] if i["change"] == "visual_moved"]), 1)

    def test_size_change_is_preserved_and_is_not_false_motion(self):
        result = self.compare(new=[("window", 50, 50, 1.2, False), OLD[1]])
        self.assertEqual(self.moves(result), [])
        changed = [i for i in result["items"] if i["change"] == "visual_modified"]
        self.assertTrue(changed)
        sizes = [i["graphics"]["subview"] for i in changed if "subview" in i["graphics"]]
        self.assertTrue(any(v["drawing_size_changed"] for v in sizes))
        self.assertTrue(all(i["graphics"]["alignment"]["note"] for i in changed))

    def test_full_page_border_does_not_hide_nested_views(self):
        result = self.compare(layout=False, old_options={"frame": True}, new_options={"frame": True})
        self.assertEqual(len(self.moves(result)), 2)
        self.assertEqual(result["coverage"]["fallback_pages"], 1)

    def test_rotation_and_page_relative_boxes_preserve_vertical_swap(self):
        result = self.compare(layout=False, old_options={"rotation": 90}, new_options={"rotation": 90})
        moves = self.moves(result)
        self.assertEqual(len(moves), 2)
        for item in moves:
            self.assertEqual(item["graphics"]["subview"]["order_reversal"][0]["axis"], "vertical")
            self.assertAlmostEqual(abs(item["graphics"]["center_displacement_pt"]["dy"]), 190, delta=.5)
            for role in ("old", "new"):
                loc, = item[role]["locations"]
                box = item[role]["source"]["region_pt"]
                self.assertAlmostEqual(loc["x"], box[0] / 300)
                self.assertAlmostEqual(loc["y"], box[1] / 500)
                self.assertLessEqual(loc["x"] + loc["width"], 1)
                self.assertLessEqual(loc["y"] + loc["height"], 1)

    def test_unmatched_view_and_uncovered_ink_remain_in_parent_evidence(self):
        result = self.compare(new=OLD + [("cross", 350, 155, 1, False)])
        self.assertEqual(result["coverage"]["subview_matching"]["unresolved"], 1)
        self.assertTrue(any(i["new"]["locations"] and not i["old"]["locations"]
                            for i in result["items"] if i["graphics"]["region_source"] != "local_subview"))
        result = self.compare(new_options={"extra_line": True})
        parent = [i for i in result["items"] if i["graphics"]["region_source"] != "local_subview"]
        self.assertTrue(any(i["new"]["locations"] for i in parent))

    def test_candidate_cap_does_not_create_false_unique_correspondence(self):
        import cv2 as cv
        import numpy as np
        from cu_diff.graphics import Region, _refine_subviews
        image = np.zeros((500, 500), dtype=np.uint8)
        for y in range(20, 430, 80):
            for x in range(20, 430, 80):
                cv.rectangle(image, (x, y), (x+30, y+30), 1, 1)
        stats, emitted, _ = _refine_subviews(
            {"old": image, "new": image}, {"old": (0, 0), "new": (0, 0)}, 1,
            {r: Region(1, (0, 0, 500, 500), "page_fallback") for r in ("old", "new")},
            {r: (500, 500) for r in ("old", "new")},
            {r: np.zeros_like(image) for r in ("old", "new")},
            {"accepted": False, "dx_pixels": 0, "dy_pixels": 0},
            (image.copy(), image.copy()), lambda *args: self.fail("Truncation must not emit pairs"))
        self.assertGreater(stats["omitted"], 0)
        self.assertEqual(stats["matched"], 0)
        self.assertEqual(emitted, 0)


if __name__ == "__main__":
    unittest.main()
