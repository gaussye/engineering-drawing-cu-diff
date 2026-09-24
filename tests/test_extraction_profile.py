import copy
import json
from pathlib import Path
import shutil
import threading
import unittest
from unittest.mock import patch
import uuid

from cu_diff.client import CacheMiss, Client, CUError, canonical, digest
from cu_diff.parallel import run_cu_pair
from cu_diff.schema import definition, extraction_profile, layout_diagnostics


def config(profile="engineering"):
    return {
        "endpoint": "https://synthetic.services.ai.azure.com",
        "completion_model": "synthetic",
        "model_deployments": {"synthetic": "synthetic-deployment"},
        "deployment_versions": {"synthetic-deployment": "synthetic:1:GlobalStandard"},
        "processing_location": "geography",
        "extraction_profile": profile,
    }


def layout_result():
    source = "D(1,1,1,2,.3)"
    return {"status": "Succeeded", "usage": {"documentPagesStandard": 1}, "result": {"contents": [{
        "kind": "document", "unit": "inch", "markdown": "SYNTHETIC",
        "pages": [{"pageNumber": 1, "width": 10., "height": 5.,
                   "lines": [{"content": "SYNTHETIC", "source": source}],
                   "words": [{"content": "SYNTHETIC", "source": source, "confidence": .95}]}],
        "tables": [{"rowCount": 1, "columnCount": 1, "cells": [
            {"rowIndex": 0, "columnIndex": 0, "content": "SYNTHETIC", "source": source}]}],
    }]}}


def ready(profile):
    return {**definition("prebuilt-analyzer-completion", profile=profile), "status": "ready"}


class ExtractionProfileTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / (".extraction-profile-test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.path = self.root / "synthetic.pdf"
        self.path.write_bytes(b"synthetic fixture bytes")
        self.cache = self.root / "cache"

    def analyze(self, client, raw=None, profile="layout"):
        with patch.object(client, "request", return_value=({}, {
                "Operation-Location": client.url("analyzerResults/synthetic")})) as request, patch.object(
                client, "poll", return_value=raw or layout_result()):
            result = client.analyze(self.path, self.cache, "synthetic", ready(profile))
        return result, request

    def test_legacy_definition_hash_and_analyzer_id_unchanged(self):
        expected = "597e58434875b198ea9c34e71c03b7ffc33da42c0ddc313295f6ddb0a411a983"
        self.assertEqual(digest(canonical(definition("prebuilt-analyzer-completion"))), expected)
        self.assertEqual(definition("gpt-5.4"), definition("gpt-5.4", profile="engineering"))
        self.assertEqual(digest(canonical(definition("gpt-5.4"))),
                         "19bcd186bb4573dfc09dad24d06637892bd31635bc2e8aa46268976a40319931")
        self.assertEqual(extraction_profile({}), "engineering")
        client = Client(config())
        with patch.object(client, "request", side_effect=[
                ({"supportedModels": {"completion": ["synthetic"]}}, {}),
                (ready("engineering"), {})]) as request:
            identifier, _ = client.ensure_analyzer(allow_create=False)
        self.assertEqual(identifier, "engineering.evidence." + expected[:16])
        self.assertEqual([call.args[0] for call in request.call_args_list], ["GET", "GET"])

    def test_layout_definition_has_no_models_or_generated_fields(self):
        spec = definition("ignored", profile="layout")
        self.assertEqual(spec["models"], {})
        self.assertEqual(spec["fieldSchema"], {})
        self.assertEqual(spec["baseAnalyzerId"], "prebuilt-document")
        self.assertEqual(spec, definition("another-ignored-model", profile="layout"))
        for flag in ("returnDetails", "enableOcr", "enableLayout"):
            self.assertIs(spec["config"][flag], True)
        self.assertNotIn("enableBarcode", spec["config"])
        for flag in ("enableFormula", "enableFigureDescription", "enableFigureAnalysis",
                     "estimateFieldSourceAndConfidence", "omitContent"):
            self.assertIs(spec["config"][flag], False)
        self.assertEqual(spec["config"]["tableFormat"], "html")
        self.assertNotIn("Items", json.dumps(spec))
        self.assertNotIn('"method": "generate"', json.dumps(spec))

    def test_profile_validation_precedes_all_requests(self):
        for value in (None, True, 0, {}, [], "", "read", "LAYOUT"):
            with self.subTest(profile=value), patch.object(Client, "request") as request:
                with self.assertRaises(ValueError):
                    Client(config(value))
                request.assert_not_called()
                with self.assertRaises(ValueError):
                    definition("synthetic", profile=value)
        client = Client(config("layout"))
        client.config["extraction_profile"] = "invalid"
        with patch.object(client, "request") as request:
            with self.assertRaises(ValueError):
                client.ensure_analyzer()
            with self.assertRaises(ValueError):
                client.analyze(self.path, self.cache, "synthetic", {})
            request.assert_not_called()

    def test_layout_setup_uses_only_immutable_get_without_model_requirements(self):
        minimal = {"endpoint": config()["endpoint"], "extraction_profile": "layout",
                   "processing_location": "geography"}
        client = Client(minimal)
        with patch.object(client, "request", return_value=(ready("layout"), {})) as request:
            identifier, analyzer = client.ensure_analyzer(allow_create=False)
        self.assertEqual(identifier, "engineering.layout." + digest(
            canonical(definition("", profile="layout")))[:16])
        request.assert_called_once_with("GET", client.url("analyzers/" + identifier))
        self.assertEqual(analyzer["models"], {})

    def test_missing_layout_analyzer_never_created_in_web_mode(self):
        client = Client(config("layout"))
        with patch.object(client, "request", side_effect=CUError("missing", 404)) as request:
            with self.assertRaisesRegex(CUError, "Web mode never creates"):
                client.ensure_analyzer(allow_create=False)
        self.assertEqual([call.args[0] for call in request.call_args_list], ["GET"])

    def test_approved_setup_creates_only_hashed_analyzer_never_defaults(self):
        client = Client(config("layout"))
        spec = definition("", profile="layout")
        with patch.object(client, "request", side_effect=[
                CUError("missing", 404), ({}, {}), (ready("layout"), {})]) as request:
            identifier, _ = client.ensure_analyzer(allow_create=True)
        self.assertEqual([call.args[0] for call in request.call_args_list], ["GET", "PUT", "GET"])
        self.assertEqual(request.call_args_list[1].args,
                         ("PUT", client.url("analyzers/" + identifier), spec, {"If-None-Match": "*"}))
        self.assertTrue(all("/defaults" not in call.args[1] for call in request.call_args_list))

    def test_layout_setup_rejects_fields_models_and_config_drift(self):
        client = Client(config("layout"))
        for mutation in (
                lambda a: a.update(models={"completion": "synthetic"}),
                lambda a: a.update(fieldSchema={"fields": {"Items": {"type": "array"}}}),
                lambda a: a["config"].update(returnDetails=False),
                lambda a: a["config"].update(enableFigureDescription=True),
                lambda a: a["config"].update(enableFigureAnalysis=True),
                lambda a: a["config"].update(enableFormula=True)):
            actual = ready("layout")
            mutation(actual)
            with patch.object(client, "request", return_value=(actual, {})):
                with self.assertRaisesRegex(CUError, "differs"):
                    client.ensure_analyzer(allow_create=False)

    def test_service_empty_schema_normalization_does_not_inherit_generated_fields(self):
        client = Client(config("layout"))
        for field_schema in (None, {}, {"fields": {}}):
            actual = ready("layout")
            actual["fieldSchema"] = field_schema
            actual["models"] = None
            with patch.object(client, "request", return_value=(actual, {})):
                _, returned = client.ensure_analyzer(allow_create=False)
            self.assertEqual(returned, actual)

    def test_ga_inherited_embedding_alias_is_accepted_only_for_empty_layout(self):
        client = Client(config("layout"))
        actual = ready("layout")
        actual["models"] = {"embedding": "prebuilt-analyzer-embedding"}
        actual["config"].update(enableSegment=False, segmentPerPage=False,
                                chartFormat="chartjs", annotationFormat="markdown")
        self.assertNotIn("enableBarcode", actual["config"])
        with patch.object(client, "request", return_value=(actual, {})):
            _, returned = client.ensure_analyzer(allow_create=False)
        self.assertEqual(returned["models"], {"embedding": "prebuilt-analyzer-embedding"})
        for mutation in (
                lambda a: a["models"].update(completion="prebuilt-analyzer-completion"),
                lambda a: a["models"].update(completion=None),
                lambda a: a["models"].update(embedding="other-model"),
                lambda a: a.update(knowledgeSources=[{"kind": "reference", "synthetic": True}]),
                lambda a: a.update(trainingData={"synthetic": True}),
                lambda a: a["config"].update(enableSegment=True),
                lambda a: a["config"].update(enableChunking=True),
                lambda a: a["config"].update(enableEmbeddings=True),
                lambda a: a["config"].update(contentCategories={"category": {"description": "synthetic"}}),
                lambda a: a.update(fieldSchema={"fields": {"Items": {"type": "array"}}}),
                lambda a: a["config"].update(enableFigureDescription=True),
                lambda a: a["config"].update(enableFigureAnalysis=True)):
            changed = copy.deepcopy(actual)
            mutation(changed)
            with patch.object(client, "request", return_value=(changed, {})):
                with self.assertRaises(CUError):
                    client.ensure_analyzer(allow_create=False)

    def test_inherited_embedding_normalization_never_applies_to_engineering(self):
        client = Client(config("engineering"))
        actual = ready("engineering")
        actual["models"] = {"embedding": "prebuilt-analyzer-embedding"}
        with patch.object(client, "request", side_effect=[
                ({"supportedModels": {"completion": ["synthetic"]}}, {}), (actual, {})]):
            with self.assertRaisesRegex(CUError, "models differs"):
                client.ensure_analyzer(allow_create=False)

    def test_layout_body_metadata_and_cache_are_model_free_and_preserve_all_raw_evidence(self):
        original = config("layout")
        before = copy.deepcopy(original)
        client = Client(original)
        (raw, metadata), request = self.analyze(client)
        self.assertEqual(raw, layout_result())
        self.assertEqual(set(request.call_args.args[2]), {"inputs"})
        self.assertIn("processingLocation=geography", request.call_args.args[1])
        self.assertEqual(metadata["extraction_profile"], "layout")
        self.assertEqual(metadata["model_deployments"], {})
        self.assertEqual(metadata["deployment_versions"], {})
        self.assertIsNone(metadata["selected_completion_model"])
        self.assertEqual(metadata["extraction_contract"]["status"], "complete")
        self.assertEqual(metadata["extraction_contract"]["word_count"], 1)
        self.assertEqual(client.usage_records[0]["metadata"]["extraction_profile"], "layout")
        self.assertEqual(original, before)
        # Direct-model/CU configured deployments do not affect a model-free layout key.
        client.config["completion_model"] = "another"
        client.config["deployment_versions"] = {"another": "different"}
        with patch.object(client, "request") as request:
            _, cached = client.analyze(self.path, self.cache, "synthetic", ready("layout"), allow_submit=False)
            request.assert_not_called()
        self.assertEqual(cached["cache_key"], metadata["cache_key"])
        self.assertTrue(cached["cache_hit"])

    def test_explicit_engineering_and_omitted_profile_reuse_existing_cache_and_body(self):
        original = config()
        del original["extraction_profile"]
        client = Client(original)
        (_, first), request = self.analyze(client, profile="engineering")
        self.assertNotIn("extraction_profile", first)
        self.assertNotIn("extraction_contract", first)
        self.assertEqual(set(request.call_args.args[2]), {"inputs", "modelDeployments"})
        deployments = {**original["model_deployments"],
                       "prebuilt-analyzer-completion": "synthetic-deployment"}
        provenance = {
            "document_sha256": digest(self.path.read_bytes()), "byte_length": self.path.stat().st_size,
            "endpoint": client.endpoint, "api_version": client.api_version,
            "analyzer_id": "synthetic", "analyzer": ready("engineering"),
            "model_deployments": deployments, "selected_completion_model": "synthetic",
            "deployment_versions": original["deployment_versions"], "processing_location": "geography"}
        self.assertEqual(first["cache_key"], digest(canonical(provenance)))
        client.config["extraction_profile"] = "engineering"
        with patch.object(client, "request") as request:
            _, cached = client.analyze(self.path, self.cache, "synthetic", ready("engineering"),
                                       allow_submit=False)
            request.assert_not_called()
        self.assertEqual(first["cache_key"], cached["cache_key"])
        layout = Client(config("layout"))
        with patch.object(layout, "request") as request:
            with self.assertRaises(CacheMiss):
                layout.analyze(self.path, self.cache, "synthetic", ready("layout"), allow_submit=False)
            request.assert_not_called()

    def test_no_text_is_allowed_but_missing_detail_arrays_are_not_called_empty(self):
        raw = layout_result()
        page = raw["result"]["contents"][0]["pages"][0]
        page.update(lines=[], words=[])
        report = layout_diagnostics(raw)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["pages"][0]["text_status"], "no_text_detected")
        (_, metadata), _ = self.analyze(Client(config("layout")), raw)
        self.assertEqual(metadata["extraction_contract"]["status"], "complete")
        del page["words"]
        report = layout_diagnostics(raw)
        self.assertTrue(report["geometry_valid"])
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["pages"][0]["text_status"], "details_missing")
        self.assertIn("missing_ocr_details", report["issues"])

    def test_text_details_validate_sources_and_word_confidence_without_requiring_line_confidence(self):
        raw = layout_result()
        self.assertEqual(layout_diagnostics(raw)["status"], "complete")
        word = raw["result"]["contents"][0]["pages"][0]["words"][0]
        for changes in ({"source": None}, {"source": "D(2,1,1,2,.3)"},
                        {"confidence": None}, {"confidence": True},
                        {"confidence": float("nan")}, {"confidence": 1.1}):
            candidate = copy.deepcopy(raw)
            candidate["result"]["contents"][0]["pages"][0]["words"][0].update(changes)
            report = layout_diagnostics(candidate)
            self.assertIn("invalid_ocr_details", report["issues"])
            self.assertEqual(report["pages"][0]["invalid_words"], 1)
        self.assertEqual(word["confidence"], .95)
        raw["result"]["contents"][0]["pages"][0]["words"] = []
        self.assertIn("text_without_words", layout_diagnostics(raw)["issues"])

    def test_geometry_failures_preserve_paid_response_and_cache_failure_without_resubmission(self):
        client = Client(config("layout"))
        raw = layout_result()
        del raw["result"]["contents"][0]["pages"]
        with patch.object(client, "request", return_value=({}, {
                "Operation-Location": client.url("analyzerResults/synthetic")})) as request, patch.object(
                client, "poll", return_value=raw):
            for _ in range(2):
                with self.assertRaisesRegex(CUError, "page geometry"):
                    client.analyze(self.path, self.cache, "synthetic", ready("layout"))
            request.assert_called_once()
        self.assertEqual(len(list(self.cache.glob("*.response.json"))), 1)
        self.assertEqual(len(list(self.cache.glob("*.metadata.json"))), 1)
        self.assertEqual(list(self.cache.glob("*.operation.json")), [])
        self.assertEqual([entry["cache_state"] for entry in client.usage_records], ["new", "cached"])
        for changes in ({"width": 0}, {"height": float("nan")}, {"pageNumber": True},
                        {"width": -1}, {"unit": "unknown"}):
            invalid = layout_result()
            invalid["result"]["contents"][0]["pages"][0].update(changes)
            self.assertFalse(layout_diagnostics(invalid)["geometry_valid"])

    def test_layout_profile_retains_duplicate_safe_parallel_cache_lifecycle(self):
        client = Client(config("layout"))
        barrier = threading.Barrier(2)

        def analyze():
            barrier.wait(3)
            return client.analyze(self.path, self.cache, "synthetic", ready("layout"))

        with patch.object(client, "request", return_value=({}, {
                "Operation-Location": client.url("analyzerResults/synthetic")})) as request, patch.object(
                client, "poll", return_value=layout_result()):
            result = run_cu_pair(client, {"old": analyze, "new": analyze}, contexts={
                "old": {"stage": "cu_full_old"}, "new": {"stage": "cu_full_new"}})
            request.assert_called_once()
        self.assertEqual(sorted(meta["cache_hit"] for _, meta in result.values()), [False, True])
        self.assertEqual({entry["stage"] for entry in client.usage_records}, {"cu_full_old", "cu_full_new"})


if __name__ == "__main__":
    unittest.main()
