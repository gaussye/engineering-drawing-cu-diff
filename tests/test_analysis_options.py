import copy
import json
from pathlib import Path
import shutil
import threading
import time
import unittest
from unittest.mock import patch
import uuid

import pymupdf

from cu_diff.analysis_options import AnalysisOptions
from cu_diff.client import Client, CUError, canonical
from cu_diff.model_client import complete_json
from cu_diff.model_compare import _body, options, COARSE_PROMPT, COARSE_SCHEMA
from cu_diff.timing import JobTiming
from cu_diff.usage import usage_report
from cu_diff.web import create_app


BASE = "http://127.0.0.1:8765"
SYNTHETIC_STAGES = [
    "analyzer_preparation", "cu_full_pair", "model_coarse",
    "cu_crop_old", "cu_crop_new", "semantic_text_pairing",
    "local_table_comparison", "local_graphics_comparison", "result_preparation",
]


def config():
    return {
        "endpoint": "https://synthetic.services.ai.azure.com",
        "completion_model": "gpt-5.4",
        "model_deployments": {"gpt-5.4": "synthetic-cu"},
        "deployment_versions": {"synthetic-cu": "gpt-5.4:synthetic:GlobalStandard"},
        "processing_location": "geography",
        "model_comparison": {
            "enabled": True, "text_pairing": True,
            "deployment": "gpt-6-astra",
            "deployment_version": "gpt-6-astra:2026-09-03:GlobalStandard",
            "default_model": "gpt-6-astra",
            "models": [
                {"id": "gpt-6-astra", "label": "GPT-6 Astra", "deployment": "gpt-6-astra",
                 "deployment_version": "gpt-6-astra:2026-09-03:GlobalStandard"},
                {"id": "gpt-6-luna", "label": "GPT-6 Luna", "deployment": "gpt-6-luna",
                 "deployment_version": "gpt-6-luna:2026-09-22:GlobalStandard"},
            ],
        },
    }


def body(client, marker="comparison"):
    return _body(client, options(client.config), COARSE_PROMPT, {"synthetic": marker},
                 [], schema=COARSE_SCHEMA)


def pdf_bytes(text):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=144, height=144)
        page.insert_text((20, 40), text)
        return pdf.tobytes()


