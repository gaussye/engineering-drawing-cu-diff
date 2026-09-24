import copy
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

import pymupdf

from cu_diff.client import CUError, CacheMiss
from cu_diff.compare import _coverage, _extract, _overlap, _record, compare_documents
from cu_diff.semantic_text import VERSION, resolve_text_pairing
from cu_diff.web_evidence import locations


def drawing(value="72", *, x=1, y=1, key="generated-alpha", page_count=1, words=True):
    tokens = ["Clearance", "=", value, "±", "0.4"]
    source = f"D(1,{x},{y},2,.2)"
    item = {"valueObject": {
        name: {"valueString": text} for name, text in (
            ("Region", "synthetic section"), ("Category", "dimension"),
            ("Key", key), ("RawText", " ".join(tokens)), ("Detail", "generated hint"))}}
    item["valueObject"]["RawText"].update(source=source, confidence=.99)
    page = {
        "pageNumber": 1, "width": 10, "height": 10, "unit": "inch",
        "lines": [{"content": " ".join(tokens), "source": source, "confidence": .99}],
        "words": [{"content": token, "source": f"D(1,{x+i*.4},{y},.35,.2)", "confidence": .99}
                  for i, token in enumerate(tokens)] if words else [],
    }
    pages = [page] + [dict(pageNumber=i+1, width=10, height=10, unit="inch", lines=[], words=[])
                      for i in range(1, page_count)]
    return {"status": "Succeeded", "result": {"contents": [{
        "unit": "inch", "fields": {"Items": {"valueArray": [item]}}, "pages": pages}]}}


def identifier(side, channel="schema", index=0, page=0):
    return f"{side}:item:0:{index}" if channel == "schema" else f"{side}:ocr:0:{page}:{index}"


def pairing(channel="schema", *, old=None, new=None, assessment="supported"):
    return {"old_ids": old or [identifier("old", channel)],
            "new_ids": new or [identifier("new", channel)], "assessment": assessment,
            "rationale": "同一局部参数与相邻上下文支持对应；仍须人工复核。"}


def unpaired(old, new):
    """Use actual compare records but intentionally leave every source unresolved."""
    result = compare_documents(old, new)
    for channel, position, keys in (
            ("schema", 0, ("differences", "unchanged")),
            ("ocr", 1, ("ocr_differences", "ocr_unchanged"))):
        a, b = _extract(old, "old", .8)[position], _extract(new, "new", .8)[position]
        result[keys[0]] = ([_record(entry, None, "unpaired", 0, "unpaired") for entry in a]
                           + [_record(None, entry, "unpaired", 0, "unpaired") for entry in b])
        result[keys[1]] = []
        result["coverage"][channel] = _coverage(result[keys[0]], len(a), len(b))
    return result


class Client:
    def __init__(self):
        self.config = {
            "completion_model": "comparison", "model_deployments": {"comparison": "gpt-6-astra"},
            "deployment_versions": {"gpt-6-astra": "synthetic"},
            "model_comparison": {"enabled": True, "text_pairing": True},
        }
        self.usage_context = {"stage": "original"}


class SemanticTextTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / (".semantic-text-" + uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.paths = {side: self.root / f"{side}.pdf" for side in ("old", "new")}
        self.make_pdfs()
        self.responses = {
            "old": drawing(), "new": drawing("86", x=2.3, key="generated-beta")}
        self.client = Client()
        self.output = {"pairs": [pairing(), pairing("ocr")], "limitations": ["合成测试；仅提供模型候选。"]}
        self.metadata = {"cache_hit": True, "response_model": "gpt-6-astra-test", "usage": {"total_tokens": 42}}
        self.calls = []
        self.mock = patch("cu_diff.semantic_text.complete_json", side_effect=self.complete)
        self.chat = self.mock.start()
        self.addCleanup(self.mock.stop)

    def make_pdfs(self, count=1):
        for path in self.paths.values():
            with pymupdf.open() as pdf:
                for _ in range(count):
                    pdf.new_page(width=720, height=720)
                pdf.save(path)

    def complete(self, client, cache, body, **kwargs):
        self.assertEqual(client.usage_context, {"stage": "model_text_pairing"})
        self.assertEqual(body["model"], "gpt-6-astra")
        self.calls.append((body, kwargs))
        return copy.deepcopy(self.output), copy.deepcopy(self.metadata)

    def run_pairing(self, comparison=None, **kwargs):
        comparison = comparison if comparison is not None else compare_documents(
            self.responses["old"], self.responses["new"])
        return resolve_text_pairing(
            comparison, self.responses, self.paths, client=self.client, cache=self.root / "cache", **kwargs)

    def test_shifted_parameter_pairing_changes_actual_independent_channels(self):
        original = compare_documents(self.responses["old"], self.responses["new"])
        self.assertTrue(all(r["change"].startswith("unpaired") for r in original["differences"]))
        a, b = original["differences"][0]["old"], original["differences"][1]["new"]
        self.assertLess(_overlap(a, b), .65)
        before, responses = copy.deepcopy(original), copy.deepcopy(self.responses)
        result = self.run_pairing(original)
        self.assertEqual(original, before)
        self.assertEqual(self.responses, responses)
        for channel, key in (("schema", "differences"), ("ocr", "ocr_differences")):
            record, = result[key]
            self.assertEqual(record["change"], "modified")
            self.assertEqual(record["match"], {
                "method": "llm_source_id_pairing", "certainty": "model_proposed", "score": None})
            self.assertTrue(record["review_required"])
            self.assertEqual(record["semantic_pairing"]["model"], self.metadata)
            self.assertEqual(record["semantic_pairing"]["status"], "supported")
            words = record["text_comparison"]
            self.assertEqual(words["status"], "complete")
            self.assertEqual(words["changed_text"], {"old": ["72"], "new": ["86"]})
            for side in ("old", "new"):
                word, = words[side]
                mapped, error = locations(word, [{"number": 1, "width_pt": 720, "height_pt": 720}])
                self.assertIsNone(error)
                self.assertAlmostEqual(mapped[0]["width"], .035)
                self.assertNotIn(word["raw_text"], ("Clearance", "±", "0.4"))
                source_entry = next(r[side] for r in before[key] if r[side])
                self.assertEqual(record[side], source_entry)
            self.assertEqual(result["coverage"][channel]["matched_old"], 1)
            self.assertEqual(result["coverage"][channel]["unpaired_new"], 0)
            self.assertEqual(result["coverage"][channel]["old_total"], 1)
        self.assertEqual(self.chat.call_count, 1)
        self.assertEqual(self.calls[0][1], {"allow_submit": False})
        self.assertEqual(self.client.usage_context, {"stage": "original"})
        payload = json.loads(self.calls[0][0]["messages"][1]["content"][0]["text"])
        self.assertEqual(payload["version"], VERSION)
        self.assertEqual(len(payload["provenance"]["old"]["response_sha256"]), 64)
        self.assertEqual(result["coverage"]["semantic_text"]["paired_groups"], 2)

    def test_uncertain_keeps_original_unpaired_and_no_word_highlights(self):
        self.output["pairs"] = [pairing(assessment="uncertain")]
        original = compare_documents(self.responses["old"], self.responses["new"])
        result = self.run_pairing(original)
        for old_record, new_record in zip(original["differences"], result["differences"]):
            self.assertEqual(old_record["change"], new_record["change"])
            self.assertEqual(old_record["old"], new_record["old"])
            self.assertEqual(old_record["new"], new_record["new"])
            self.assertEqual(new_record["semantic_pairing"]["status"], "uncertain")
            self.assertNotIn("text_comparison", new_record)
        self.assertEqual(result["coverage"]["schema"]["unpaired_old"], 1)
        self.assertEqual(result["coverage"]["semantic_text"]["uncertain_groups"], 1)
        self.assertEqual(result["coverage"]["semantic_text"]["status"], "partial")
        self.assertEqual(result["coverage"]["semantic_text"]["unreferenced_ids"]["old"], ["old:ocr:0:0:0"])

    def test_pending_integration_status_is_finalized_without_mutating_input(self):
        self.responses["new"] = drawing("86", x=1, key="generated-beta")
        original = compare_documents(self.responses["old"], self.responses["new"], semantic_pairing=True)
        self.assertEqual(original["coverage"]["changed_text_pairing"], "semantic_pending")
        self.assertTrue(all(r["change"].startswith("unpaired") for r in original["differences"]))
        result = self.run_pairing(original)
        self.assertEqual(result["coverage"]["changed_text_pairing"], "semantic_completed")
        self.assertEqual(original["coverage"]["changed_text_pairing"], "semantic_pending")
        self.assertEqual(result["differences"][0]["old"]["field_evidence"],
                         original["differences"][0]["old"]["field_evidence"])
        self.assertTrue(all(warning in result["warnings"] for warning in original["warnings"]))
        self.output["pairs"] = [pairing(assessment="uncertain")]
        result = self.run_pairing(original)
        self.assertEqual(result["coverage"]["changed_text_pairing"], "semantic_partial")
        self.client.config["model_comparison"]["text_pairing"] = False
        result = self.run_pairing(original)
        self.assertEqual(result["coverage"]["changed_text_pairing"], "semantic_disabled")
        self.client.config["model_comparison"]["text_pairing"] = True
        original = compare_documents(self.responses["old"], self.responses["old"], semantic_pairing=True)
        result = self.run_pairing(original)
        self.assertEqual(result["coverage"]["changed_text_pairing"], "semantic_no_candidates")
        self.assertEqual(self.chat.call_count, 2)

    def test_invalid_ids_rejected_atomically(self):
        original = compare_documents(self.responses["old"], self.responses["new"])
        before = copy.deepcopy(original)
        cases = [
            pairing(old=["old:unknown"]),
            pairing(old=["new:item:0:0"]),
            pairing(old=["context:old:item:0:0"]),
            pairing(new=["new:ocr:0:0:0"]),
            pairing(old=["old:item:0:0", "old:item:0:0"]),
        ]
        for invalid in cases:
            with self.subTest(invalid=invalid):
                self.output["pairs"] = [pairing("ocr"), invalid]
                with self.assertRaises(CUError):
                    self.run_pairing(original)
                self.assertEqual(original, before)
        self.output["pairs"] = [pairing(), pairing(assessment="uncertain")]
        with self.assertRaises(CUError):
            self.run_pairing(original)
        self.assertEqual(original, before)

    def test_strict_schema_and_group_limits(self):
        good = copy.deepcopy(self.output)
        for invalid in (
                {"pairs": [], "limitations": [], "unexpected": True},
                {"pairs": [dict(pairing(), assessment="changed")], "limitations": []},
                {"pairs": [dict(pairing(), old_ids=[])], "limitations": []},
                {"pairs": [dict(pairing(), rationale=10)], "limitations": []},
                {"pairs": [pairing()] * 81, "limitations": []},
                {"pairs": [], "limitations": ["oversized response"] * 33},
                {"pairs": [pairing(old=["old:item:0:0"] * 5)], "limitations": []}):
            with self.subTest(output=invalid):
                self.output = invalid
                with self.assertRaises(CUError):
                    self.run_pairing()
        self.output = good

    def test_missing_low_confidence_or_unreconstructable_words_never_fabricates(self):
        for case in ("missing", "low", "partial", "ambiguous", "bad_source", "wrong_content"):
            with self.subTest(case=case):
                self.responses["old"] = drawing()
                page = self.responses["old"]["result"]["contents"][0]["pages"][0]
                if case == "missing":
                    page["words"] = []
                elif case == "low":
                    page["words"][2]["confidence"] = .2
                elif case == "partial":
                    page["words"] = page["words"][2:3]
                elif case == "ambiguous":
                    page["words"].insert(2, copy.deepcopy(page["words"][2]))
                elif case == "bad_source":
                    page["words"][2]["source"] = "D(1,20,20,.2,.2)"
                else:
                    extra = copy.deepcopy(self.responses["old"]["result"]["contents"][0])
                    page["words"] = []
                    self.responses["old"]["result"]["contents"].append(extra)
                result = self.run_pairing(unpaired(self.responses["old"], self.responses["new"]))
                record = next(r for r in result["differences"] if "text_comparison" in r)
                words = record["text_comparison"]
                self.assertEqual(words["status"], "unavailable")
                self.assertTrue(words["issues"])
                self.assertEqual(words["old"], [])
                self.assertEqual(words["new"], [])
                self.assertTrue(record["old"]["polygons"])

    def test_same_text_with_changed_key_is_interpretation_only(self):
        self.responses["new"] = drawing("72", x=2.3, key="another-generated-key")
        result = self.run_pairing(unpaired(self.responses["old"], self.responses["new"]))
        record, = result["differences"]
        self.assertEqual(record["change"], "interpretation_only")
        self.assertEqual(record["text_comparison"]["changed_text"], {"old": [], "new": []})
        ocr, = result["ocr_unchanged"]
        self.assertEqual(ocr["change"], "unchanged")
        self.assertTrue(ocr["review_required"])

    def test_identical_text_with_different_cu_tokenization_has_no_highlights(self):
        self.responses["new"] = drawing("72", x=2.3, key="changed-key")
        page = self.responses["new"]["result"]["contents"][0]["pages"][0]
        word = page["words"][2]
        word["content"] = "7"
        page["words"].insert(3, dict(word, content="2", source="D(1,3.3,1,.1,.2)"))
        result = self.run_pairing(unpaired(self.responses["old"], self.responses["new"]))
        self.assertEqual(result["differences"][0]["text_comparison"]["status"], "complete")
        self.assertEqual(result["differences"][0]["text_comparison"]["changed_text"], {"old": [], "new": []})

    def test_source_spans_exclude_unrelated_words_but_still_require_geometry(self):
        content = self.responses["old"]["result"]["contents"][0]
        page = content["pages"][0]
        text = page["lines"][0]["content"]
        offset = 0
        for word in page["words"]:
            word["span"] = {"offset": offset, "length": len(word["content"])}
            offset += len(word["content"]) + 1
        span = {"offset": 0, "length": len(text)}
        page["lines"][0]["span"] = span
        content["fields"]["Items"]["valueArray"][0]["valueObject"]["RawText"]["span"] = span
        page["words"].append({
            "content": "OTHER", "source": "D(1,1,1,.2,.2)",
            "confidence": .99, "span": {"offset": 300, "length": 5}})
        result = self.run_pairing()
        self.assertEqual(result["differences"][0]["text_comparison"]["status"], "complete")
        self.assertEqual(result["differences"][0]["text_comparison"]["changed_text"]["old"], ["72"])
        page["words"][2]["source"] = "D(1,8,8,.3,.2)"
        result = self.run_pairing()
        self.assertEqual(result["differences"][0]["text_comparison"]["status"], "unavailable")
        self.assertEqual(result["differences"][0]["text_comparison"]["new"], [])

    def test_exact_spans_allow_only_tiny_independently_rounded_polygon_overhang(self):
        content = self.responses["old"]["result"]["contents"][0]
        page = content["pages"][0]
        field = content["fields"]["Items"]["valueArray"][0]["valueObject"]["RawText"]
        field["source"] = "D(1,1,1.00002,3,1.00008,3,1.20008,1,1.20002)"
        span = {"offset": 500, "length": len(field["valueString"])}
        field["spans"] = [span]
        page["lines"][0]["span"] = span
        offset = span["offset"]
        for word in page["words"]:
            word["span"] = {"offset": offset, "length": len(word["content"])}
            offset += len(word["content"]) + 1
        original_word = copy.deepcopy(page["words"][2])
        result = self.run_pairing(unpaired(self.responses["old"], self.responses["new"]))
        for key in ("differences", "ocr_differences"):
            record, = result[key]
            self.assertEqual(record["text_comparison"]["status"], "complete")
            self.assertEqual(record["text_comparison"]["changed_text"], {"old": ["72"], "new": ["86"]})
            word, = record["text_comparison"]["old"]
            self.assertEqual(word["raw"], original_word)
            self.assertEqual(word["source"], original_word["source"])
            mapped, error = locations(word, [{"number": 1, "width_pt": 720, "height_pt": 720}])
            self.assertIsNone(error)
            self.assertAlmostEqual(mapped[0]["y"], .1)
            self.assertAlmostEqual(mapped[0]["width"], .035)
        # Mere nearby geometry is not enough: the tiny overhang needs CU spans.
        del field["spans"]
        result = self.run_pairing(unpaired(self.responses["old"], self.responses["new"]))
        self.assertEqual(result["differences"][0]["text_comparison"]["status"], "unavailable")
        self.assertEqual(result["ocr_differences"][0]["text_comparison"]["status"], "complete")
        # Even matching spans cannot authorize a material geometric displacement.
        field["spans"] = [span]
        field["source"] = "D(1,1,1.002,3,1.002,3,1.202,1,1.202)"
        result = self.run_pairing(unpaired(self.responses["old"], self.responses["new"]))
        self.assertEqual(result["differences"][0]["text_comparison"]["status"], "unavailable")
        self.assertEqual(result["differences"][0]["text_comparison"]["old"], [])

    def test_insertion_keeps_unchanged_side_empty(self):
        page = self.responses["new"]["result"]["contents"][0]["pages"][0]
        page["words"][2]["content"] = "72"
        page["words"].insert(3, {
            "content": "mm", "confidence": .99, "source": "D(1,3.45,1,.03,.2)"})
        page["lines"][0]["content"] = "Clearance = 72 mm ± 0.4"
        item = self.responses["new"]["result"]["contents"][0]["fields"]["Items"]["valueArray"][0]
        item["valueObject"]["RawText"]["valueString"] = page["lines"][0]["content"]
        result = self.run_pairing()
        words = result["differences"][0]["text_comparison"]
        self.assertEqual(words["status"], "complete")
        self.assertEqual(words["changed_text"], {"old": [], "new": ["mm"]})
        self.assertEqual(words["old"], [])

    def split_old(self):
        content = self.responses["old"]["result"]["contents"][0]
        first = content["fields"]["Items"]["valueArray"][0]
        second = copy.deepcopy(first)
        first["valueObject"]["RawText"].update(valueString="Clearance =", source="D(1,1,1,.75,.2)")
        second["valueObject"]["RawText"].update(valueString="72 ± 0.4", source="D(1,1.8,1,1.2,.2)")
        content["fields"]["Items"]["valueArray"].append(second)
        content["pages"][0]["lines"] = [
            {"content": "Clearance =", "source": "D(1,1,1,.75,.2)", "confidence": .99},
            {"content": "72 ± 0.4", "source": "D(1,1.8,1,1.2,.2)", "confidence": .99}]
        self.output["pairs"] = [
            pairing(channel, old=[identifier("old", channel, i) for i in range(2)])
            for channel in ("schema", "ocr")]

    def test_compact_split_merge_retains_every_source(self):
        self.split_old()
        comparison = unpaired(self.responses["old"], self.responses["new"])
        result = self.run_pairing(comparison)
        schema, = result["differences"]
        self.assertEqual(schema["old"]["schema_item_ids"], ["old:item:0:0", "old:item:0:1"])
        self.assertEqual(len(schema["old"]["schema_sources"]), 2)
        self.assertEqual(len(schema["old"]["lines"]), 2)
        self.assertEqual(schema["text_comparison"]["changed_text"], {"old": ["72"], "new": ["86"]})
        self.assertEqual(schema["text_comparison"]["status"], "complete")
        for channel in ("schema", "ocr"):
            self.assertEqual(result["coverage"][channel]["matched_old"], 2)
            self.assertEqual(result["coverage"][channel]["matched_new"], 1)
            self.assertEqual(result["coverage"][channel]["unpaired_old"], 0)

    def test_group_out_of_order_or_distant_columns_rejected(self):
        self.split_old()
        comparison = unpaired(self.responses["old"], self.responses["new"])
        self.output["pairs"][0]["old_ids"].reverse()
        with self.assertRaises(CUError):
            self.run_pairing(comparison)
        self.output["pairs"][0]["old_ids"].reverse()
        second = self.responses["old"]["result"]["contents"][0]["fields"]["Items"]["valueArray"][1]
        second["valueObject"]["RawText"]["source"] = "D(1,8,1,1.2,.2)"
        comparison = unpaired(self.responses["old"], self.responses["new"])
        with self.assertRaises(CUError):
            self.run_pairing(comparison)

    def test_groups_cannot_cross_pages_or_contents(self):
        self.make_pdfs(2)
        self.split_old()
        old = self.responses["old"]["result"]["contents"][0]
        old["pages"].append({"pageNumber": 2, "width": 10, "height": 10, "unit": "inch", "lines": [], "words": []})
        second = old["fields"]["Items"]["valueArray"][1]
        second["valueObject"]["RawText"]["source"] = "D(2,1.8,1,1.2,.2)"
        with self.assertRaises(CUError):
            self.run_pairing(unpaired(self.responses["old"], self.responses["new"]))
        second["valueObject"]["RawText"]["source"] = "D(1,1.8,1,1.2,.2)"
        extra = copy.deepcopy(old)
        extra["fields"]["Items"]["valueArray"] = [old["fields"]["Items"]["valueArray"].pop()]
        self.responses["old"]["result"]["contents"].append(extra)
        self.output["pairs"] = [pairing(old=["old:item:0:0", "old:item:1:0"])]
        with self.assertRaises(CUError):
            self.run_pairing(unpaired(self.responses["old"], self.responses["new"]))

    def test_duplicate_parameter_names_no_numeric_only_fallback(self):
        for side in ("old", "new"):
            content = self.responses[side]["result"]["contents"][0]
            other = drawing("91" if side == "old" else "97", x=6, y=5,
                            key="generated-alpha" if side == "old" else "generated-beta")
            extra = other["result"]["contents"][0]
            content["fields"]["Items"]["valueArray"].extend(extra["fields"]["Items"]["valueArray"])
            content["pages"][0]["lines"].extend(extra["pages"][0]["lines"])
            content["pages"][0]["words"].extend(extra["pages"][0]["words"])
        self.output["pairs"] = []
        result = self.run_pairing(unpaired(self.responses["old"], self.responses["new"]))
        self.assertTrue(all(r["change"].startswith("unpaired") for r in result["differences"]))
        self.assertEqual(result["coverage"]["semantic_text"]["paired_groups"], 0)
        self.assertEqual(len(result["coverage"]["semantic_text"]["unreferenced_ids"]["old"]), 4)
        # An explicit same-role pairing only associates its own physical words.
        self.output["pairs"] = [pairing()]
        result = self.run_pairing(unpaired(self.responses["old"], self.responses["new"]))
        record = next(r for r in result["differences"] if r.get("semantic_pairing"))
        self.assertEqual(record["text_comparison"]["changed_text"], {"old": ["72"], "new": ["86"]})

    def test_context_anchors_are_read_only_same_channel(self):
        for side in ("old", "new"):
            content = self.responses[side]["result"]["contents"][0]
            extra = drawing("91", x=1, y=1.4, key="anchor")
            anchor = extra["result"]["contents"][0]
            content["fields"]["Items"]["valueArray"].extend(anchor["fields"]["Items"]["valueArray"])
            content["pages"][0]["lines"].extend(anchor["pages"][0]["lines"])
            content["pages"][0]["words"].extend(anchor["pages"][0]["words"])
        comparison = compare_documents(self.responses["old"], self.responses["new"])
        result = self.run_pairing(comparison)
        payload = json.loads(self.calls[-1][0]["messages"][1]["content"][0]["text"])
        anchors = payload["context_anchors"]["old"]
        self.assertTrue(anchors)
        self.assertTrue(all(v["eligible"] is False for v in anchors))
        for candidate in payload["catalogs"]["old"]:
            selected = [v for v in anchors if v["id"] in candidate["context_ids"]]
            self.assertTrue(all(v["channel"] == candidate["channel"] for v in selected))
        self.assertEqual(result["unchanged"], comparison["unchanged"])
        self.output["pairs"] = [pairing(old=[anchors[0]["id"]])]
        with self.assertRaises(CUError):
            self.run_pairing(comparison)
        self.output["pairs"] = [pairing(old=["old:item:0:1"])]
        with self.assertRaises(CUError):
            self.run_pairing(comparison)

    def test_page_and_catalog_budgets_are_explicit_across_channels(self):
        self.make_pdfs(3)
        for side in ("old", "new"):
            self.responses[side] = drawing("72" if side == "old" else "86", page_count=3)
            content = self.responses[side]["result"]["contents"][0]
            template = content["fields"]["Items"]["valueArray"][0]
            content["fields"]["Items"]["valueArray"] = []
            for i in range(23):
                item = copy.deepcopy(template)
                item["valueObject"]["Key"]["valueString"] = f"generated-{side}-{i}"
                item["valueObject"]["RawText"]["source"] = f"D(1,1,{1+i*.25},2,.2)"
                content["fields"]["Items"]["valueArray"].append(item)
            distant = copy.deepcopy(template)
            distant["valueObject"]["RawText"]["source"] = "D(3,1,1,2,.2)"
            content["fields"]["Items"]["valueArray"].append(distant)
        self.client.config["model_comparison"]["max_text_pairing_entries"] = 20
        self.output["pairs"] = []
        result = self.run_pairing(unpaired(self.responses["old"], self.responses["new"]))
        coverage = result["coverage"]["semantic_text"]
        self.assertEqual(coverage["submitted"], {"old": 20, "new": 20})
        self.assertEqual(coverage["omitted"], {"old": 5, "new": 5})
        self.assertEqual(coverage["pages_omitted"], {"old": 1, "new": 1})
        self.assertIn("page budget", [v["reason"] for v in coverage["omissions"]["old"]])
        self.assertIn("old:ocr:0:0:0", coverage["omitted_ids"]["old"])
        self.assertEqual(result["coverage"]["schema"]["old_total"], 24)
        self.assertEqual(result["coverage"]["schema"]["unpaired_old"], 24)
        images = [v for v in self.calls[-1][0]["messages"][1]["content"] if v["type"] == "image_url"]
        self.assertEqual(len(images), 4)

    def test_invalid_or_forged_source_is_omitted_not_paired(self):
        comparison = unpaired(self.responses["old"], self.responses["new"])
        comparison["differences"][0]["old"]["source"] = "D(1,8,8,1,.2)"
        self.output["pairs"] = [pairing("ocr")]
        result = self.run_pairing(comparison)
        coverage = result["coverage"]["semantic_text"]
        self.assertIn("old:item:0:0", coverage["omitted_ids"]["old"])
        self.assertEqual(result["coverage"]["schema"]["unpaired_old"], 1)
        self.assertEqual(result["coverage"]["ocr"]["matched_old"], 1)

    def test_out_of_bounds_has_explicit_omission(self):
        self.responses["old"] = drawing(x=11)
        comparison = unpaired(self.responses["old"], self.responses["new"])
        result = self.run_pairing(comparison)
        self.chat.assert_not_called()
        self.assertEqual(result["coverage"]["semantic_text"]["omitted"]["old"], 2)
        self.assertTrue(all(r["change"].startswith("unpaired") for r in result["differences"]))

    def test_oversized_text_and_failed_cu_operation_are_explicit(self):
        raw = self.responses["old"]["result"]["contents"][0]["fields"]["Items"]["valueArray"][0]
        raw["valueObject"]["RawText"]["valueString"] = "S" * 4001
        self.output["pairs"] = [pairing("ocr")]
        result = self.run_pairing(unpaired(self.responses["old"], self.responses["new"]))
        self.assertEqual(result["coverage"]["semantic_text"]["omitted_ids"]["old"], ["old:item:0:0"])
        self.assertNotIn("S" * 4001, json.dumps(self.calls[-1][0]))
        self.responses["old"]["status"] = "Failed"
        result = self.run_pairing(unpaired(self.responses["old"], self.responses["new"]))
        self.assertEqual(result["coverage"]["semantic_text"]["submitted"]["old"], 0)
        self.assertEqual(self.chat.call_count, 1)

    def test_duplicate_or_already_consumed_endpoint_cannot_be_reused(self):
        comparison = unpaired(self.responses["old"], self.responses["new"])
        comparison["differences"].append(copy.deepcopy(comparison["differences"][0]))
        self.output["pairs"] = [pairing("ocr")]
        result = self.run_pairing(comparison)
        self.assertEqual(result["coverage"]["semantic_text"]["omitted_ids"]["old"],
                         ["old:item:0:0", "old:item:0:0"])
        comparison = unpaired(self.responses["old"], self.responses["new"])
        left, right = comparison["differences"][0]["old"], comparison["differences"][1]["new"]
        comparison["differences"].append(_record(left, right, "existing_match", 1, "high"))
        result = self.run_pairing(comparison)
        self.assertIn("already consumed", result["coverage"]["semantic_text"]["omissions"]["old"][0]["reason"])
        self.assertTrue(any(r["match"]["method"] == "existing_match" for r in result["differences"]))

    def test_cache_only_failure_passes_through_without_mutation(self):
        comparison = unpaired(self.responses["old"], self.responses["new"])
        before = copy.deepcopy(comparison)
        self.chat.side_effect = CacheMiss("synthetic immutable cache miss")
        with self.assertRaises(CacheMiss):
            self.run_pairing(comparison)
        self.assertFalse(self.chat.call_args.kwargs["allow_submit"])
        self.assertEqual(comparison, before)
        self.assertEqual(self.client.usage_context, {"stage": "original"})

    def test_submission_permission_and_request_are_deterministic(self):
        self.run_pairing(allow_submit=True)
        first = copy.deepcopy(self.calls[-1][0])
        self.run_pairing(allow_submit=False)
        self.assertEqual(first, self.calls[-1][0])
        self.assertTrue(self.calls[0][1]["allow_submit"])
        self.assertFalse(self.calls[1][1]["allow_submit"])
        self.responses["new"]["result"]["contents"][0]["pages"][0]["words"][0]["confidence"] = .98
        self.run_pairing()
        self.assertNotEqual(first, self.calls[-1][0])

    def test_disabled_no_candidates_and_one_sided_never_request(self):
        for settings in ({}, {"enabled": True}, {"enabled": False, "text_pairing": True}):
            self.client.config["model_comparison"] = settings
            result = self.run_pairing()
            self.assertEqual(result["coverage"]["semantic_text"]["status"], "disabled")
        self.client.config["model_comparison"] = {"enabled": True, "text_pairing": True}
        self.responses["new"] = copy.deepcopy(self.responses["old"])
        comparison = compare_documents(self.responses["old"], self.responses["new"])
        saved_paths, self.paths = self.paths, {}
        result = self.run_pairing(comparison)
        self.paths = saved_paths
        self.assertEqual(result["coverage"]["semantic_text"]["status"], "no_candidates")
        self.responses["new"]["result"]["contents"][0]["fields"]["Items"]["valueArray"] = []
        self.responses["new"]["result"]["contents"][0]["pages"][0]["lines"] = []
        result = self.run_pairing()
        self.assertEqual(result["coverage"]["semantic_text"]["submitted"], {"old": 0, "new": 0})
        self.assertEqual(result["coverage"]["semantic_text"]["omitted"]["old"], 2)
        self.chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
