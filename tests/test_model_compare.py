import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

from cu_diff.client import CUError, digest
from cu_diff.model_compare import compare_with_model, options, _record, _focus_for_review, _visual_placeholder


def response(width=10, height=5, *, words=None, low=False):
    page = {"pageNumber": 1, "width": width, "height": height, "unit": "inch"}
    if words is None:
        page["lines"] = [
            {"content": "SYNTHETIC SPEC A", "source": "D(1,1,1,2,.3)"},
            {"content": "SYNTHETIC SPEC B", "source": "D(1,5,3,2,.3)"},
        ]
    else:
        page["words"] = [
            {"content": text, "confidence": .5 if low else .95,
             "source": f"D(1,{width*(.1+i*.2)},{height*.3},{width*.12},{height*.3})"}
            for i, text in enumerate(words)
        ]
    return {"status": "Succeeded", "result": {"contents": [{"unit": "inch", "pages": [page]}]}}


def pair(index=1, **extra):
    return {"label": "Synthetic specification", "old_ids": [f"old:e{index}"],
            "new_ids": [f"new:e{index}"], "assessment": "changed", "priority": 1,
            "rationale": "Model proposes this same-role pairing.", "observations": [], **extra}


def many_lines(count):
    document = response()
    document["result"]["contents"][0]["pages"][0]["lines"] = [
        {"content": f"SYNTHETIC ROW {index}", "source": "D(1,1,1,2,.3)"}
        for index in range(1, count + 1)]
    return document


class FakeClient:
    def __init__(self):
        self.config = {"completion_model": "test-model", "model_deployments": {"test-model": "test-deploy"},
                       "model_comparison": {"enabled": True, "max_regions": 1}}
        self.calls = []
        self.low = False
        self.old_words, self.new_words = ["SYN", "3A"], ["SYN", "8A"]

    def analyze(self, path, cache, identifier, analyzer, *, allow_submit):
        side = "old" if len(self.calls) % 2 == 0 else "new"
        self.calls.append((path, allow_submit))
        with pymupdf.open(path) as pdf:
            rect = pdf[0].rect
        words = self.old_words if side == "old" else self.new_words
        return response(rect.width/72, rect.height/72, words=words, low=self.low), {
            "cache_hit": True, "usage": {"pages": 1},
        }


class ModelComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = []
        for role in ("old", "new"):
            path = self.root / f"{role}.pdf"
            with pymupdf.open() as pdf:
                page = pdf.new_page(width=720, height=360)
                page.insert_text((72, 85), f"SYNTHETIC {role}")
                pdf.save(path)
            self.paths.append(path)
        self.client = FakeClient()
        self.coarse = {"pairs": [pair()], "limitations": []}
        self.fine = {"assessment": "changes", "limitations": [], "changes": [{
            "label": "Synthetic rating", "old_ids": ["old:w1", "old:w2"],
            "new_ids": ["new:w1", "new:w2"], "rationale": "Same role, different rating.",
        }]}
        self.visual = {"views": [], "limitations": ["No synthetic graphical change confirmed."]}
        self.patch = patch("cu_diff.model_compare.complete_json", side_effect=self.completion)
        self.chat = self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def completion(self, client, cache, body, **kwargs):
        if "NON-TEXT" in body["messages"][0]["content"]:
            return copy.deepcopy(self.visual), {"cache_hit": True, "usage": {"total_tokens": 15}}
        fine = "WORD IDs" in body["messages"][0]["content"]
        self.assertTrue(body["messages"][1]["content"][1]["type"], "text")
        return copy.deepcopy(self.fine if fine else self.coarse), {"cache_hit": True, "usage": {"total_tokens": 20}}

    def run_comparison(self, old=None, new=None, **kwargs):
        return compare_with_model(*self.paths, old or response(), new or response(),
                                  client=self.client, cache=self.root / "cache",
                                  analyzer_id="synthetic", analyzer={}, **kwargs)

    def test_model_pairing_then_crop_words_and_original_coordinates(self):
        before = [digest(path.read_bytes()) for path in self.paths]
        stages = []
        result = self.run_comparison(stage_progress=stages.append)
        self.assertEqual(stages, ["model_coarse", "cu_crop_preparation", "cu_crop_pair",
                                  "model_fine"])
        self.assertEqual(len(self.client.calls), 2)
        self.assertTrue(all(not submit for _, submit in self.client.calls))
        self.assertEqual(self.chat.call_count, 2)
        record, = result["items"]
        self.assertEqual(record["change"], "model_text_modified")
        self.assertEqual(record["old"]["raw_text"], "SYN 3A")
        self.assertEqual(record["new"]["raw_text"], "SYN 8A")
        for side in ("old", "new"):
            self.assertEqual(len(record[side]["locations"]), 1)
            self.assertEqual(len(record[side]["context_locations"]), 2)
            box = record[side]["locations"][0]
            mapping = record[side]["source"][1]["crop_mapping"]
            self.assertAlmostEqual(box["x"], (mapping["rect"][0]+.3*mapping["width"])/720, places=6)
            self.assertAlmostEqual(box["y"], (mapping["rect"][1]+.3*mapping["height"])/360, places=6)
            self.assertEqual(box["page"], 1)
        self.assertEqual(result["coverage"]["catalog"]["unreferenced_ids"]["old"], ["old:e2"])
        self.assertEqual(before, [digest(path.read_bytes()) for path in self.paths])

    def test_invalid_concurrency_rejected_before_any_model_or_cu_request(self):
        self.client.config["performance"] = {"cu_workers": 3}
        with self.assertRaises(ValueError):
            self.run_comparison()
        self.chat.assert_not_called()
        self.assertEqual(self.client.calls, [])

    def test_layout_request_schema_constrains_each_side_without_mutating_legacy_schema(self):
        from cu_diff.model_compare import COARSE_SCHEMA, FINE_SCHEMA
        originals = copy.deepcopy((COARSE_SCHEMA, FINE_SCHEMA))
        self.client.config["extraction_profile"] = "layout"
        self.run_comparison()
        coarse, fine = [call.args[2]["response_format"]["json_schema"]["schema"]
                        for call in self.chat.call_args_list]
        pair_properties = coarse["properties"]["pairs"]["items"]["properties"]
        observation = pair_properties["observations"]["items"]["properties"]
        fine_properties = fine["properties"]["changes"]["items"]["properties"]
        for side in ("old", "new"):
            for fields in (pair_properties, observation):
                self.assertEqual(fields[f"{side}_ids"]["items"]["enum"], [f"{side}:e1", f"{side}:e2"])
            self.assertEqual(fine_properties[f"{side}_ids"]["items"]["enum"], [f"{side}:w1", f"{side}:w2"])
        self.assertEqual((COARSE_SCHEMA, FINE_SCHEMA), originals)

    def test_layout_still_rejects_wrong_side_response_without_crops(self):
        self.client.config["extraction_profile"] = "layout"
        self.coarse["pairs"][0]["new_ids"] = ["old:e1"]
        with self.assertRaisesRegex(CUError, "unknown or wrong-side"):
            self.run_comparison()
        self.assertEqual(self.client.calls, [])

    def test_layout_out_of_pair_known_observation_is_explicit_review_without_inference_or_frames(self):
        self.client.config.update(extraction_profile="layout")
        self.client.config["model_comparison"]["visual_review"] = True
        self.coarse["pairs"][0]["observations"] = [
            {"kind": "visual_change", "old_ids": ["old:e2"], "new_ids": ["new:e2"],
             "description": "Synthetic out-of-group observation", "check": "Review sources"}]
        result = self.run_comparison()
        record, = result["items"]
        self.assertEqual(record["change"], "model_review")
        self.assertEqual(record["model_comparison"]["highlight_scope"], "none_invalid_observation_group")
        self.assertEqual(record["model_comparison"]["rejected_source_ids"],
                         {"old": ["old:e2"], "new": ["new:e2"]})
        self.assertTrue(result["warnings"])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.chat.call_count, 1)
        for side in ("old", "new"):
            self.assertEqual(record[side]["locations"], [])
            self.assertTrue(record["model_context"][side]["locations"])
        self.assertEqual(result["coverage"]["visual"]["regions"], [])

    def test_layout_unknown_observation_still_fails_atomically(self):
        self.client.config["extraction_profile"] = "layout"
        self.coarse["pairs"].append(pair(2, observations=[
            {"kind": "text_change", "old_ids": ["new:e1"], "new_ids": ["new:e2"],
             "description": "Invalid cross-side reference", "check": "Review sources"}]))
        with self.assertRaisesRegex(CUError, "unknown or wrong-side"):
            self.run_comparison()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.chat.call_count, 1)

    def test_layout_figure_as_text_is_explicit_unboxed_review_and_valid_pairs_continue(self):
        self.client.config["extraction_profile"] = "layout"
        self.client.config["model_comparison"]["visual_review"] = True
        document = response()
        document["result"]["contents"][0]["figures"] = [{
            "description": "SYNTHETIC FIGURE CONTEXT", "source": "D(1,1,1,2,.3)"}]
        self.coarse["pairs"][0]["observations"] = [
            {"kind": "text_change", "old_ids": ["old:e1"], "new_ids": ["new:e1"],
             "description": "Synthetic invalid figure-as-text", "check": "Check OCR"},
            {"kind": "visual_change", "old_ids": ["old:e1"], "new_ids": ["new:e1"],
             "description": "Synthetic visual observation", "check": "Check geometry"}]
        self.coarse["pairs"].append(pair(2))
        result = self.run_comparison(old=document, new=document)
        review = result["items"][0]
        self.assertEqual(review["change"], "model_review")
        self.assertEqual(review["model_comparison"]["invalid_text_source_ids"],
                         {"old": ["old:e1"], "new": ["new:e1"]})
        self.assertEqual(review["old"]["locations"], [])
        self.assertEqual(review["new"]["locations"], [])
        self.assertTrue(any("图形上下文" in warning for warning in result["warnings"]))
        self.assertEqual(result["items"][1]["change"], "model_text_modified")
        self.assertEqual(result["coverage"]["visual"]["regions"], [])
        self.assertEqual(len(self.client.calls), 2)

    def test_layout_figure_text_review_still_validates_all_observation_ids(self):
        self.client.config["extraction_profile"] = "layout"
        document = response()
        document["result"]["contents"][0]["figures"] = [{
            "description": "SYNTHETIC FIGURE CONTEXT", "source": "D(1,1,1,2,.3)"}]
        self.coarse["pairs"][0]["observations"] = [
            {"kind": "text_change", "old_ids": ["old:e1"], "new_ids": ["new:e1"],
             "description": "Invalid text evidence", "check": "Check OCR"},
            {"kind": "visual_change", "old_ids": ["old:invented"], "new_ids": [],
             "description": "Unknown evidence", "check": "Verify"}]
        with self.assertRaisesRegex(CUError, "unknown or wrong-side"):
            self.run_comparison(old=document, new=document)
        self.assertEqual(self.client.calls, [])

    def test_insertion_does_not_color_unchanged_anchor_as_difference(self):
        self.client.old_words, self.client.new_words = ["SYN", "END"], ["SYN", "NEW", "END"]
        self.fine["changes"][0]["new_ids"].append("new:w3")
        record, = self.run_comparison()["items"]
        self.assertEqual(record["old"]["locations"], [])
        self.assertEqual(len(record["new"]["locations"]), 1)
        self.assertTrue(record["model_context"]["old"]["locations"])
        self.assertEqual(record["old"]["raw_text"], "SYN END")

    def test_low_confidence_is_review_not_red_candidate(self):
        self.client.low = True
        record, = self.run_comparison()["items"]
        self.assertEqual(record["change"], "model_review")
        self.assertEqual(record["model_comparison"]["status"], "review_only")

    def test_uncertain_assessment_with_referenced_hypotheses_stays_review_only(self):
        self.fine["assessment"] = "uncertain"
        for absent in (False, True):
            with self.subTest(missing_counterpart=absent):
                if absent:
                    self.fine["changes"][0]["new_ids"] = []
                item, = self.run_comparison()["items"]
                self.assertEqual(item["change"], "model_review")
                self.assertEqual(item["model_comparison"]["status"], "review_only")
                self.assertEqual(item["model_comparison"]["fine_assessment"], "uncertain")
                self.assertTrue(any("整体判断仍不确定" in reason for reason in item["review_reasons"]))
                if absent:
                    self.assertIsNone(item["new"])

    def test_truly_contradictory_fine_assessments_still_fail(self):
        self.fine["assessment"] = "unchanged"
        with self.assertRaisesRegex(CUError, "inconsistent"):
            self.run_comparison()
        self.fine.update(assessment="changes", changes=[])
        with self.assertRaisesRegex(CUError, "inconsistent"):
            self.run_comparison()

    def test_budget_keeps_unprocessed_candidates_visible(self):
        self.coarse["pairs"].append(pair(2))
        result = self.run_comparison()
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(result["coverage"]["unprocessed"], 1)
        self.assertEqual(result["items"][1]["change"], "model_review")
        self.assertIn("预算", result["items"][1]["review_reasons"][0])

    def test_missing_counterpart_never_creates_coordinates(self):
        self.coarse["pairs"][0]["new_ids"] = []
        result = self.run_comparison()
        record, = result["items"]
        self.assertIsNone(record["new"])
        self.assertEqual(record["change"], "model_review")
        self.assertEqual(self.client.calls, [])

    def test_unknown_and_cross_side_ids_still_fail_even_when_repeated(self):
        for ids in (["old:invented"], ["new:e1"], ["old:invented"] * 2, ["new:e1"] * 2):
            self.coarse["pairs"][0]["old_ids"] = ids
            with self.assertRaises(CUError):
                self.run_comparison()
        self.assertEqual(self.client.calls, [])

    def test_repeated_coarse_ids_are_stably_deduplicated_with_original_audit(self):
        for profile in ("engineering", "layout"):
            with self.subTest(profile=profile):
                self.client.config["extraction_profile"] = profile
                self.coarse["pairs"][0]["old_ids"] = ["old:e1", "old:e1"]
                self.coarse["pairs"][0]["observations"] = [{
                    "kind": "text_change", "old_ids": ["old:e1"] * 2,
                    "new_ids": ["new:e1"] * 2, "description": "Synthetic change", "check": "Verify"}]
                before = copy.deepcopy(self.coarse)
                result = self.run_comparison()
                self.assertEqual(result["items"][0]["change"], "model_text_modified")
                audit = result["coverage"]["coarse"]
                self.assertEqual(audit["proposed_pairs"], before["pairs"])
                self.assertEqual(len(audit["reference_adjustments"]), 3)
                self.assertEqual(audit["reference_adjustments"][0], {
                    "pair_index": 0, "observation_index": None, "side": "old",
                    "original_ids": ["old:e1", "old:e1"], "normalized_ids": ["old:e1"],
                    "removed_count": 1})
                self.assertTrue(any("去重" in warning for warning in result["warnings"]))
                self.assertEqual(self.coarse, before)

    def test_coarse_dedup_keeps_first_occurrence_order(self):
        from cu_diff.model_compare import _normalize_coarse_references
        self.coarse["pairs"][0]["old_ids"] = ["old:e2", "old:e1", "old:e2", "old:e1"]
        normalized, adjustments = _normalize_coarse_references(self.coarse)
        self.assertEqual(normalized["pairs"][0]["old_ids"], ["old:e2", "old:e1"])
        self.assertEqual(adjustments[0]["removed_count"], 2)

    def test_raw_reference_budget_cannot_be_bypassed_by_dedup(self):
        self.coarse["pairs"][0]["old_ids"] = ["old:e1"] * 41
        result = self.run_comparison()
        self.assertEqual(self.client.calls, [])
        item, = result["items"]
        self.assertEqual(item["change"], "model_review")
        self.assertEqual(item["old"]["locations"], [])
        self.assertEqual(item["model_comparison"]["budget_issues"][0]["actual"], 41)
        self.assertEqual(result["coverage"]["coarse"]["proposed_pairs"][0]["old_ids"], ["old:e1"] * 41)

    def test_oversized_unchanged_table_is_retained_and_other_regions_continue(self):
        for profile in ("engineering", "layout"):
            with self.subTest(profile=profile):
                self.client.config["extraction_profile"] = profile
                oversized = pair(assessment="unchanged",
                                 old_ids=[f"old:e{i}" for i in range(1, 52)],
                                 new_ids=[f"new:e{i}" for i in range(1, 52)])
                oversized["observations"] = [{
                    "kind": "unchanged_text", "old_ids": oversized["old_ids"][:],
                    "new_ids": oversized["new_ids"][:], "description": "Synthetic table",
                    "check": "Check every row"}]
                self.coarse["pairs"] = [oversized, pair(52)]
                before = copy.deepcopy(self.coarse)
                result = self.run_comparison(old=many_lines(52), new=many_lines(52))
                review, confirmed = result["items"]
                self.assertEqual(review["change"], "model_review")
                self.assertEqual(confirmed["change"], "model_text_modified")
                for side in ("old", "new"):
                    self.assertEqual(review[side]["locations"], [])
                    self.assertEqual(len(review["model_context"][side]["locations"]), 51)
                    self.assertIn("SYNTHETIC ROW 51", review[side]["raw_text"])
                self.assertEqual(len(review["model_comparison"]["budget_issues"]), 4)
                self.assertEqual(result["coverage"]["coarse"]["proposed_pairs"], before["pairs"])
                self.assertEqual(list(result["coverage"]["coarse"]["budget_exceeded_pairs"]), ["0"])
                self.assertEqual(result["coverage"]["reread_regions"], 1)
                self.assertTrue(any("不截断来源" in warning for warning in result["warnings"]))
                self.assertEqual(self.coarse, before)

    def test_observation_reference_and_observation_count_budgets_are_review_only(self):
        for kind in ("reference", "observation"):
            with self.subTest(kind=kind):
                observation = {"kind": "visual_change", "old_ids": ["old:e1"],
                               "new_ids": ["new:e1"], "description": "Synthetic", "check": "Verify"}
                self.coarse["pairs"][0]["observations"] = (
                    [dict(observation, old_ids=["old:e1"] * 41)] if kind == "reference"
                    else [copy.deepcopy(observation) for _ in range(21)])
                self.client.config["model_comparison"]["visual_review"] = True
                result = self.run_comparison()
                self.assertEqual(self.client.calls, [])
                self.assertEqual(result["coverage"]["visual"]["regions"], [])
                item, = result["items"]
                self.assertEqual(item["old"]["locations"], [])
                self.assertEqual(item["change"], "model_review")
                self.assertTrue(item["model_comparison"]["budget_issues"])

    def test_region_count_budget_retains_additional_pairs_without_confirmation(self):
        self.coarse["pairs"] = [pair(i, assessment="unchanged") for i in range(1, 42)]
        result = self.run_comparison(old=many_lines(41), new=many_lines(41))
        self.assertEqual(self.client.calls, [])
        item, = result["items"]
        self.assertEqual(item["model_comparison"]["budget_issues"],
                         [{"kind": "region_count", "actual": 41, "limit": 40}])
        self.assertEqual(item["old"]["locations"], [])
        self.assertEqual(len(result["coverage"]["coarse"]["proposed_pairs"]), 41)

    def test_unknown_or_wrong_side_ids_in_budget_exceeding_tails_fail_before_inference(self):
        for position in ("pair", "observation", "region"):
            for invalid in ("old:invented", "new:e1"):
                with self.subTest(position=position, invalid=invalid):
                    self.coarse["pairs"] = [pair()]
                    if position == "pair":
                        self.coarse["pairs"][0]["old_ids"] = ["old:e1"] * 40 + [invalid]
                    elif position == "observation":
                        self.coarse["pairs"][0]["observations"] = [
                            {"kind": "visual_change", "old_ids": ["old:e1"], "new_ids": [],
                             "description": "Synthetic", "check": "Verify"} for _ in range(21)]
                        self.coarse["pairs"][0]["observations"][-1]["old_ids"] = [invalid]
                    else:
                        self.coarse["pairs"] = [pair() for _ in range(41)]
                        self.coarse["pairs"][-1]["old_ids"] = [invalid]
                    with self.assertRaisesRegex(CUError, "unknown or wrong-side"):
                        self.run_comparison()
                    self.assertEqual(self.client.calls, [])

    def test_budget_review_does_not_erase_shared_source_conflicts(self):
        self.coarse["pairs"] = [pair(old_ids=["old:e1"] * 41), pair()]
        result = self.run_comparison()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(result["coverage"]["coarse"]["conflicting_source_ids"])
        self.assertTrue(all(item["old"]["locations"] == [] for item in result["items"]))

    def test_hard_response_array_bound_still_rejects_excessive_payloads(self):
        self.coarse["pairs"][0]["old_ids"] = ["old:e1"] * 2001
        with self.assertRaisesRegex(CUError, "oversized array"):
            self.run_comparison()
        self.assertEqual(self.client.calls, [])

    def test_duplicate_observation_does_not_bypass_parent_group_validation(self):
        self.client.config["extraction_profile"] = "layout"
        self.coarse["pairs"][0]["observations"] = [{
            "kind": "visual_change", "old_ids": ["old:e2"] * 2, "new_ids": [],
            "description": "Synthetic out-of-group observation", "check": "Verify"}]
        result = self.run_comparison()
        self.assertEqual(self.client.calls, [])
        item, = result["items"]
        self.assertEqual(item["model_comparison"]["highlight_scope"], "none_invalid_observation_group")
        self.assertEqual(item["old"]["locations"], [])
        self.assertEqual(item["new"]["locations"], [])

    def test_dedup_within_pair_does_not_hide_conflicts_between_pairs(self):
        self.coarse["pairs"][0]["old_ids"] *= 2
        self.coarse["pairs"].append(pair())
        result = self.run_comparison()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(result["coverage"]["coarse"]["conflicting_source_ids"])
        self.assertTrue(all(item["old"]["locations"] == [] for item in result["items"]))

    def test_fine_word_duplicates_are_not_normalized(self):
        self.fine["changes"][0]["old_ids"] = ["old:w1", "old:w1", "old:w2"]
        with self.assertRaisesRegex(CUError, "repeated"):
            self.run_comparison()

    def test_reused_region_evidence_is_explicitly_ambiguous_not_sent_for_confirmation(self):
        self.coarse["pairs"].append(pair())
        result = self.run_comparison()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(all(item["change"] == "model_review" for item in result["items"]))
        self.assertTrue(result["coverage"]["coarse"]["conflicting_source_ids"])
        self.assertTrue(any("配对冲突" in warning for warning in result["warnings"]))

    def test_word_order_and_fabricated_fields_are_rejected(self):
        self.fine["changes"][0]["old_ids"].reverse()
        with self.assertRaisesRegex(CUError, "reading order"):
            self.run_comparison()
        self.fine["changes"][0]["old_ids"].reverse()
        self.fine["changes"][0]["new_text"] = "MODEL INVENTED CORRECTION"
        with self.assertRaisesRegex(CUError, "properties"):
            self.run_comparison()

    def test_geometry_mismatch_fails_before_model_request(self):
        with self.assertRaisesRegex(CUError, "geometry"):
            self.run_comparison(old=response(width=12))
        self.chat.assert_not_called()

    def test_identical_word_values_do_not_become_modified_due_to_model_rationale(self):
        self.client.new_words = self.client.old_words
        result = self.run_comparison()
        self.assertEqual(result["items"], [])
        self.assertTrue(any("相同CU文字" in warning for warning in result["warnings"]))

    def test_unchanged_model_assessment_is_not_full_coverage(self):
        self.coarse["pairs"][0]["assessment"] = "unchanged"
        result = self.run_comparison()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["coverage"]["coarse"]["unchanged_model_assessments"], 1)
        self.assertEqual(result["coverage"]["status"], "completed_with_limits")
        self.assertTrue(result["coverage"]["catalog"]["unreferenced_ids"]["new"])

    def test_fine_unchanged_is_not_a_default_review_with_stale_coarse_boxes(self):
        self.fine = {"assessment": "unchanged", "changes": [], "limitations": ["Some context is outside the crop."]}
        result = self.run_comparison()
        item, = result["items"]
        self.assertEqual(item["change"], "model_no_text_change")
        self.assertEqual(item["old"]["raw_text"], "SYN 3A")
        self.assertEqual(item["old"]["locations"], [])
        self.assertEqual(item["new"]["locations"], [])
        self.assertTrue(item["model_context"]["old"]["locations"])

    def test_review_focus_excludes_unchanged_labels_and_visual_context(self):
        selected = {side: [
            {"id": f"{side}:e1", "role": "dimension", "raw_text": "H +/- 0.8", "confidence": .9,
             "source": f"{side}:same", "locations": [{"page": 1, "x": .1}]},
            {"id": f"{side}:e2", "role": "dimension", "raw_text": "7 Min" if side == "old" else "9 Min",
             "confidence": .9, "source": f"{side}:different", "locations": [{"page": 1, "x": .2}]},
            {"id": f"{side}:e3", "role": "figure context", "raw_text": "SYNTHETIC VIEW",
             "confidence": None, "source": f"{side}:view", "locations": [{"page": 1, "x": .3}]},
        ] for side in ("old", "new")}
        proposal = pair(observations=[
            {"kind": "text_change", "description": "Minimum value differs", "check": "Read the numbers.",
             "old_ids": ["old:e1", "old:e2"], "new_ids": ["new:e1", "new:e2"]},
            {"kind": "unchanged_text", "description": "Height annotation remains", "check": "Context only.",
             "old_ids": ["old:e1"], "new_ids": ["new:e1"]},
            {"kind": "visual_change", "description": "Old hatch fill, new unfilled area",
             "check": "Inspect fill strokes, not physical material removal.",
             "old_ids": ["old:e3"], "new_ids": ["new:e3"]},
        ])
        item = _record(proposal, selected, stage="coarse", issues=["Budget reached"])
        for side in ("old", "new"):
            self.assertEqual(item[side]["locations"], [{"page": 1, "x": .2}])
            self.assertEqual(len(item["model_context"][side]["locations"]), 3)
        self.assertEqual(item["model_comparison"]["observations"], proposal["observations"])
        proposal["observations"][0]["old_ids"] = ["old:e3"]
        with self.assertRaisesRegex(CUError, "figure context"):
            _focus_for_review(proposal, selected)
        proposal["observations"][0]["old_ids"] = ["old:unknown"]
        with self.assertRaisesRegex(CUError, "unknown"):
            _focus_for_review(proposal, selected)

    def test_visual_only_suggestion_has_context_but_no_invented_text_frames(self):
        self.coarse["pairs"][0].update(new_ids=[], observations=[
            {"kind": "visual_change", "description": "Check an internal fill difference",
             "old_ids": [], "new_ids": [], "check": "Inspect both source drawings."},
        ])
        item, = self.run_comparison()["items"]
        self.assertEqual(item["old"]["locations"], [])
        self.assertIsNone(item["new"])
        self.assertTrue(item["model_context"]["old"]["locations"])

    def test_crop_cache_tampering_is_an_error(self):
        self.run_comparison()
        crop = self.client.calls[0][0]
        crop.write_bytes(b"not the original crop")
        with self.assertRaisesRegex(CUError, "identity"):
            self.run_comparison()

    def test_second_page_crop_maps_back_to_second_original_page(self):
        for path in self.paths:
            with pymupdf.open(path) as pdf:
                pdf.new_page(width=720, height=360)
                data = pdf.tobytes()
            path.write_bytes(data)
        operation = response()
        first = operation["result"]["contents"][0]["pages"][0]
        second = copy.deepcopy(first)
        first["lines"] = []
        second["pageNumber"] = 2
        for line in second["lines"]:
            line["source"] = line["source"].replace("D(1,", "D(2,")
        operation["result"]["contents"][0]["pages"].append(second)
        result = self.run_comparison(operation, operation)
        for side in ("old", "new"):
            self.assertEqual(result["items"][0][side]["locations"][0]["page"], 2)

    def test_cancel_before_second_crop_stops_further_billed_requests(self):
        def progress(message):
            if "new CU" in message:
                raise ValueError("Synthetic cancelled upload")
        with self.assertRaisesRegex(ValueError, "cancelled"):
            self.run_comparison(progress=progress)
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.chat.call_count, 1)

    def test_disabled_and_invalid_options(self):
        self.client.config["model_comparison"]["enabled"] = False
        self.assertFalse(self.run_comparison()["coverage"]["enabled"])
        self.chat.assert_not_called()
        for value in (0, 9, True, 1.5):
            with self.assertRaises(ValueError):
                options({"model_comparison": {"max_regions": value}})
        with self.assertRaises(ValueError):
            options({"model_comparison": {"enabled": "true"}})
        for value in ({"deployment": "synthetic-next"}, {"deployment_version": "v2"},
                      {"deployment": "", "deployment_version": "v2"}):
            with self.assertRaises(ValueError):
                options({"model_comparison": value})

    def test_comparison_model_is_selected_independently_from_cu(self):
        self.client.config["model_comparison"].update(
            deployment="synthetic-next", deployment_version="next-v1:TestSku")
        self.run_comparison()
        for call in self.chat.call_args_list:
            self.assertEqual(call.args[2]["model"], "synthetic-next")
        self.assertEqual(self.client.config["completion_model"], "test-model")

    def test_optional_visual_stage_reuses_crops_without_additional_cu_calls(self):
        self.client.config["model_comparison"].update(
            visual_review=True, deployment="synthetic-next", deployment_version="next-v1")
        self.coarse["pairs"][0]["observations"] = [{
            "kind": "visual_change", "description": "A potential nontext fill change",
            "old_ids": [], "new_ids": [], "check": "Inspect separate subfeatures",
        }]
        stages = []
        result = self.run_comparison(stage_progress=stages.append)
        self.assertEqual(stages, ["model_coarse", "cu_crop_preparation", "cu_crop_pair",
                                  "model_fine", "model_visual"])
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(self.chat.call_count, 3)
        self.assertEqual(self.chat.call_args_list[-1].args[2]["model"], "synthetic-next")
        visual = result["coverage"]["visual"]["regions"][0]
        self.assertTrue(visual["no_visual_change_observed"])
        self.assertEqual(visual["features"], 0)
        self.assertTrue(any("不代表已证明图形完全相同" in text for text in result["warnings"]))
        self.assertTrue(all(item["channel"] == "model" for item in result["items"]))

    def test_visual_budget_and_invalid_options_are_explicit(self):
        for value in ({"visual_review": 1}, {"max_visual_regions": 0}, {"max_visual_regions": 5}):
            with self.assertRaises(ValueError):
                options({"model_comparison": value})
        self.client.config["model_comparison"].update(visual_review=True, max_visual_regions=1)
        self.coarse["pairs"].append(pair(2, label="Second synthetic view"))
        for proposal in self.coarse["pairs"]:
            proposal["observations"] = [{
                "kind": "visual_change", "description": "Possible hatching difference",
                "old_ids": [], "new_ids": [], "check": "Inspect pixels",
            }]
        result = self.run_comparison()
        deferred = [r for r in result["coverage"]["visual"]["regions"] if r["status"] == "unprocessed"]
        self.assertEqual(len(deferred), 1)
        item, = [i for i in result["items"] if i["change"] == "model_visual_deferred"]
        self.assertEqual(item["old"]["locations"], [])
        self.assertEqual(item["new"]["locations"], [])
        self.assertEqual(item["old"]["raw_text"], "")
        self.assertEqual(item["visual_comparison"]["measurement_status"], "unmeasured")
        self.assertEqual(item["visual_comparison"]["changed_pixels"], {"old": None, "new": None})
        self.assertEqual(len(self.client.calls), 2)

    def test_semantic_text_pairing_requires_explicit_boolean_and_bounded_catalogue(self):
        self.assertFalse(options({})["text_pairing"])
        self.assertTrue(options({"model_comparison": {"text_pairing": True}})["text_pairing"])
        for setting in ({"text_pairing": 1}, {"max_text_pairing_entries": 0},
                        {"max_text_pairing_entries": 801}, {"max_text_pairing_entries": True}):
            with self.assertRaises(ValueError):
                options({"model_comparison": setting})

    def test_single_sided_route_has_priority_within_shared_budget_and_no_duplicate_record(self):
        self.client.config["model_comparison"].update(visual_review=True, max_visual_regions=1)
        self.coarse["pairs"].append(pair(2, label="Generic late one-sided object", old_ids=[]))
        for proposal in self.coarse["pairs"]:
            proposal["observations"] = [{
                "kind": "visual_change", "description": "Check an object, not a physical addition",
                "old_ids": [], "new_ids": [], "check": "Locate and search opposite pages",
            }]

        def presence(proposal, selected, paths, **kwargs):
            self.assertFalse(kwargs["allow_submit"])
            self.assertEqual(self.client.usage_context["stage"], "model_visual_presence")
            self.assertEqual(selected["old"], [])
            self.assertEqual(set(paths), {"old", "new"})
            item = _visual_placeholder(
                proposal, selected, "Synthetic review", route="single_sided",
                status="presence_review", change="model_visual_presence_review")
            return item, {"label": proposal["label"], "route": "single_sided", "status": "presence_review"}

        stages = []
        with patch("cu_diff.model_presence.review_presence", side_effect=presence) as review:
            result = self.run_comparison(stage_progress=stages.append)
        self.assertEqual(stages[-1], "model_visual_presence")
        self.assertNotIn("model_visual", stages)
        review.assert_called_once()
        region, deferred = result["coverage"]["visual"]["regions"]
        self.assertEqual(region["route"], "single_sided")
        self.assertEqual(deferred["status"], "unprocessed")
        self.assertEqual(self.chat.call_count, 2, "No paired visual call may bypass the shared budget")
        self.assertEqual(sum(i["key"] == "Generic late one-sided object" for i in result["items"]), 1)

    def test_coarse_unchanged_graphical_suggestions_do_not_use_budget_or_claim_change(self):
        self.client.config["model_comparison"].update(visual_review=True, max_visual_regions=1)
        self.coarse["pairs"][0]["assessment"] = "unchanged"
        self.coarse["pairs"].append(pair(2, label="Actual visual hypothesis"))
        for proposal in self.coarse["pairs"]:
            proposal["observations"] = [{
                "kind": "visual_change", "description": "Graphical check recommendation",
                "old_ids": [], "new_ids": [], "check": "Check if needed",
            }]
        result = self.run_comparison()
        regions = result["coverage"]["visual"]["regions"]
        self.assertEqual([r["status"] for r in regions], ["context_review", "reviewed"])
        row, = [i for i in result["items"] if i["change"] == "model_visual_context"]
        self.assertEqual(row["old"]["locations"], [])
        self.assertEqual(row["new"]["locations"], [])
        self.assertEqual(row["model_comparison"]["status"], "coarse_unchanged")
        self.assertTrue(any(i["change"] == "model_visual_no_change" for i in result["items"]))
        self.assertEqual(self.chat.call_count, 3)



if __name__ == "__main__":
    unittest.main()