class SyntheticClient(Client):
    instances = []
    calls = []
    model_gate = None
    model_entered = None
    model_failure = False

    def __init__(self, config):
        super().__init__(config)
        self.instances.append(self)

    def _authenticate(self):
        raise AssertionError("Synthetic tests must never authenticate")

    def ensure_analyzer(self, *, allow_create=True):
        assert allow_create is False
        return "synthetic", {}

    def request(self, method, url, body=None, extra_headers=None):
        assert method == "POST"
        self.calls.append((url, copy.deepcopy(body)))
        if "/openai/" in url:
            if self.model_entered:
                self.model_entered.set()
            if self.model_gate:
                assert self.model_gate.wait(5)
            if self.model_failure:
                raise CUError("Synthetic ambiguous request")
            return {
                "model": body["model"],
                "choices": [{"finish_reason": "stop", "message": {
                    "content": '{"pairs":[],"limitations":[]}', "refusal": None}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }, {}
        return {}, {"Operation-Location": self.url("analyzerResults/synthetic")}

    def poll(self, url, *, usage_entry=None):
        return {"status": "Succeeded", "usage": {"documentPagesStandard": 1},
                "result": {"contents": [{"unit": "inch", "fields": {},
                                        "pages": [{"pageNumber": 1, "width": 2, "height": 2,
                                                   "lines": []}]}]}}


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / (".analysis-options-test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        SyntheticClient.instances, SyntheticClient.calls = [], []
        SyntheticClient.model_gate = SyntheticClient.model_entered = None
        SyntheticClient.model_failure = False


class OptionTests(WorkspaceTest):
    def test_allowlist_snapshot_isolation_and_configuration_validation(self):
        original = config()
        expected = copy.deepcopy(original)
        registry = AnalysisOptions(original)
        astra, astra_public = registry.snapshot()
        luna, public = registry.snapshot(False, "gpt-6-luna")
        self.assertEqual(public, {"use_cache": False, "model": "gpt-6-luna",
                                  "model_label": "GPT-6 Luna"})
        self.assertTrue(astra_public["use_cache"])
        for key in ("completion_model", "model_deployments", "deployment_versions", "endpoint"):
            self.assertEqual(luna[key], original[key])
        self.assertEqual(luna["model_comparison"]["deployment"], "gpt-6-luna")
        luna["model_deployments"]["gpt-5.4"] = "changed"
        original["model_comparison"]["models"][0]["deployment"] = "changed"
        again, _ = registry.snapshot()
        self.assertEqual(again, astra)
        self.assertEqual(astra, expected)
        for invalid in (0, 1, None, "", "false", [], {}):
            with self.subTest(use_cache=invalid), self.assertRaises(ValueError):
                registry.snapshot(invalid)
        for invalid in ("", "gpt-5.4", "https://attacker.example", True, {}, ["gpt-6-luna"]):
            with self.subTest(model=invalid), self.assertRaises(ValueError):
                registry.snapshot(True, invalid)
        mutations = (
            lambda c: c["model_comparison"].update(models=[]),
            lambda c: c["model_comparison"].update(default_model="not-approved"),
            lambda c: c["model_comparison"]["models"][1].update(id="gpt-6-astra"),
            lambda c: c["model_comparison"]["models"][0].update(endpoint="https://other.example"),
            lambda c: c["model_comparison"]["models"][0].update(deployment_version=""),
        )
        for mutation in mutations:
            invalid = config()
            mutation(invalid)
            with self.assertRaises(ValueError):
                AnalysisOptions(invalid)

    def test_legacy_astra_body_cache_and_cu_keys_remain_exactly_compatible(self):
        legacy = config()
        del legacy["model_comparison"]["models"]
        del legacy["model_comparison"]["default_model"]
        registry = AnalysisOptions(config())
        astra_config, _ = registry.snapshot()
        luna_config, _ = registry.snapshot(model="gpt-6-luna")
        legacy_client, astra, luna = [SyntheticClient(c) for c in (legacy, astra_config, luna_config)]
        document = self.root / "synthetic.pdf"
        document.write_bytes(b"synthetic bytes")
        cache = self.root / "cache"
        _, old_cu = legacy_client.analyze(document, cache, "synthetic", {})
        old_body = body(legacy_client)
        self.assertEqual(canonical(old_body), canonical(body(astra)))
        _, old_model = complete_json(legacy_client, cache, old_body)
        calls = len(SyntheticClient.calls)
        _, new_cu = luna.analyze(document, cache, "synthetic", {}, allow_submit=False)
        _, new_model = complete_json(astra, cache, body(astra), allow_submit=False)
        self.assertEqual(len(SyntheticClient.calls), calls)
        self.assertEqual(old_cu["cache_key"], new_cu["cache_key"])
        self.assertEqual(old_model["cache_key"], new_model["cache_key"])
        self.assertTrue(new_cu["cache_hit"])
        self.assertTrue(new_model["cache_hit"])
        _, luna_model = complete_json(luna, cache, body(luna))
        self.assertNotEqual(luna_model["cache_key"], new_model["cache_key"])
        self.assertEqual(luna_model["response_model"], "gpt-6-luna")
        pricing = {"currency": "USD", "rates": [
            {"key": "model.gpt-6-astra." + kind, "price": 1., "unit_quantity": 1000000,
             "unit": "tokens", "currency": "USD", "as_of": "2026-09-24",
             "source": "https://example.com/synthetic-pricing"}
            for kind in ("input", "cached_input", "output")]}
        journal = usage_report(luna.usage_records, pricing)
        entry = next(e for e in journal["entries"] if e["service"] == "model")
        self.assertEqual(entry["model"], "gpt-6-luna")
        self.assertIsNone(entry["reference_cost"])
        self.assertTrue(all("gpt-6-luna" in m["key"] for m in entry["meters"]))


class TimingTests(unittest.TestCase):
    def test_parallel_children_overlap_without_inflating_top_level_wall_clock(self):
        now = [0.]
        timing = JobTiming(lambda: now[0])
        timing.start()
        timing.stage("cu_full_pair", execution="parallel")
        timing.step("cu_full_old", "running")
        timing.step("cu_full_new", "running")
        now[0] = 3.
        timing.step("cu_full_new", "completed")
        now[0] = 5.
        live = timing.snapshot()["stages"][0]
        self.assertEqual([s["elapsed_seconds"] for s in live["steps"]], [5., 3.])
        self.assertEqual([s["status"] for s in live["steps"]], ["running", "completed"])
        timing.step("cu_full_old", "completed")
        timing.stage("model_coarse")
        now[0] = 7.
        timing.finish()
        snapshot = timing.snapshot()
        self.assertEqual(snapshot["total_seconds"], 7.)
        self.assertEqual(sum(s["elapsed_seconds"] for s in snapshot["stages"]), 7.)
        self.assertEqual(snapshot["stages"][0]["elapsed_seconds"], 5.)
        self.assertEqual(snapshot["stages"][0]["execution"], "parallel")

    def test_failed_parallel_stage_preserves_finished_sibling_duration(self):
        now = [0.]
        timing = JobTiming(lambda: now[0])
        timing.start()
        timing.stage("cu_crop_pair", execution="parallel")
        timing.step("cu_crop_old", "running")
        timing.step("cu_crop_new", "running")
        now[0] = 2.
        timing.step("cu_crop_old", "failed")
        now[0] = 4.
        timing.step("cu_crop_new", "completed")
        timing.finish(failed=True)
        stage = timing.snapshot()["stages"][0]
        self.assertEqual(stage["status"], "failed")
        self.assertEqual([s["status"] for s in stage["steps"]], ["failed", "completed"])
        self.assertEqual([s["elapsed_seconds"] for s in stage["steps"]], [2., 4.])
        self.assertEqual(stage["elapsed_seconds"], 4.)

    def test_repeated_substeps_are_unique_and_nonoverlapping(self):
        now = [0.]
        timing = JobTiming(lambda: now[0])
        timing.start()
        expected = ["model_coarse", "cu_crop_old", "cu_crop_new", "model_fine",
                    "cu_crop_old", "cu_crop_new", "model_fine", "model_visual",
                    "model_visual_presence"]
        for identifier in expected:
            timing.stage(identifier)
            now[0] += 1.
        timing.finish()
        snapshot = timing.snapshot()
        stages = snapshot["stages"]
        self.assertEqual(len({s["id"] for s in stages}), len(expected))
        self.assertEqual(stages[4]["id"], "cu_crop_old_2")
        self.assertIn("第2段", stages[4]["label"])
        self.assertNotIn("model_comparison", {s["id"] for s in stages})
        self.assertTrue(all(s["elapsed_seconds"] == 1. for s in stages))
        self.assertEqual(sum(s["elapsed_seconds"] for s in stages), snapshot["total_seconds"])

    def test_live_sequential_queue_complete_and_failure_timing(self):
        now = [10.]
        clock = lambda: now[0]
        timing = JobTiming(clock)
        now[0] = 12.
        self.assertEqual(timing.snapshot()["queue_seconds"], 2.)
        timing.start()
        timing.stage("analyzer_preparation")
        now[0] = 15.
        live = timing.snapshot()
        self.assertEqual(live["total_seconds"], 5.)
        self.assertEqual(live["stages"][0]["elapsed_seconds"], 3.)
        self.assertEqual(live["stages"][0]["status"], "running")
        timing.stage("cu_full_old")
        now[0] = 19.
        timing.finish(failed=True)
        done = timing.snapshot()
        self.assertEqual([s["status"] for s in done["stages"]], ["completed", "failed"])
        self.assertEqual([s["elapsed_seconds"] for s in done["stages"]], [3., 4.])
        self.assertEqual(done["total_seconds"], 9.)
        now[0] = 99.
        self.assertEqual(timing.snapshot(), done)
        timing.finish()
        self.assertEqual(timing.snapshot(), done)
        clean = JobTiming(clock)
        clean.start()
        clean.stage("result_preparation")
        clean.finish()
        self.assertEqual(clean.snapshot()["stages"][0]["status"], "completed")


class JobOptionTests(WorkspaceTest):
    def setUp(self):
        super().setUp()
        self.app = create_app(config(), self.root / "data", self.root / "cache",
                              allow_azure=True, client_factory=SyntheticClient)
        self.store = self.app.extensions["review_store"]
        self.addCleanup(self.stop)
        self.client = self.app.test_client()
        self.boot = self.client.get("/api/bootstrap", base_url=BASE).get_json()
        self.headers = {"Origin": BASE, "X-CSRF-Token": self.boot["csrf_token"]}
        self.upload("old", "OLD")
        self.revision = self.upload("new", "NEW").get_json()["revision"]
        self.crop = self.root / "synthetic-crop.pdf"
        self.crop.write_bytes(b"synthetic crop bytes")

        def compare(*args, client, cache, allow_submit, progress, stage_progress, **kwargs):
            stage_progress("model_coarse")
            progress("Synthetic model progress")
            client.usage_context = {"stage": "model_coarse"}
            _, meta = complete_json(client, cache, body(client), allow_submit=allow_submit)
            for side in ("old", "new"):
                stage_progress("cu_crop_" + side)
                client.usage_context = {"stage": "cu_crop_" + side}
                client.analyze(self.crop, cache, "synthetic", {}, allow_submit=allow_submit)
            return {"items": [], "warnings": [], "coverage": {"model": meta}}

        def pair(comparison, *args, client, cache, allow_submit, **kwargs):
            client.usage_context = {"stage": "model_text_pairing"}
            complete_json(client, cache, body(client, "pairing"), allow_submit=allow_submit)
            return comparison

        for target, replacement in (("cu_diff.model_compare.compare_with_model", compare),
                                    ("cu_diff.semantic_text.resolve_text_pairing", pair)):
            patcher = patch(target, side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def stop(self):
        if SyntheticClient.model_gate:
            SyntheticClient.model_gate.set()
        self.store.executor.shutdown(wait=True)

    def upload(self, role, text):
        return self.client.put("/api/documents/" + role, base_url=BASE, headers=self.headers,
                               content_type="application/pdf", data=pdf_bytes(text))

    def submit(self, **values):
        return self.client.post("/api/compare", base_url=BASE, headers=self.headers,
                                json={"revision": self.revision, **values})

    def wait(self, identifier):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = self.client.get("/api/jobs/" + identifier, base_url=BASE).get_json()
            if job["status"] not in ("queued", "running"):
                return job
            time.sleep(.01)
        self.fail("Synthetic job did not finish")

    def run_job(self, **values):
        response = self.submit(**values)
        self.assertEqual(response.status_code, 202, response.get_json())
        self.assertIn("timing", response.get_json())
        job = self.wait(response.get_json()["job_id"])
        self.assertEqual(job["status"], "succeeded", job.get("error"))
        return job

    def test_default_bootstrap_options_and_cache_reuse_then_model_switch(self):
        self.assertEqual(self.boot["analysis_options"], {
            "use_cache": True, "model": "gpt-6-astra",
            "models": [{"id": "gpt-6-astra", "label": "GPT-6 Astra"},
                       {"id": "gpt-6-luna", "label": "GPT-6 Luna"}]})
        self.assertEqual(self.boot["model"], "gpt-5.4")
        self.assertEqual(self.boot["model_comparison_deployment"], "gpt-6-astra")
        first = self.run_job()
        count = len(SyntheticClient.calls)
        self.assertEqual(count, 5)  # Two full CU, one deduplicated crop, two direct requests.
        second = self.run_job()
        self.assertEqual(len(SyntheticClient.calls), count)
        self.assertEqual(second["analysis_options"], first["analysis_options"])
        self.store.allow_azure = False
        self.run_job()
        self.assertEqual(len(SyntheticClient.calls), count)
        self.store.allow_azure = True
        third = self.run_job(model="gpt-6-luna")
        self.assertEqual(len(SyntheticClient.calls), count + 2)
        self.assertEqual(third["result"]["model_coverage"]["model"]["model"], "gpt-6-luna")
        self.assertEqual(third["analysis_options"], third["result"]["analysis_options"])
        self.assertEqual(self.store.config["model_comparison"]["deployment"], "gpt-6-astra")
        self.assertEqual(first["result"]["model_coverage"]["model"]["deployment_version"],
                         "gpt-6-astra:2026-09-03:GlobalStandard")
        for instance in SyntheticClient.instances:
            self.assertEqual(instance.config["completion_model"], "gpt-5.4")
            self.assertNotIn("gpt-6-luna", instance.config["deployment_versions"])

    def test_full_cu_requests_overlap_and_keep_per_side_usage_and_child_timing(self):
        rendezvous = threading.Barrier(2)
        original = SyntheticClient.poll

        def poll(client, *args, **kwargs):
            if client.usage_context.get("stage", "").startswith("cu_full_"):
                rendezvous.wait(timeout=5)
            return original(client, *args, **kwargs)

        with patch.object(SyntheticClient, "poll", poll):
            job = self.run_job()
        group = next(s for s in job["timing"]["stages"] if s["id"] == "cu_full_pair")
        self.assertEqual(group["execution"], "parallel")
        self.assertEqual([s["id"] for s in group["steps"]], ["cu_full_old", "cu_full_new"])
        self.assertTrue(all(s["status"] == "completed" for s in group["steps"]))
        audit = json.loads(next((self.root / "data").glob("web-session-*/*.json")).read_text(encoding="utf-8"))
        full = [r for r in audit["usage_records"] if r["stage"].startswith("cu_full_")]
        self.assertEqual({r["stage"] for r in full}, {"cu_full_old", "cu_full_new"})
        self.assertEqual(len({r["id"] for r in full}), 2)

    def test_parallel_full_failure_drains_other_side_without_partial_result(self):
        rendezvous = threading.Barrier(2)
        original = SyntheticClient.poll
        completed = threading.Event()

        def poll(client, *args, **kwargs):
            rendezvous.wait(timeout=5)
            if client.usage_context["stage"] == "cu_full_old":
                raise CUError("Synthetic old extraction failed")
            time.sleep(.02)
            result = original(client, *args, **kwargs)
            completed.set()
            return result

        with patch.object(SyntheticClient, "poll", poll):
            started = self.submit()
            job = self.wait(started.get_json()["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertNotIn("result", job)
        self.assertTrue(completed.is_set())
        group = job["timing"]["stages"][-1]
        self.assertEqual(group["id"], "cu_full_pair")
        self.assertEqual(group["status"], "failed")
        self.assertEqual([s["status"] for s in group["steps"]], ["failed", "completed"])
        self.assertEqual(len(SyntheticClient.calls), 2)

    def test_cache_off_isolated_durable_namespace_preserves_shared_cache_and_guards(self):
        self.run_job()
        shared = {p.relative_to(self.store.cache): p.read_bytes()
                  for p in self.store.cache.rglob("*") if p.is_file()}
        guard = self.store.cache / "model-comparison" / "synthetic.pending.json"
        guard.write_text('{"state":"pending"}', encoding="utf-8")
        count = len(SyntheticClient.calls)
        for _ in range(2):
            job = self.run_job(use_cache=False)
            count += 5
            self.assertEqual(len(SyntheticClient.calls), count)
            self.assertFalse(job["analysis_options"]["use_cache"])
            namespace = self.store.cache / "uncached-jobs" / job["job_id"]
            self.assertTrue(namespace.is_dir())
            self.assertEqual(len(list(namespace.glob("*.response.json"))), 3)
            self.assertEqual(len(list((namespace / "model-comparison").glob("*.response.json"))), 2)
            # Exactly repeated crops still deduplicate within the fresh job.
            self.assertEqual(job["usage_cost"]["summary"]["requests"]["new"], 5)
            self.assertEqual(job["usage_cost"]["summary"]["requests"]["cached"], 1)
        for path, content in shared.items():
            self.assertEqual((self.store.cache / path).read_bytes(), content)
        self.assertEqual(guard.read_text(encoding="utf-8"), '{"state":"pending"}')
        self.run_job()
        self.assertEqual(len(SyntheticClient.calls), count)

    def test_cached_historical_service_durations_do_not_become_current_timing(self):
        self.run_job()
        count = len(SyntheticClient.calls)
        for path in (self.store.cache / "model-comparison").glob("*.response.json"):
            saved = json.loads(path.read_text(encoding="utf-8"))
            saved["metadata"]["elapsed_seconds"] = 987654.
            path.write_text(json.dumps(saved), encoding="utf-8")
        for path in self.store.cache.glob("*.metadata.json"):
            saved = json.loads(path.read_text(encoding="utf-8"))
            saved["elapsed_seconds"] = 987654.
            path.write_text(json.dumps(saved), encoding="utf-8")
        job = self.run_job()
        self.assertEqual(len(SyntheticClient.calls), count)
        self.assertEqual(job["result"]["model_coverage"]["model"]["elapsed_seconds"], 987654.)
        self.assertLess(job["timing"]["total_seconds"], 100.)
        self.assertTrue(all(0 <= s["elapsed_seconds"] < 100. for s in job["timing"]["stages"]))

    def test_queued_job_keeps_frozen_model_and_pipeline_snapshot(self):
        gate = threading.Event()
        entered = threading.Event()
        self.addCleanup(gate.set)

        def occupy_worker():
            entered.set()
            gate.wait(5)

        self.store.executor.submit(occupy_worker)
        self.assertTrue(entered.wait(2))
        response = self.submit(model="gpt-6-luna", use_cache=False)
        identifier = response.get_json()["job_id"]
        queued = self.client.get("/api/jobs/" + identifier, base_url=BASE).get_json()
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["timing"]["stages"], [])
        time.sleep(.025)
        later = self.client.get("/api/jobs/" + identifier, base_url=BASE).get_json()
        self.assertGreater(later["timing"]["queue_seconds"], queued["timing"]["queue_seconds"])
        self.store.model_options.update(enabled=False, text_pairing=False)
        self.store.config["model_comparison"]["deployment"] = "not-approved"
        gate.set()
        done = self.wait(identifier)
        self.assertEqual(done["status"], "succeeded", done.get("error"))
        self.assertEqual(done["analysis_options"]["model"], "gpt-6-luna")
        self.assertEqual(done["result"]["model_coverage"]["model"]["model"], "gpt-6-luna")
        self.assertEqual([s["id"] for s in done["timing"]["stages"]], SYNTHETIC_STAGES)
        self.assertGreaterEqual(done["timing"]["queue_seconds"], later["timing"]["queue_seconds"])

    def test_invalid_options_and_auth_reject_without_queue_or_calls(self):
        for value in (None, 0, 1, "true", "false", [], {}):
            response = self.submit(use_cache=value)
            self.assertEqual(response.status_code, 400)
        for value in (None, 0, True, {}, [], "", "gpt-5.4", "other-deployment",
                      "https://other.example/openai/v1"):
            response = self.submit(model=value)
            self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/compare", base_url=BASE,
                                    json={"revision": self.revision, "use_cache": False})
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            "/api/compare", base_url=BASE, json={"revision": self.revision},
            headers={**self.headers, "Origin": "https://attacker.example"})
        self.assertEqual(response.status_code, 403)
        self.store.allow_azure = False
        response = self.submit(use_cache=False)
        self.assertEqual(response.status_code, 409)
        self.assertIn("cache-only", response.get_json()["error"])
        self.assertEqual(self.store.jobs, {})
        self.assertEqual(SyntheticClient.instances, [])
        self.assertEqual(SyntheticClient.calls, [])

    def test_live_timing_and_snapshot_then_persisted_completion(self):
        SyntheticClient.model_gate = threading.Event()
        SyntheticClient.model_entered = threading.Event()
        response = self.submit(model="gpt-6-luna", use_cache=False)
        identifier = response.get_json()["job_id"]
        self.assertTrue(SyntheticClient.model_entered.wait(3))
        first = self.client.get("/api/jobs/" + identifier, base_url=BASE).get_json()
        self.assertEqual(first["timing"]["stages"][-1]["id"], "model_coarse")
        self.assertEqual(first["timing"]["stages"][-1]["status"], "running")
        self.assertIn("usage_cost", first)
        self.assertNotIn("_config", first)
        self.assertNotIn("_timing", first)
        time.sleep(.025)
        second = self.client.get("/api/jobs/" + identifier, base_url=BASE).get_json()
        self.assertGreater(second["timing"]["total_seconds"], first["timing"]["total_seconds"])
        self.assertGreater(second["timing"]["stages"][-1]["elapsed_seconds"],
                           first["timing"]["stages"][-1]["elapsed_seconds"])
        self.assertEqual(self.submit(model="gpt-6-astra").status_code, 409)
        SyntheticClient.model_gate.set()
        done = self.wait(identifier)
        self.assertEqual(done["status"], "succeeded", done.get("error"))
        self.assertEqual(done["analysis_options"]["model"], "gpt-6-luna")
        self.assertEqual([s["id"] for s in done["timing"]["stages"]], SYNTHETIC_STAGES)
        self.assertTrue(all(s["status"] == "completed" for s in done["timing"]["stages"]))
        self.assertEqual(done["timing"], done["result"]["timing"])
        self.assertGreaterEqual(done["timing"]["total_seconds"],
                                done["timing"]["queue_seconds"] +
                                sum(s["elapsed_seconds"] for s in done["timing"]["stages"]))
        self.store.executor.shutdown(wait=True)
        audit = json.loads(next((self.root / "data").rglob(identifier + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(audit["timing"], done["timing"])
        self.assertEqual(audit["analysis_options"], done["analysis_options"])
        self.assertEqual(audit["usage_cost"], done["usage_cost"])
        self.assertEqual(Path(audit["cache_namespace"]),
                         self.store.cache / "uncached-jobs" / identifier)

    def test_failed_model_preserves_guard_usage_and_actual_elapsed(self):
        SyntheticClient.model_failure = True
        response = self.submit(use_cache=False, model="gpt-6-luna")
        identifier = response.get_json()["job_id"]
        done = self.wait(identifier)
        self.assertEqual(done["status"], "failed")
        self.assertNotIn("result", done)
        self.assertEqual(done["timing"]["stages"][-1]["status"], "failed")
        self.assertEqual(done["timing"]["stages"][-1]["id"], "model_coarse")
        self.assertEqual(done["usage_cost"]["summary"]["requests"]["unknown"], 1)
        namespace = self.store.cache / "uncached-jobs" / identifier
        markers = list((namespace / "model-comparison").glob("*.pending.json"))
        self.assertEqual(len(markers), 1)
        client = SyntheticClient.instances[-1]
        count = len(SyntheticClient.calls)
        with self.assertRaisesRegex(CUError, "pending"):
            complete_json(client, namespace, body(client))
        self.assertEqual(len(SyntheticClient.calls), count)
        self.assertTrue(markers[0].exists())
        self.store.executor.shutdown(wait=True)
        audit = json.loads(next((self.root / "data").rglob(identifier + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(audit["timing"], done["timing"])
        self.assertEqual(audit["analysis_options"]["model"], "gpt-6-luna")

    def test_stale_job_retains_usage_and_timing(self):
        SyntheticClient.model_gate = threading.Event()
        SyntheticClient.model_entered = threading.Event()
        response = self.submit()
        identifier = response.get_json()["job_id"]
        self.assertTrue(SyntheticClient.model_entered.wait(3))
        self.upload("old", "REPLACED")
        SyntheticClient.model_gate.set()
        self.store.executor.shutdown(wait=True)
        done = self.wait(identifier)
        self.assertEqual(done["status"], "stale")
        self.assertNotIn("result", done)
        self.assertGreater(done["timing"]["total_seconds"], 0)
        self.assertEqual(done["timing"]["stages"][-1]["status"], "failed")
        self.assertGreater(done["usage_cost"]["summary"]["requests"]["new"], 0)


if __name__ == "__main__":
    unittest.main()
