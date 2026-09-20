"""Synthetic PDF and raster evidence only; no network or customer fixtures."""

import argparse
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import pymupdf

if not all(importlib.util.find_spec(name) for name in ("cv2", "numpy")):
    raise unittest.SkipTest("Install .[graphics] for local graphical tests")

from cu_diff.cli import graphics as run_graphics_cli
from cu_diff.graphics import GraphicsError, compare_graphics


def drawing(path, *, dx=0, dy=0, lines=(20,), width=300, label="TEST A", pages=1, rotation=0,
            scale_x=1, scale_y=None):
    scale_y = scale_x if scale_y is None else scale_y
    with pymupdf.open() as pdf:
        for _ in range(pages):
            page = pdf.new_page(width=600, height=400)
            page.draw_rect(pymupdf.Rect(100+dx, 100+dy, 100+width+dx, 240+dy),
                           color=(0, 0, 0), width=.7)
            for x in lines:
                page.draw_line((100+x+dx, 100+dy), (100+x+dx, 240+dy), width=.7)
            for x in range(150, 170, 3):
                page.draw_line((x+dx, 150+dy), (x+dx, 200+dy), width=.7)
            page.draw_circle((340+dx, 170+dy), 15, width=.7)
            page.insert_text((240+dx, 215+dy), label, fontsize=12)
        with pymupdf.open() as output:
            for page in pdf:
                target = output.new_page(width=600, height=400)
                target.show_pdf_page(pymupdf.Rect(100*(1-scale_x), 100*(1-scale_y),
                                                100+500*scale_x, 100+300*scale_y),
                                     pdf, page.number, keep_proportion=False)
                target.set_rotation(rotation)
            output.save(path)


def layout(dx=0, dy=0, width=300, words=True, scale_x=1, scale_y=None):
    scale_y = scale_x if scale_y is None else scale_y
    def src(box):
        box = tuple(100+(value-100)*(scale_x if axis % 2 == 0 else scale_y)
                    for axis, value in enumerate(box))
        x0, y0, x1, y1 = (value/72 for value in box)
        return f"D(1,{x0},{y0},{x1},{y0},{x1},{y1},{x0},{y1})"
    return {"status": "Succeeded", "result": {"contents": [{
        "unit": "inch",
        "pages": [{"pageNumber": 1, "width": 600/72, "height": 400/72,
                   "words": [{"content": "TEST", "source": src((235+dx, 200+dy, 310+dx, 220+dy))}]
                   if words else []}],
        "paragraphs": [{"content": "SYNTHETIC PANEL 123"}],
        "figures": [{"source": src((80+dx, 80+dy, 120+width+dx, 260+dy)),
                     "elements": ["/paragraphs/0"]}],
    }]}}


class GraphicsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old, self.new = self.root / "old.pdf", self.root / "new.pdf"
        drawing(self.old)

    def tearDown(self):
        self.temp.cleanup()

    def run_pair(self, old=None, new=None):
        return compare_graphics(self.old, self.new, old or layout(), new or layout())

    def test_identical_render_has_no_graphical_changes(self):
        drawing(self.new)
        result = self.run_pair()
        self.assertEqual(result["items"], [])
        self.assertEqual(result["coverage"]["unchanged_regions"], 1)

    def test_added_long_vertical_lines_have_only_new_side_evidence(self):
        drawing(self.new, lines=(8, 13, 20))
        result = self.run_pair()
        feature, = [item for item in result["items"] if item["graphics"]["method"] == "rectangular_interior_vertical_runs"]
        self.assertIn("1 → 3", feature["key"])
        self.assertEqual(feature["old"]["locations"], [])
        self.assertEqual(len(feature["new"]["locations"]), 2)
        self.assertEqual(len(feature["old"]["counterpart_locations"]), 2)
        self.assertTrue(all(loc["from_side"] == "new" for loc in feature["old"]["counterpart_locations"]))
        for box in feature["new"]["locations"]:
            self.assertGreater(box["x"], 100/600)
            self.assertLess(box["x"], 120/600)
            self.assertGreater(box["height"], 130/400)

    def test_pure_translation_produces_no_design_difference(self):
        drawing(self.new, dx=18, dy=12)
        result = self.run_pair(new=layout(dx=18, dy=12))
        self.assertEqual(result["items"], [])
        self.assertEqual(result["coverage"]["unchanged_regions"], 1)
        self.assertEqual(result["coverage"]["comparison_policy"], "design_content_only")

    def test_translation_with_added_lines_keeps_design_difference(self):
        drawing(self.new, dx=18, dy=12, lines=(8, 13, 20))
        result = self.run_pair(new=layout(dx=18, dy=12))
        self.assertNotIn("visual_moved", {item["change"] for item in result["items"]})
        feature, = [i for i in result["items"]
                    if i["graphics"]["method"] == "rectangular_interior_vertical_runs"]
        self.assertEqual(feature["old"]["locations"], [])
        self.assertEqual(len(feature["new"]["locations"]), 2)
        for projected, actual in zip(feature["old"]["counterpart_locations"], feature["new"]["locations"]):
            self.assertAlmostEqual(projected["x"], actual["x"] - 18/600, delta=.001)
            self.assertAlmostEqual(projected["y"], actual["y"] - 12/400, delta=.001)

    def test_uniform_scale_is_not_default_design_content(self):
        drawing(self.new, scale_x=1.2)
        old, new = layout(), layout(scale_x=1.2)
        plain = self.run_pair(old, new)
        optional = compare_graphics(self.old, self.new, old, new, include_transformations=True)
        self.assertEqual(plain["items"], [])
        self.assertEqual(optional["coverage"]["design_content_items"], 0)
        scaled, = [item for item in optional["items"] if item["change"] == "visual_scaled"]
        for ratio in scaled["graphics"]["scale_ratio"].values():
            self.assertAlmostEqual(ratio, 1.2, delta=.01)
        self.assertAlmostEqual(scaled["graphics"]["alignment"]["scale_ratio"], 1.2, delta=.01)
        self.assertEqual(scaled["graphics"]["evidence_role"], "transformation_frame")
        self.assertTrue(scaled["graphics"]["optional_transformation"])
        for role in ("old", "new"):
            self.assertEqual(scaled[role]["locations"], scaled[role]["context_locations"])
            self.assertEqual(scaled[role]["evidence_role"], "transformation_frame")
        self.assertIn("uniform_drawing_scale", plain["coverage"]["ignored_changes"])

    def test_uniform_scaling_range_and_raster_rounding(self):
        for factor in (.65, .8, .96, 1.05, 1.3, 1.5):
            with self.subTest(scale=factor):
                drawing(self.new, scale_x=factor)
                result = compare_graphics(self.old, self.new, layout(), layout(scale_x=factor),
                                          include_transformations=True)
                self.assertEqual(result["coverage"]["design_content_items"], 0)
                scaled, = [item for item in result["items"] if item["change"] == "visual_scaled"]
                for ratio in scaled["graphics"]["scale_ratio"].values():
                    self.assertAlmostEqual(ratio, factor, delta=.01)

    def test_near_uniform_scale_diagnostics_keep_actual_axis_ratios(self):
        for factor, aspect_delta in ((.96, .002), (.96, .007), (1.05, .002), (1.05, .007)):
            with self.subTest(scale=factor, aspect_delta=aspect_delta):
                sy = factor*(1+aspect_delta)
                drawing(self.new, scale_x=factor, scale_y=sy, lines=(8, 13, 20), label="TEST B")
                old, new = layout(), layout(scale_x=factor, scale_y=sy)
                plain = compare_graphics(self.old, self.new, old, new)
                optional = compare_graphics(self.old, self.new, old, new, include_transformations=True)
                self.assertEqual(plain["items"], [item for item in optional["items"]
                                                 if not item["graphics"].get("optional_transformation")])
                self.assertIn("visual_annotation", {item["change"] for item in plain["items"]})
                feature, = [item for item in plain["items"]
                            if item["graphics"]["method"] == "rectangular_interior_vertical_runs"]
                self.assertEqual(feature["old"]["locations"], [])
                self.assertEqual(len(feature["new"]["locations"]), 2)
                scaled, = [item for item in optional["items"] if item["change"] == "visual_scaled"]
                self.assertAlmostEqual(scaled["graphics"]["scale_ratio"]["x"], factor, delta=.005)
                self.assertAlmostEqual(scaled["graphics"]["scale_ratio"]["y"], sy, delta=.005)
                self.assertIsInstance(scaled["graphics"]["alignment"]["scale_ratio"], float)
                self.assertTrue(optional["coverage"]["transformations_included"])
                self.assertEqual(optional["coverage"]["transformation_candidates"]["visual_scaled"], 1)
                self.assertFalse(plain["coverage"]["transformations_included"])

    def test_centered_uniform_scaling_does_not_invent_translation(self):
        drawing(self.new, scale_x=1.2, dx=-25, dy=-70/6)
        result = compare_graphics(self.old, self.new, layout(),
                                  layout(scale_x=1.2, dx=-25, dy=-70/6),
                                  include_transformations=True)
        self.assertEqual([item["change"] for item in result["items"]], ["visual_scaled"])

    def test_optional_translation_is_separate_from_content(self):
        drawing(self.new, dx=18, dy=12)
        result = compare_graphics(self.old, self.new, layout(), layout(dx=18, dy=12),
                                  include_transformations=True)
        moved, = result["items"]
        self.assertEqual(moved["change"], "visual_moved")
        self.assertAlmostEqual(moved["graphics"]["translation_pt"]["dx"], 18, delta=.5)
        self.assertAlmostEqual(moved["graphics"]["translation_pt"]["dy"], 12, delta=.5)
        self.assertEqual(result["coverage"]["design_content_items"], 0)

    def test_scale_with_new_or_deleted_lines_preserves_design_and_stable_ids(self):
        for old_lines, new_lines in (((20,), (8, 13, 20)), ((8, 13, 20), (20,))):
            with self.subTest(old=old_lines, new=new_lines):
                drawing(self.old, lines=old_lines)
                drawing(self.new, scale_x=1.2, lines=new_lines)
                old, new = layout(), layout(scale_x=1.2)
                plain = self.run_pair(old, new)
                optional = compare_graphics(self.old, self.new, old, new, include_transformations=True)
                content = [item for item in optional["items"]
                           if not item["graphics"].get("optional_transformation")]
                self.assertEqual(content, plain["items"])
                self.assertTrue(content)
                self.assertTrue(any(i["change"] == "visual_scaled" for i in optional["items"]))
                feature, = [i for i in content if i["graphics"]["method"]
                            == "rectangular_interior_vertical_runs"]
                empty = "old" if len(new_lines) > len(old_lines) else "new"
                self.assertEqual(feature[empty]["locations"], [])
                self.assertEqual(len(feature["new" if empty == "old" else "old"]["locations"]), 2)

    def test_scale_with_changed_text_remains_annotation(self):
        drawing(self.new, scale_x=1.2, label="TEST B")
        result = self.run_pair(new=layout(scale_x=1.2))
        self.assertTrue(any(item["change"] == "visual_annotation" for item in result["items"]))

    def test_translation_and_scale_are_separate_optional_frames(self):
        drawing(self.new, dx=18, dy=12, scale_x=1.2)
        old, new = layout(), layout(dx=18, dy=12, scale_x=1.2)
        self.assertEqual(self.run_pair(old, new)["items"], [])
        result = compare_graphics(self.old, self.new, old, new, include_transformations=True)
        self.assertEqual({item["change"] for item in result["items"]}, {"visual_moved", "visual_scaled"})
        moved = next(item for item in result["items"] if item["change"] == "visual_moved")
        self.assertAlmostEqual(moved["graphics"]["translation_pt"]["dx"], 51.6, delta=1)
        self.assertAlmostEqual(moved["graphics"]["translation_pt"]["dy"], 28.4, delta=1)

    def test_nonuniform_deformation_is_not_normalized(self):
        drawing(self.new, scale_x=1.2, scale_y=1.05)
        result = compare_graphics(self.old, self.new, layout(),
                                  layout(scale_x=1.2, scale_y=1.05), include_transformations=True)
        self.assertTrue(any(item["change"] in {"visual_modified", "visual_uncertain"}
                            for item in result["items"]))
        self.assertNotIn("visual_scaled", {item["change"] for item in result["items"]})

    def test_uniform_scale_rotated_page_has_bounded_original_evidence(self):
        drawing(self.old, rotation=90)
        drawing(self.new, scale_x=1.2, rotation=90)
        result = compare_graphics(self.old, self.new, include_transformations=True)
        self.assertEqual(result["coverage"]["design_content_items"], 0)
        self.assertIn("visual_scaled", {item["change"] for item in result["items"]})
        for item in result["items"]:
            for role in ("old", "new"):
                for loc in item[role]["locations"]:
                    self.assertTrue(0 <= loc["x"] < loc["x"]+loc["width"] <= 1)
                    self.assertTrue(0 <= loc["y"] < loc["y"]+loc["height"] <= 1)

    def test_resizing_is_not_warped_away_or_reported_as_pure_translation(self):
        drawing(self.new, width=330)
        result = self.run_pair(new=layout(width=330))
        changed = [item for item in result["items"] if item["change"] == "visual_modified"]
        self.assertTrue(changed)
        self.assertFalse(any(item["change"] == "visual_moved" for item in result["items"]))
        self.assertAlmostEqual(changed[0]["graphics"]["alignment"]["outline_width_ratio"], 1.1, delta=.02)
        self.assertGreater(changed[0]["graphics"]["changed_pixels"]["new"], 20)

    def test_changed_text_is_annotation_not_a_material_inference(self):
        drawing(self.new, label="TEST B")
        result = self.run_pair()
        self.assertTrue(any(item["change"] == "visual_annotation" for item in result["items"]))
        self.assertTrue(all(item["review_required"] for item in result["items"]))

    def test_absent_cu_figures_explicitly_uses_full_page(self):
        drawing(self.new, lines=(8, 13, 20))
        result = compare_graphics(self.old, self.new)
        self.assertEqual(result["coverage"]["fallback_pages"], 1)
        self.assertTrue(result["items"])
        self.assertTrue(all(item["graphics"]["region_source"] == "page_fallback" for item in result["items"]))

    def test_ambiguous_figure_pairing_is_not_forced(self):
        drawing(self.new)
        old, new = layout(), layout()
        for response in (old, new):
            figures = response["result"]["contents"][0]["figures"]
            figures.append(deepcopy(figures[0]))
        result = self.run_pair(old, new)
        self.assertEqual(result["coverage"]["paired_regions"], 0)
        self.assertEqual(result["coverage"]["unpaired_regions"], 4)
        self.assertEqual({item["change"] for item in result["items"]}, {"unpaired_old", "unpaired_new"})
        for item in result["items"]:
            self.assertTrue(item["old"] is None or item["new"] is None)

    def test_added_page_does_not_invent_old_page_evidence(self):
        drawing(self.new, pages=2)
        result = self.run_pair()
        added, = [item for item in result["items"] if item["change"] == "unpaired_new"]
        self.assertIsNone(added["old"])
        self.assertEqual(added["new"]["locations"][0]["page"], 2)

    def test_pdf_rotation_normalization_preserves_comparison(self):
        drawing(self.new, rotation=90)
        with pymupdf.open(self.new) as pdf:
            pdf[0].remove_rotation()
            pdf.save(self.old)
        result = compare_graphics(self.old, self.new)
        self.assertEqual(result["items"], [])

    def test_coordinates_and_provenance_are_original_page_relative(self):
        drawing(self.new, lines=(8, 13, 20))
        result = self.run_pair()
        for item in result["items"]:
            for role in ("old", "new"):
                evidence = item[role]
                if evidence is None:
                    continue
                self.assertEqual(evidence["source"]["kind"], "local_pdf_render")
                self.assertIsNone(evidence["confidence"])
                for location in evidence["locations"]:
                    self.assertTrue(0 <= location["x"] < location["x"]+location["width"] <= 1)
                    self.assertTrue(0 <= location["y"] < location["y"]+location["height"] <= 1)

    def test_invalid_dpi_and_failed_progress_are_not_success_shaped(self):
        drawing(self.new)
        with self.assertRaises(GraphicsError):
            compare_graphics(self.old, self.new, dpi=600)
        def stop(_):
            raise ValueError("Synthetic cancelled job")
        with self.assertRaisesRegex(ValueError, "cancelled"):
            compare_graphics(self.old, self.new, progress=stop)

    def test_response_objects_are_not_modified(self):
        drawing(self.new, lines=(8, 13, 20))
        old, new = layout(), layout()
        original = deepcopy((old, new))
        self.run_pair(old, new)
        self.assertEqual((old, new), original)

    def test_local_cli_writes_provenance_without_cu(self):
        drawing(self.new, lines=(8, 13, 20))
        output = self.root / "results"
        run_graphics_cli(argparse.Namespace(
            old=self.old, new=self.new, old_response=None, new_response=None,
            old_metadata=None, new_metadata=None, dpi=160, output=output))
        result = json.loads((output / "graphics.json").read_text(encoding="utf-8"))
        self.assertEqual(result["provenance"]["azure_calls"], 0)
        self.assertTrue((output / "graphics.zh.md").exists())

    def test_cli_transformation_flags_filter_json_and_report(self):
        drawing(self.new, scale_x=1.2, dx=18, dy=12)
        for translation, scaling in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(translation=translation, scaling=scaling):
                output = self.root / f"results-{translation}-{scaling}"
                run_graphics_cli(argparse.Namespace(
                    old=self.old, new=self.new, old_response=None, new_response=None,
                    old_metadata=None, new_metadata=None, dpi=160, output=output,
                    include_translation=translation, include_scaling=scaling))
                result = json.loads((output / "graphics.json").read_text(encoding="utf-8"))
                report = (output / "graphics.zh.md").read_text(encoding="utf-8")
                changes = {item["change"] for item in result["items"]}
                self.assertEqual("visual_moved" in changes, translation)
                self.assertEqual("visual_scaled" in changes, scaling)
                self.assertEqual(result["coverage"]["design_content_items"], 0)
                for item in result["items"]:
                    self.assertIn(item["id"], report)

    def test_cli_rejects_wrong_cu_document_hash(self):
        drawing(self.new)
        metadata = self.root / "metadata.json"
        metadata.write_text('{"document_sha256":"wrong"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash does not match"):
            run_graphics_cli(argparse.Namespace(
                old=self.old, new=self.new, old_response=self.root/"response.json", new_response=None,
                old_metadata=metadata, new_metadata=None, dpi=160, output=self.root/"results"))
