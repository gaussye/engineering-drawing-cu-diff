import copy
import json
from pathlib import Path
import shutil
import threading
import unittest
from unittest.mock import patch
import uuid

import pymupdf

from cu_diff.client import CUError, digest
from cu_diff.model_compare import options
from cu_diff.model_presence import review_presence, SCHEMA


def box(x0=180, y0=180, x1=420, y1=420):
    return dict(x0=x0, y0=y0, x1=x1, y1=y1)


class PresenceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / (".presence-tests-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.paths = {side: self.root / f"{side}.pdf" for side in ("old", "new")}
        self.make_pdf("new", [(1, (100, 100, 200, 200))])
        self.make_pdf("old", [(2, (300, 300, 380, 380))])
        self.selected = {"old": [], "new": [self.entry("new")]}
        self.pair = {"label": "合成图形", "old_ids": [], "new_ids": ["new:e1"],
                     "assessment": "uncertain", "priority": 1, "rationale": "需搜索对应对象",
                     "observations": [{"kind": "unresolved", "description": "单侧候选",
                                       "old_ids": [], "new_ids": ["new:e1"], "check": "全页核对"}]}
        self.client = type("Client", (), {})()
        self.client.config = {"completion_model": "synthetic",
                              "model_deployments": {"synthetic": "synthetic-deployment"}}
        self.client.usage_context = {"stage": "model_visual_presence", "region_index": 4}
        self.opts = options({})
        self.opts["crop_dpi"] = 200
        self.result = {"present_status": "located", "target_box": [box()],
                       "counterpart_status": "not_found", "counterpart_boxes": [],
                       "searched_pages": [1, 2], "rationale": "已定位可见图形，所提供对侧页面中未找到可靠对应。",
                       "limitations": ["仅模型观察，尚需人工复核。"]}
        self.meta = {"cache_hit": True, "cache_key": "synthetic", "usage": {"total_tokens": 12}}
        self.completion = patch("cu_diff.model_presence.complete_json",
                                side_effect=lambda *a, **k: (copy.deepcopy(self.result), self.meta))
        self.chat = self.completion.start()
        self.addCleanup(self.completion.stop)

    def entry(self, side, page=1):
        return {"id": f"{side}:e1", "role": "OCR line", "raw_text": "图形附近的尺寸",
                "confidence": .9, "source": "synthetic CU anchor",
                "locations": [{"page": page, "x": .45, "y": .18, "width": .12, "height": .03}]}

    def make_pdf(self, side, objects, count=3):
        with pymupdf.open() as pdf:
            for number in range(1, count + 1):
                page = pdf.new_page(width=500, height=500)
                for page_number, rect in objects:
                    if page_number == number:
                        page.draw_rect(rect, color=(0, 0, 0), fill=(0, 0, 0))
            pdf.save(self.paths[side])

    def run_review(self, **kwargs):
        return review_presence(
            self.pair, self.selected, self.paths, client=self.client,
            cache=self.root / "cache", opts=self.opts,
            catalogs={side: {e["id"]: e for e in self.selected[side]} for side in ("old", "new")},
            **kwargs)

    def test_new_only_partial_coverage_and_yellow_object_not_cu_anchor(self):
        before = {side: digest(path.read_bytes()) for side, path in self.paths.items()}
        record, coverage = self.run_review(pdf_lock=threading.Lock())
        self.assertEqual(self.chat.call_count, 1)
        self.assertEqual(record["change"], "model_visual_presence_review")
        self.assertEqual(record["match"], {"method": "model_single_sided_search",
                                          "certainty": "model_proposed", "score": None})
        loc, = record["new"]["locations"]
        self.assertAlmostEqual(loc["x"], .2, delta=.002)
        self.assertAlmostEqual(loc["width"], .2, delta=.003)
        self.assertEqual(loc["highlight_color"], "yellow")
        self.assertTrue(loc["review_only"])
        self.assertEqual(loc["evidence_role"], "model_proposal_ink_extent")
        self.assertNotEqual(loc, record["new"]["context_locations"][0])
        self.assertEqual(record["old"]["locations"], [])
        self.assertIsNone(record["model_context"]["old"])
        self.assertEqual(coverage["search_coverage"],
                         {"searched_pages": [1, 2], "total_pages": 3, "complete": False})
        self.assertEqual(record["visual_comparison"]["changed_pixels"], {"old": None, "new": None})
        self.assertEqual(record["visual_comparison"]["measurement_status"], "object_extent_only")
        self.assertNotIn("visual_grounded", json.dumps(record))
        self.assertTrue(any("未全部提供" in value for value in coverage["limitations"]))
        self.assertTrue(any("不能证明不存在" in value for value in coverage["limitations"]))
        self.assertEqual(before, {side: digest(path.read_bytes()) for side, path in self.paths.items()})
        self.assertEqual(record["new"]["source"]["locations"], record["new"]["locations"])

    def test_old_only_with_present_page_beyond_opposite_budget(self):
        self.selected = {"old": [self.entry("old", page=2)], "new": []}
        self.opts["max_pages_per_side"] = 1
        self.result["searched_pages"] = [1]
        self.result["target_box"] = [box(580, 580, 820, 820)]
        record, coverage = self.run_review()
        self.assertEqual(record["visual_comparison"]["presence_side"], "old")
        self.assertEqual(record["old"]["locations"][0]["page"], 2)
        self.assertEqual(record["new"]["locations"], [])
        self.assertEqual(coverage["status"], "presence_review")

    def test_relocated_rescaled_counterpart_on_other_page(self):
        self.result.update(counterpart_status="found",
                           counterpart_boxes=[{"page": 2, "bbox": box(580, 580, 820, 820)}],
                           rationale="候选在对侧第二页的不同位置可见；对应关系仍待复核。")
        record, _ = self.run_review()
        self.assertEqual(record["old"]["locations"][0]["page"], 2)
        self.assertAlmostEqual(record["old"]["locations"][0]["x"], .6, delta=.002)
        self.assertAlmostEqual(record["old"]["locations"][0]["width"], .16, delta=.003)
        self.assertEqual(record["new"]["locations"][0]["page"], 1)
        for side in ("old", "new"):
            self.assertFalse(record[side]["source"]["local_ink_validation"][0]["object_identity_verified"])
            self.assertEqual(record[side]["locations"][0]["highlight_color"], "yellow")
        self.assertEqual(record["visual_comparison"]["description"], self.result["rationale"])

    def test_white_present_proposal_has_no_phantom_frame(self):
        self.result["target_box"] = [box(0, 0, 100, 100)]
        record, coverage = self.run_review()
        self.assertEqual(record["new"]["locations"], [])
        self.assertIn("未检测到可见墨迹", record["new"]["location_error"])
        self.assertEqual(coverage["measurement_status"], "unresolved")

    def test_white_counterpart_has_no_phantom_frame(self):
        self.result.update(counterpart_status="found",
                           counterpart_boxes=[{"page": 1, "bbox": box()}])
        record, coverage = self.run_review()
        self.assertEqual(record["old"]["locations"], [])
        self.assertEqual(coverage["measurement_status"], "unresolved")
        self.assertEqual(record["visual_comparison"]["counterpart_status"], "found")
        self.assertIn("未检测到可见墨迹", record["old"]["location_error"])

    def test_uncertain_has_no_frames(self):
        self.result.update(present_status="uncertain", target_box=[], counterpart_status="uncertain")
        record, coverage = self.run_review()
        self.assertTrue(all(not record[side]["locations"] for side in ("old", "new")))
        self.assertEqual(coverage["measurement_status"], "unresolved")

    def test_complete_page_coverage_still_never_confirms_absence(self):
        self.opts["max_pages_per_side"] = 4
        self.result["searched_pages"] = [1, 2, 3]
        record, coverage = self.run_review()
        self.assertTrue(coverage["search_coverage"]["complete"])
        self.assertTrue(record["review_required"])
        self.assertTrue(any("不能证明不存在" in v for v in coverage["limitations"]))
        self.assertEqual(record["old"]["locations"], [])

    def test_payload_cache_hooks_and_deterministic_provenance(self):
        first, coverage = self.run_review()
        args, kwargs = self.chat.call_args
        self.assertIs(args[0], self.client)
        self.assertEqual(args[1], self.root / "cache")
        self.assertFalse(kwargs["allow_submit"])
        body = args[2]
        self.assertEqual(body["model"], "synthetic-deployment")
        self.assertEqual(body["response_format"]["json_schema"]["schema"], SCHEMA)
        payload = json.loads(body["messages"][1]["content"][0]["text"])
        self.assertEqual(payload["documents"]["new"]["source_sha256"],
                         digest(self.paths["new"].read_bytes()))
        self.assertEqual([i["page"] for i in payload["images"]], [1, 1, 1, 2])
        self.assertEqual(len([v for v in body["messages"][1]["content"]
                              if v["type"] == "image_url"]), 4)
        self.assertEqual(payload["model_context"]["new"]["source"], ["synthetic CU anchor"])
        self.assertEqual(coverage["model"], self.meta)
        second, _ = self.run_review(allow_submit=True)
        self.assertTrue(self.chat.call_args.kwargs["allow_submit"])
        self.assertEqual(first, second)
        self.assertEqual(body, self.chat.call_args.args[2])
        self.assertEqual(self.client.usage_context, {"stage": "model_visual_presence", "region_index": 4})

    def test_invalid_claims_raise_cuerror(self):
        valid = copy.deepcopy(self.result)
        mutations = [
            {"present_status": "missing"}, {"present_status": True},
            {"target_box": []}, {"target_box": [box(), box()]}, {"target_box": None},
            {"target_box": [box(x0=True)]}, {"target_box": [box(x1=420.0)]},
            {"target_box": [box(x0=-1)]}, {"target_box": [box(x1=1001)]},
            {"target_box": [box(x1=180)]}, {"target_box": [box(y0=500)]},
            {"target_box": [{"x0": 1, "y0": 2, "x1": 3}]},
            {"counterpart_status": "absent"}, {"counterpart_status": "found"},
            {"counterpart_boxes": [{"page": 1, "bbox": box()}]},
            {"counterpart_status": "found", "counterpart_boxes": [{"page": 3, "bbox": box()}]},
            {"counterpart_status": "found", "counterpart_boxes": [{"page": True, "bbox": box()}]},
            {"counterpart_status": "found", "counterpart_boxes": [{"page": 0, "bbox": box()}]},
            {"counterpart_status": "found", "counterpart_boxes": [{"page": "2", "bbox": box()}]},
            {"counterpart_status": "found", "counterpart_boxes": [{"page": 2, "bbox": box(x1=0)}]},
            {"counterpart_status": "found", "counterpart_boxes": [{"page": 2, "bbox": box()}]*4},
            {"counterpart_status": "found", "counterpart_boxes": [{"page": 2, "bbox": box()}]*2},
            {"searched_pages": [1]}, {"searched_pages": [2, 1]}, {"searched_pages": [1, 2, 3]},
            {"searched_pages": [True, 2]}, {"searched_pages": [1, 2, 2]},
            {"rationale": ""}, {"rationale": None}, {"limitations": [1]}, {"extra": "no"},
            {"present_status": "uncertain", "target_box": []},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.result = dict(valid, **mutation)
                with self.assertRaises(CUError):
                    self.run_review()
        self.result = {key: value for key, value in valid.items() if key != "limitations"}
        with self.assertRaises(CUError):
            self.run_review()

    def test_invalid_inputs_fail_before_completion(self):
        original = copy.deepcopy(self.selected)
        for selected in ({"old": [], "new": []},
                         {"old": [self.entry("old")], "new": [self.entry("new")]},
                         {"old": [], "new": [self.entry("new", 99)]},
                         {"old": [], "new": [self.entry("new", True)]},
                         {"old": [], "new": [self.entry("new", 1), self.entry("new", 2)]}):
            with self.subTest(selected=selected):
                self.selected = selected
                with self.assertRaises(CUError):
                    self.run_review()
        self.selected = original
        self.selected["new"][0]["locations"][0]["x"] = float("nan")
        with self.assertRaises(CUError):
            self.run_review()
        self.assertEqual(self.chat.call_count, 0)

    def test_cache_miss_is_not_swallowed_or_retried(self):
        self.chat.side_effect = CUError("cache-only miss")
        with self.assertRaisesRegex(CUError, "cache-only miss"):
            self.run_review()
        self.assertEqual(self.chat.call_count, 1)


if __name__ == "__main__":
    unittest.main()
