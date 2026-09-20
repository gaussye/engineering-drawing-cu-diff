"""Synthetic coordinate contracts for derived counterpart frames."""

from copy import deepcopy
import unittest

from cu_diff.graphics import GraphicsError, Region, _link_counterparts, _location, _side


class CounterpartTests(unittest.TestCase):
    def setUp(self):
        self.old_size, self.new_size = (200, 100), (300, 180)
        self.box = (40, 30, 60, 40)
        self.old = _side(Region(1, (0, 0, 200, 100), "cu_figure"), self.old_size,
                         [_location(self.box, 1, self.old_size)], 2, (20, 40))
        self.new = _side(Region(1, (0, 0, 300, 180), "cu_figure"), self.new_size, [], 2, (80, 60))
        self.metadata = {
            "alignment": {"accepted": True, "method": "verified_uniform_scale",
                          "scale_ratio": 1.2, "dx_pixels": 4, "dy_pixels": -2},
            "changed_pixels": {"old": 70, "new": 0},
        }

    def assert_box(self, location, box, size):
        for name, expected in zip(("x", "y", "width", "height"),
                                  (box[0]/size[0], box[1]/size[1],
                                   (box[2]-box[0])/size[0], (box[3]-box[1])/size[1])):
            self.assertAlmostEqual(location[name], expected, places=12)

    def test_forward_scale_and_crop_origins_preserve_observed_evidence(self):
        old = deepcopy(self.old)
        metadata = deepcopy(self.metadata)
        _link_counterparts(self.old, self.new, self.metadata)
        loc, = self.new["counterpart_locations"]
        self.assert_box(loc, (78, 41, 102, 53), self.new_size)
        self.assertEqual(loc["evidence_role"], "projected_counterpart")
        self.assertEqual(loc["from_side"], "old")
        self.assertEqual(loc["source_location_index"], 0)
        self.assertEqual(self.old, old)
        self.assertEqual(self.new["locations"], [])
        self.assertEqual(self.metadata, metadata)
        self.assertEqual(self.new["counterpart_source"]["kind"], "registered_residual_projection")

    def test_inverse_scale_and_translation_project_new_only_evidence(self):
        self.old["locations"] = []
        self.new["locations"] = [_location((78, 41, 102, 53), 1, self.new_size)]
        _link_counterparts(self.old, self.new, self.metadata)
        loc, = self.old["counterpart_locations"]
        self.assert_box(loc, self.box, self.old_size)
        self.assertEqual(loc["from_side"], "new")
        self.assertEqual(self.old["locations"], [])

    def test_translation_without_scale_and_rotated_page_dimensions(self):
        self.metadata["alignment"].pop("scale_ratio")
        _link_counterparts(self.old, self.new, self.metadata)
        self.assert_box(self.new["counterpart_locations"][0], (72, 39, 92, 49), self.new_size)
        for side, size in ((self.old, (100, 200)), (self.new, (180, 300))):
            side["source"]["page_size_pt"] = list(size)
        self.old["locations"] = [_location(self.box, 1, (100, 200))]
        _link_counterparts(self.old, self.new, self.metadata)
        self.assert_box(self.new["counterpart_locations"][0], (72, 39, 92, 49), (180, 300))

    def test_unaccepted_unpaired_or_two_sided_evidence_is_not_projected(self):
        old, new = deepcopy(self.old), deepcopy(self.new)
        self.metadata["alignment"]["accepted"] = False
        _link_counterparts(old, new, self.metadata)
        self.assertEqual(new, self.new)
        self.metadata["alignment"]["accepted"] = True
        _link_counterparts(None, new, self.metadata)
        self.assertEqual(new, self.new)
        new["locations"] = [_location((78, 41, 102, 53), 1, self.new_size)]
        original = deepcopy(new)
        _link_counterparts(old, new, self.metadata)
        self.assertEqual(new, original)
        self.assertEqual(old, self.old)

    def test_missing_origin_and_outside_page_are_explicit_not_invented(self):
        self.new["source"]["crop_origin_pixels"] = None
        _link_counterparts(self.old, self.new, self.metadata)
        self.assertIn("裁切原点", self.new["counterpart_location_error"])
        self.assertNotIn("counterpart_locations", self.new)
        self.new["source"]["crop_origin_pixels"] = (80, 60)
        self.metadata["alignment"]["dx_pixels"] = -1000
        _link_counterparts(self.old, self.new, self.metadata)
        self.assertEqual(self.new["counterpart_locations"], [])
        self.assertIn("页面外", self.new["counterpart_location_error"])

    def test_partial_page_intersection_is_clipped(self):
        self.metadata["alignment"]["dx_pixels"] = -170
        _link_counterparts(self.old, self.new, self.metadata)
        loc, = self.new["counterpart_locations"]
        self.assert_box(loc, (0, 41, 15, 53), self.new_size)

    def test_invalid_accepted_registration_fails_explicitly(self):
        for factor in (0, float("nan"), -1):
            with self.subTest(scale=factor):
                self.metadata["alignment"]["scale_ratio"] = factor
                with self.assertRaises(GraphicsError):
                    _link_counterparts(self.old, self.new, self.metadata)


if __name__ == "__main__":
    unittest.main()
