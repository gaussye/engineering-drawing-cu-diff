import copy
import json
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from cu_diff.client import CUError, CacheMiss, canonical, digest
from cu_diff.model_client import complete_json


def request_body():
    return {
        "model": "synthetic",
        "messages": [{"role": "user", "content": "synthetic prompt"}],
        "max_completion_tokens": 100,
        "reasoning_effort": "low",
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "comparison", "strict": True, "schema": {
                "type": "object", "properties": {"changes": {"type": "array"}},
                "required": ["changes"], "additionalProperties": False,
            }},
        },
    }


def response():
    return {
        "model": "synthetic-actual-v1",
        "choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": '{"changes":[]}', "refusal": None,
        }}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    }


class FakeClient:
    def __init__(self):
        self.endpoint = "https://synthetic.services.ai.azure.com"
        self.config = {
            "model_deployments": {"gpt-test": "synthetic"},
            "deployment_versions": {"synthetic": "v1"},
        }
        self.events = []
        self.calls = []
        self.raw = response()
        self.error = None

    def request(self, method, url, body):
        self.calls.append((method, url, body))
        if self.error:
            self.events.append({"status": 500, "error_body": "SENSITIVE SENTINEL"})
            raise self.error
        return self.raw, {}


class ModelClientTests(unittest.TestCase):
    def setUp(self):
        self.cache = Path.cwd() / (".model-client-test-" + uuid.uuid4().hex)
        self.cache.mkdir()
        self.addCleanup(shutil.rmtree, self.cache)
        self.client = FakeClient()
        self.body = request_body()

    def complete(self, **kwargs):
        return complete_json(self.client, self.cache, self.body, **kwargs)

    def response_path(self):
        return next((self.cache / "model-comparison").glob("*.response.json"))

    def test_success_cache_and_actual_provenance(self):
        parsed, first = self.complete()
        cached, second = self.complete(allow_submit=False)
        self.assertEqual(parsed, {"changes": []})
        self.assertEqual(parsed, cached)
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.calls[0], (
            "POST", self.client.endpoint + "/openai/v1/chat/completions", self.body,
        ))
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["request_sha256"], digest(canonical(self.body)))
        self.assertEqual(first["model"], "synthetic")
        self.assertEqual(first["deployment_version"], "v1")
        self.assertEqual(first["response_model"], "synthetic-actual-v1")
        self.assertEqual(first["finish_reason"], "stop")
        self.assertEqual(first["usage"], self.client.raw["usage"])
        self.assertGreaterEqual(first["elapsed_seconds"], 0)
        self.assertNotIn("cost", first)
        self.assertNotIn("synthetic prompt", json.dumps(first))
        self.assertFalse(list((self.cache / "model-comparison").glob("*.pending.json")))

    def test_separate_comparison_deployment_is_pinned_without_mutating_cu_mapping(self):
        before = copy.deepcopy(self.client.config)
        self.client.config["model_comparison"] = {
            "deployment": "synthetic-comparison", "deployment_version": "comparison-v1:TestSku",
        }
        self.body["model"] = "synthetic-comparison"
        _, first = self.complete()
        self.assertEqual(first["deployment_version"], "comparison-v1:TestSku")
        self.assertEqual(self.client.config["model_deployments"], before["model_deployments"])
        self.assertEqual(self.client.config["deployment_versions"], before["deployment_versions"])
        self.client.config["model_comparison"]["deployment_version"] = "comparison-v2:TestSku"
        _, second = self.complete()
        self.assertNotEqual(first["cache_key"], second["cache_key"])
        self.body["model"] = "synthetic"
        with self.assertRaisesRegex(CUError, "comparison deployment"):
            self.complete()
        self.assertEqual(len(self.client.calls), 2)

    def test_separate_comparison_requires_version(self):
        self.client.config["model_comparison"] = {"deployment": "synthetic-comparison"}
        self.body["model"] = "synthetic-comparison"
        with self.assertRaises(CUError):
            self.complete()
        self.assertEqual(self.client.calls, [])

    def test_fingerprint_invalidates_all_request_dimensions(self):
        _, initial = self.complete()
        mutations = [
            lambda: self.body["messages"][0].update(content="different prompt"),
            lambda: self.body["response_format"]["json_schema"]["schema"].update(
                properties={"changes": {"type": "array", "maxItems": 3}}),
            lambda: self.body.update(max_completion_tokens=200),
            lambda: self.client.config["deployment_versions"].update(synthetic="v2"),
            lambda: setattr(self.client, "endpoint", "https://other.services.ai.azure.com"),
        ]
        keys = {initial["cache_key"]}
        for mutate in mutations:
            mutate()
            with self.assertRaises(CacheMiss):
                self.complete(allow_submit=False)
            _, meta = self.complete()
            self.assertNotIn(meta["cache_key"], keys)
            keys.add(meta["cache_key"])
        with patch("cu_diff.model_client.PROTOCOL_VERSION", "next-protocol"):
            with self.assertRaises(CacheMiss):
                self.complete(allow_submit=False)
        self.assertEqual(len(self.client.calls), 6)

    def test_unconfigured_model_or_version_never_submits(self):
        for model in ("gpt-test", "unknown", "", None):
            with self.subTest(model=model):
                self.body["model"] = model
                with self.assertRaisesRegex(CUError, "configured"):
                    self.complete()
        self.body["model"] = "synthetic"
        self.client.config["deployment_versions"] = {"other": "v1"}
        with self.assertRaisesRegex(CUError, "configured"):
            self.complete()
        self.client.config["deployment_versions"]["synthetic"] = "v1"
        self.client.config["model_deployments"] = {}
        with self.assertRaisesRegex(CUError, "configured"):
            self.complete()
        self.assertEqual(self.client.calls, [])

    def test_cache_only_miss_has_no_requests_or_marker(self):
        with self.assertRaises(CacheMiss):
            self.complete(allow_submit=False)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_requires_strict_schema_metadata_without_validating_output_schema(self):
        self.body["response_format"]["json_schema"]["strict"] = False
        with self.assertRaisesRegex(CUError, "strict"):
            self.complete()
        self.assertEqual(self.client.calls, [])
        self.body["response_format"]["json_schema"]["strict"] = True
        self.client.raw["choices"][0]["message"]["content"] = '{"not_in_schema":1}'
        self.assertEqual(self.complete()[0], {"not_in_schema": 1})
        self.assertEqual(self.complete()[0], {"not_in_schema": 1})
        self.assertEqual(len(self.client.calls), 1)

    def test_failed_completion_is_cached_and_never_rebilled(self):
        cases = []
        for finish in ("length", "content_filter", None):
            raw = response()
            raw["choices"][0]["finish_reason"] = finish
            cases.append(raw)
        for content in ("not JSON SENSITIVE SENTINEL", "[]", "null",
                        '{"x":NaN}', '{"x":1,"x":2}', None):
            raw = response()
            raw["choices"][0]["message"]["content"] = content
            cases.append(raw)
        for field, value in (("refusal", "SENSITIVE SENTINEL"),
                             ("refusal", ""),
                             ("tool_calls", [{"id": "tool"}]),
                             ("function_call", {"name": "tool"})):
            raw = response()
            raw["choices"][0]["message"][field] = value
            cases.append(raw)
        cases.extend([{}, [], {"error": {"message": "SENSITIVE SENTINEL"}},
                      dict(response(), choices=[]),
                      dict(response(), choices=[{}, {}]),
                      dict(response(), choices=[None]),
                      dict(response(), choices=[{"finish_reason": "stop", "message": None}]),
                      dict(response(), model=None)])
        for index, raw in enumerate(cases):
            with self.subTest(index=index):
                self.body["messages"][0]["content"] = str(index)
                self.client.raw = raw
                before = len(self.client.calls)
                with self.assertRaises(CUError) as first:
                    self.complete()
                with self.assertRaises(CUError) as second:
                    self.complete()
                self.assertEqual(str(first.exception), str(second.exception))
                self.assertNotIn("SENSITIVE SENTINEL", str(first.exception))
                self.assertEqual(len(self.client.calls), before + 1)

    def test_usage_is_optional_and_not_estimated(self):
        del self.client.raw["usage"]
        self.assertIsNone(self.complete()[1]["usage"])

    def test_corrupted_cache_is_explicit_and_never_resubmits(self):
        self.complete()
        path = self.response_path()
        original = json.loads(path.read_text(encoding="utf-8"))
        mutations = [
            lambda saved: saved["metadata"].update(request_sha256="bad"),
            lambda saved: saved["metadata"].update(deployment_version="bad"),
            lambda saved: saved["metadata"].update(elapsed_seconds=-1),
            lambda saved: saved["response"].update(model="tampered"),
            lambda saved: saved.update(state="pending"),
            lambda saved: saved.pop("response"),
        ]
        for mutate in mutations:
            saved = copy.deepcopy(original)
            mutate(saved)
            path.write_text(json.dumps(saved), encoding="utf-8")
            with self.assertRaisesRegex(CUError, "Corrupt"):
                self.complete()
        path.write_text("SENSITIVE SENTINEL invalid", encoding="utf-8")
        with self.assertRaisesRegex(CUError, "Corrupt") as caught:
            self.complete()
        self.assertNotIn("SENSITIVE SENTINEL", str(caught.exception))
        self.assertEqual(len(self.client.calls), 1)

    def test_cached_raw_response_is_revalidated(self):
        self.complete()
        path = self.response_path()
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["response"]["choices"][0]["finish_reason"] = "length"
        saved["response_sha256"] = digest(canonical(saved["response"]))
        path.write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaisesRegex(CUError, "finish"):
            self.complete()
        self.assertEqual(len(self.client.calls), 1)

    def test_transport_error_is_redacted_and_marker_prevents_duplicate_post(self):
        self.client.error = CUError("SENSITIVE SENTINEL server response", 500)
        with self.assertRaises(CUError) as caught:
            self.complete()
        self.assertEqual(caught.exception.status, 500)
        self.assertNotIn("SENSITIVE SENTINEL", str(caught.exception))
        self.assertNotIn("SENSITIVE SENTINEL", json.dumps(self.client.events))
        marker = next((self.cache / "model-comparison").glob("*.pending.json"))
        self.assertIn(marker.name, str(caught.exception))
        self.client.error = None
        for submit in (True, False):
            with self.assertRaisesRegex(CUError, "pending"):
                self.complete(allow_submit=submit)
        self.assertEqual(len(self.client.calls), 1)
        marker.unlink()
        self.complete()
        self.assertEqual(len(self.client.calls), 2)

    def test_timeout_never_retries(self):
        self.client.error = TimeoutError("SENSITIVE SENTINEL")
        for _ in range(2):
            with self.assertRaises(CUError):
                self.complete()
        self.assertEqual(len(self.client.calls), 1)

    def test_pending_marker_exists_before_post_and_arbitrates_competitor(self):
        original = self.client.request

        def concurrent(method, url, body):
            markers = list((self.cache / "model-comparison").glob("*.pending.json"))
            self.assertEqual(len(markers), 1)
            with self.assertRaisesRegex(CUError, "pending"):
                self.complete()
            return original(method, url, body)

        self.client.request = concurrent
        self.complete()
        self.assertEqual(len(self.client.calls), 1)

    def test_local_cache_save_failure_keeps_billing_guard(self):
        with patch("cu_diff.model_client.save_json", side_effect=OSError("SENSITIVE SENTINEL")):
            with self.assertRaisesRegex(CUError, "could not be cached") as caught:
                self.complete()
        self.assertNotIn("SENSITIVE SENTINEL", str(caught.exception))
        with self.assertRaisesRegex(CUError, "pending"):
            self.complete()
        self.assertEqual(len(self.client.calls), 1)

    def test_response_wins_if_other_caller_finishes_before_marker_creation(self):
        self.complete()
        response_path = self.response_path()
        saved = response_path.read_text(encoding="utf-8")
        response_path.unlink()
        original_open = Path.open

        def racing_open(path, *args, **kwargs):
            if args and args[0] == "x":
                with original_open(response_path, "w", encoding="utf-8") as target:
                    target.write(saved)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", racing_open):
            _, metadata = self.complete()
        self.assertTrue(metadata["cache_hit"])
        self.assertEqual(len(self.client.calls), 1)

    def test_marker_creation_failure_submits_nothing(self):
        with patch.object(Path, "open", side_effect=OSError("SENSITIVE SENTINEL")):
            with self.assertRaisesRegex(CUError, "no request submitted"):
                self.complete()
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
