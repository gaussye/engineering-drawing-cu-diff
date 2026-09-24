"""Layout-only synthetic evidence: no generated Items, customer data or services."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

import pymupdf

from cu_diff.compare import _extract, compare_documents, compare_responses
from cu_diff.semantic_text import resolve_text_pairing
from cu_diff.web_evidence import web_result


def layout(value="42.6", x=1):
    tokens = ["R8", "=", value, "±", "0.3"]
    words = [{"content": token, "source": f"D(1,{x+i*.4},1,.35,.2)", "confidence": .99}
             for i, token in enumerate(tokens)]
    words.append({"content": "REFERENCE", "source": "D(1,1,2,1,.2)", "confidence": .99})
    return {"status": "Succeeded", "result": {"contents": [{
        "unit": "inch",
        "pages": [{"pageNumber": 1, "width": 10, "height": 10, "unit": "inch",
                   "lines": [
                       {"content": " ".join(tokens), "source": f"D(1,{x},1,2,.2)", "confidence": .99},
                       {"content": "REFERENCE", "source": "D(1,1,2,1,.2)", "confidence": .99},
                   ], "words": words}],
        "tables": [],
    }]}}


class LayoutExtractionTests(unittest.TestCase):
    def test_intentionally_missing_schema_is_not_an_extraction_failure(self):
        old, new = layout(), layout("57.2", 3)
        original = deepcopy((old, new))
        comparison = compare_responses(old, new, extraction_profile="layout", semantic_pairing=True)
        self.assertEqual(comparison["extraction_profile"], "layout")
        self.assertEqual(comparison["primary_text_channel"], "ocr")
        self.assertEqual(comparison["differences"], [])
        self.assertEqual(comparison["unchanged"], [])
        self.assertEqual(comparison["uncertainties"], [])
        self.assertEqual(comparison["coverage"]["schema"]["old_total"], 0)
        self.assertEqual(comparison["coverage"]["schema_domain_fields"], "unavailable_by_design")
        self.assertEqual(comparison["coverage"]["old_extraction"]["invalid_items"], 0)
        self.assertEqual(comparison["coverage"]["ocr"]["old_total"], 2)
        self.assertTrue(comparison["coverage"]["notices"])
        self.assertFalse(any("no Items" in w or "missing or invalid Category" in w
                             for w in comparison["warnings"]))
        old_record, new_record = comparison["ocr_differences"]
        self.assertEqual(old_record["old"]["id"], "old:ocr:0:0:0")
        self.assertEqual(new_record["new"]["id"], "new:ocr:0:0:0")
        self.assertEqual(old_record["old"]["raw"], old["result"]["contents"][0]["pages"][0]["lines"][0])
        self.assertEqual((old, new), original)

    def test_valid_equal_layout_has_no_false_schema_review(self):
        comparison = compare_documents(layout(), layout(), extraction_profile="layout")
        self.assertFalse(comparison["review_required"])
        self.assertEqual(len(comparison["ocr_unchanged"]), 2)
        self.assertEqual(comparison["coverage"]["schema"]["review_required"], 0)

    def test_default_engineering_behavior_is_preserved(self):
        old, new = layout(), layout("57.2", 3)
        implicit = compare_documents(old, new)
        explicit = compare_documents(old, new, extraction_profile="engineering")
        self.assertEqual(implicit, explicit)
        self.assertEqual(implicit["primary_text_channel"], "schema")
        self.assertTrue(any("no Items.valueArray" in w for w in implicit["warnings"]))
        for invalid in ("auto", "", None, 7, True):
            with self.subTest(profile=invalid):
                with self.assertRaises(ValueError):
                    compare_responses(old, new, extraction_profile=invalid)
                with self.assertRaises(ValueError):
                    _extract(old, "old", .8, extraction_profile=invalid)

    def test_no_generated_title_or_bom_schema_is_synthesized(self):
        old = layout()
        content = old["result"]["contents"][0]
        content["pages"][0]["lines"][0]["content"] = "DRAWN BY SYNTHETIC NAME"
        content["tables"] = [{"rowCount": 2, "columnCount": 2, "cells": [
            {"rowIndex": 0, "columnIndex": 0, "content": "DESCRIPTION", "source": "D(1,1,3,1,.2)"},
            {"rowIndex": 0, "columnIndex": 1, "content": "QTY", "source": "D(1,2,3,1,.2)"},
            {"rowIndex": 1, "columnIndex": 0, "content": "SYNTHETIC COVER", "source": "D(1,1,3.3,1,.2)"},
            {"rowIndex": 1, "columnIndex": 1, "content": "2", "source": "D(1,2,3.3,1,.2)"},
        ]}]
        content["fields"] = {
            "Items": {"valueArray": [{"valueObject": {"RawText": {"valueString": "UNTRUSTED GENERATED"}}}]},
            "Uncertainties": {"valueArray": [{"valueString": "generated interpretation"}]},
        }
        original = deepcopy(old)
        with patch("cu_diff.compare.reconcile_title_fields", side_effect=AssertionError("No title heuristic")), \
                patch("cu_diff.compare.reconcile_bom", side_effect=AssertionError("No schema BOM heuristic")):
            comparison = compare_responses(old, deepcopy(old), extraction_profile="layout")
        self.assertEqual(comparison["differences"], [])
        self.assertEqual(comparison["unchanged"], [])
        self.assertEqual(comparison["uncertainties"], [])
        self.assertTrue(any("ignores unexpected" in w for w in comparison["warnings"]))
        self.assertEqual(old, original)

    def test_missing_ocr_and_invalid_geometry_remain_visible(self):
        for case, message in (
                ("pages", "no full OCR pages"), ("empty_pages", "no OCR pages"),
                ("lines", "has no OCR lines"), ("empty_lines", "no OCR text recognized"),
                ("words", "missing or invalid OCR words"), ("empty_words", "OCR words array is empty"),
                ("bad_word_geometry", "exceed page bounds"), ("bad_word", "invalid OCR word content"),
                ("dimensions", "invalid OCR page geometry"), ("bounds", "exceed page bounds"),
                ("source", "missing source"), ("text", "invalid OCR content")):
            with self.subTest(case=case):
                old = layout()
                content = old["result"]["contents"][0]
                page = content["pages"][0]
                if case == "pages":
                    del content["pages"]
                elif case == "empty_pages":
                    content["pages"] = []
                elif case == "lines":
                    del page["lines"]
                elif case == "empty_lines":
                    page["lines"] = []
                elif case == "words":
                    del page["words"]
                elif case == "empty_words":
                    page["words"] = []
                elif case == "bad_word_geometry":
                    page["words"][2]["source"] = "D(1,20,1,1,.2)"
                elif case == "bad_word":
                    page["words"][2] = None
                elif case == "dimensions":
                    page["width"] = None
                elif case == "bounds":
                    page["lines"][0]["source"] = "D(1,12,1,2,.2)"
                elif case == "source":
                    del page["lines"][0]["source"]
                elif case == "text":
                    page["lines"][0]["content"] = None
                comparison = compare_responses(old, layout(), extraction_profile="layout")
                self.assertTrue(comparison["review_required"])
                self.assertTrue(any(message in w for w in comparison["warnings"]), comparison["warnings"])
                self.assertFalse(any("no Items" in w for w in comparison["warnings"]))

    def test_layout_service_diagnostics_are_preserved(self):
        old = layout()
        old["warnings"] = [{"code": "SyntheticPartialRead", "message": "Check source scan"}]
        comparison = compare_responses(old, layout(), extraction_profile="layout")
        self.assertTrue(comparison["review_required"])
        self.assertEqual(comparison["coverage"]["service_warning_count"], 1)
        self.assertEqual(comparison["service_warnings"][0]["raw"], old["warnings"][0])


class LayoutSemanticEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / (".layout-evidence-" + uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.paths = {side: self.root / f"{side}.pdf" for side in ("old", "new")}
        for path in self.paths.values():
            with pymupdf.open() as pdf:
                pdf.new_page(width=720, height=720)
                pdf.save(path)
        self.responses = {"old": layout(), "new": layout("57.2", 3)}
        self.client = type("SyntheticClient", (), {})()
        self.client.config = {
            "completion_model": "synthetic", "model_deployments": {"synthetic": "gpt-6-astra"},
            "model_comparison": {"enabled": True, "text_pairing": True},
        }
        self.output = {"pairs": [{
            "old_ids": ["old:ocr:0:0:0"], "new_ids": ["new:ocr:0:0:0"],
            "assessment": "supported", "rationale": "同一参数，来源数字不同；须人工复核。",
        }], "limitations": []}
        self.docs = {side: {"pages": [{"number": 1, "width_pt": 720, "height_pt": 720}]}
                     for side in self.paths}

    def test_ocr_only_semantic_digits_use_raw_cu_words_without_anchor_frames(self):
        original = deepcopy(self.responses)
        comparison = compare_responses(**self.responses, extraction_profile="layout", semantic_pairing=True)
        with patch("cu_diff.semantic_text.complete_json", return_value=(
                self.output, {"cache_hit": True, "response_model": "synthetic"})) as complete:
            result = resolve_text_pairing(
                comparison, self.responses, self.paths, client=self.client, cache=self.root / "cache")
        self.assertEqual(complete.call_count, 1)
        self.assertFalse(complete.call_args.kwargs["allow_submit"])
        payload = json.loads(complete.call_args.args[2]["messages"][1]["content"][0]["text"])
        self.assertTrue(all(v["channel"] == "ocr" for entries in payload["catalogs"].values() for v in entries))
        self.assertTrue(all(v["channel"] == "ocr" for entries in payload["context_anchors"].values() for v in entries))
        self.assertEqual(result["differences"], [])
        record, = result["ocr_differences"]
        self.assertEqual(record["text_comparison"]["status"], "complete")
        self.assertEqual(record["text_comparison"]["changed_text"], {"old": ["42.6"], "new": ["57.2"]})
        for side in self.paths:
            raw_word = self.responses[side]["result"]["contents"][0]["pages"][0]["words"][2]
            actual_word, = record["text_comparison"][side]
            self.assertEqual(actual_word["raw"], raw_word)
            self.assertEqual(actual_word["source"], raw_word["source"])
        self.assertEqual(len(result["ocr_unchanged"]), 1)
        web = web_result(result, self.docs, {})
        self.assertEqual(web["extraction_profile"], "layout")
        self.assertEqual(web["primary_text_channel"], "ocr")
        self.assertEqual(web["metadata"]["extraction_profile"], "layout")
        self.assertEqual(web["metadata"]["primary_text_channel"], "ocr")
        item, = web["items"]
        self.assertEqual(item["channel"], "ocr")
        for side in self.paths:
            self.assertEqual(item[side]["id"], f"{side}:ocr:0:0:0")
            self.assertEqual(item["text_comparison"][side][0]["id"], f"{side}:word:0:0:2")
            box, = item[side]["locations"]
            self.assertAlmostEqual(box["width"], .035)
            self.assertGreater(item[side]["context_locations"][0]["width"], box["width"])
            self.assertEqual(len(item["text_comparison"][side]), 1)
        self.assertEqual(self.responses, original)
        self.assertTrue(web["coverage"]["notices"])

    def test_ocr_only_without_word_sources_does_not_fabricate_highlights(self):
        self.responses["old"]["result"]["contents"][0]["pages"][0]["words"] = []
        comparison = compare_responses(**self.responses, extraction_profile="layout", semantic_pairing=True)
        with patch("cu_diff.semantic_text.complete_json", return_value=(
                self.output, {"cache_hit": True, "response_model": "synthetic"})):
            result = resolve_text_pairing(
                comparison, self.responses, self.paths, client=self.client, cache=self.root / "cache")
        record, = result["ocr_differences"]
        self.assertEqual(record["text_comparison"]["status"], "unavailable")
        self.assertEqual(record["text_comparison"]["old"], [])
        self.assertEqual(record["text_comparison"]["new"], [])
        item, = web_result(result, self.docs, {})["items"]
        self.assertEqual(item["channel"], "ocr")
        for side in self.paths:
            self.assertEqual(item[side]["locations"], [])
            self.assertTrue(item[side]["context_locations"])


if __name__ == "__main__":
    unittest.main()
