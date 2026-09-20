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


def drawing(path, *, dx=0, dy=0, lines=(20,), width=300, label="TEST A", pages=1, rotation=0):
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
            page.set_rotation(rotation)
        pdf.save(path)


def layout(dx=0, dy=0, width=300, words=True):
    def src(box):
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

    def test_cli_rejects_wrong_cu_document_hash(self):
        drawing(self.new)
        metadata = self.root / "metadata.json"
        metadata.write_text('{"document_sha256":"wrong"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash does not match"):
            run_graphics_cli(argparse.Namespace(
                old=self.old, new=self.new, old_response=self.root/"response.json", new_response=None,
                old_metadata=metadata, new_metadata=None, dpi=160, output=self.root/"results"))
