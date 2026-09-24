import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from cu_diff.client import CacheMiss, Client, CUError, canonical, digest
from cu_diff.parallel import cu_workers, parallel_cu_enabled, run_cu_pair
from cu_diff.usage import begin_usage, finish_usage, _notify


def configuration(workers=2):
    return {
        "endpoint": "https://synthetic.services.ai.azure.com",
        "completion_model": "synthetic",
        "model_deployments": {"synthetic": "deployment"},
        "deployment_versions": {"deployment": "synthetic:1:GlobalStandard"},
        "processing_location": "geography",
        "performance": {"cu_workers": workers},
    }


def contexts():
    return {"old": {"stage": "cu_full_old"}, "new": {"stage": "cu_full_new"}}


class ParallelTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / (".parallel-test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.client = Client(configuration())
        self.client.usage_context = {"stage": "main"}
        self.coordinator = threading.get_ident()

    def test_parallel_overlap_scoped_contexts_unique_ids_and_detached_snapshots(self):
        barrier = threading.Barrier(2)
        notifications, progress = [], []
        snapshots = []

        def observer(snapshot):
            snapshots.append(snapshot)
            notifications.append(copy.deepcopy(snapshot))
            # Mutations made by an observer must never change the client journal.
            snapshot[0]["metadata"]["observer_mutation"] = True

        self.client.usage_observer = observer
        expected = {"old": {"stage": "cu_crop_old", "region_index": 4},
                    "new": {"stage": "cu_crop_new", "region_index": 4}}

        def task(side):
            self.assertNotEqual(threading.get_ident(), self.coordinator)
            barrier.wait(3)
            entry = begin_usage(self.client, "cu", side, "new", {"source": side})
            barrier.wait(3)
            finish_usage(self.client, entry, {"usage": {"documentPagesStandard": 1}},
                         outcome="poll_response")
            _notify(self.client)
            finish_usage(self.client, entry, {"usage": {"documentPagesStandard": 2}})
            return self.client.usage_context.copy()

        def changed(side, status):
            self.assertEqual(threading.get_ident(), self.coordinator)
            progress.append((side, status))

        result = run_cu_pair(
            self.client, {side: lambda side=side: task(side) for side in expected},
            contexts=expected, progress=changed)
        self.assertEqual(list(result), ["old", "new"])
        self.assertEqual(result, expected)
        self.assertEqual(self.client.usage_context, {"stage": "main"})
        self.assertEqual(len(notifications), 8)
        self.assertEqual(len({id(snapshot) for snapshot in snapshots}), 8)
        self.assertEqual([len(n) for n in notifications], sorted(len(n) for n in notifications))
        self.assertEqual({entry["id"] for entry in self.client.usage_records}, {"U001", "U002"})
        for entry in self.client.usage_records:
            self.assertEqual(entry["stage"], expected[entry["cache_key"]]["stage"])
            self.assertEqual(entry["region_index"], 4)
            self.assertNotIn("observer_mutation", entry["metadata"])
            self.assertEqual(entry["usage"]["documentPagesStandard"], 2)
        self.assertEqual({s: [status for side, status in progress if side == s]
                          for s in expected},
                         {"old": ["running", "completed"], "new": ["running", "completed"]})
        self.assertIsNone(notifications[0][0]["usage"])
        self.assertEqual(len(notifications[-1]), 2)

    def test_failure_drains_sibling_and_keeps_usage_before_raising(self):
        barrier = threading.Barrier(2)
        failed = threading.Event()
        finished = threading.Event()
        progress = []

        def old():
            entry = begin_usage(self.client, "cu", "old", "new", {})
            barrier.wait(3)
            finish_usage(self.client, entry, outcome="request_failed_or_unknown", cache_state="unknown")
            failed.set()
            raise CUError("Synthetic failure")

        def new():
            entry = begin_usage(self.client, "cu", "new", "new", {})
            barrier.wait(3)
            self.assertTrue(failed.wait(3))
            time.sleep(.03)
            finish_usage(self.client, entry, {"usage": {"documentPagesStandard": 7}})
            finished.set()
            return "must not return partial result"

        with self.assertRaisesRegex(CUError, "Synthetic failure"):
            run_cu_pair(self.client, {"old": old, "new": new}, contexts=contexts(),
                        progress=lambda side, status: progress.append((side, status)))
        self.assertTrue(finished.is_set())
        self.assertIn(("old", "failed"), progress)
        self.assertIn(("new", "completed"), progress)
        entries = {entry["stage"]: entry for entry in self.client.usage_records}
        self.assertEqual(entries["cu_full_old"]["cache_state"], "unknown")
        self.assertEqual(entries["cu_full_new"]["usage"]["documentPagesStandard"], 7)

    def test_config_one_runs_serially_and_still_drains_failed_pair(self):
        self.client.config["performance"]["cu_workers"] = 1
        calls = []

        def task(side):
            self.assertEqual(threading.get_ident(), self.coordinator)
            calls.append((side, self.client.usage_context.copy()))
            if side == "old":
                raise CUError("first failed")
            return side

        with self.assertRaisesRegex(CUError, "first failed"):
            run_cu_pair(self.client, {side: lambda side=side: task(side) for side in contexts()},
                        contexts=contexts())
        self.assertEqual(calls, [("old", {"stage": "cu_full_old"}),
                                 ("new", {"stage": "cu_full_new"})])
        self.assertEqual(self.client.usage_context, {"stage": "main"})

    def test_duck_typed_client_explicit_serial_fallback_and_context_restore(self):
        for client in (SimpleNamespace(), SimpleNamespace(usage_context={"stage": "saved"}),
                       SimpleNamespace(supports_parallel_cu=True)):
            existed = hasattr(client, "usage_context")
            previous = copy.deepcopy(getattr(client, "usage_context", None))
            threads = []

            def task():
                threads.append(threading.get_ident())
                return client.usage_context.copy()

            result = run_cu_pair(client, {"old": task, "new": task}, contexts=contexts())
            self.assertEqual(result, contexts())
            self.assertEqual(threads, [self.coordinator] * 2)
            self.assertEqual(hasattr(client, "usage_context"), existed)
            self.assertEqual(getattr(client, "usage_context", None), previous)

    def test_maximum_two_workers_and_result_input_order(self):
        barrier = threading.Barrier(2)
        counter_lock = threading.Lock()
        active, maximum = 0, 0
        task_contexts = {side: {"stage": side} for side in ("a", "b", "c", "d")}

        def task(side):
            nonlocal active, maximum
            with counter_lock:
                active += 1
                maximum = max(active, maximum)
            barrier.wait(3)
            time.sleep(.01)
            with counter_lock:
                active -= 1
            return side

        result = run_cu_pair(self.client,
                             {side: lambda side=side: task(side) for side in task_contexts},
                             contexts=task_contexts)
        self.assertEqual(maximum, 2)
        self.assertEqual(list(result), list(task_contexts))

    def test_unconfigured_mock_client_runs_serially_with_real_usage_journal(self):
        client = Mock()

        def task(side):
            self.assertEqual(threading.get_ident(), self.coordinator)
            entry = begin_usage(client, "cu", side, "new", {})
            finish_usage(client, entry, {"usage": {"documentPagesStandard": 1}})
            return side

        result = run_cu_pair(client, {side: lambda side=side: task(side) for side in contexts()},
                             contexts=contexts())
        self.assertEqual(result, {"old": "old", "new": "new"})
        self.assertEqual([entry["stage"] for entry in client.usage_records],
                         ["cu_full_old", "cu_full_new"])
        self.assertEqual([entry["id"] for entry in client.usage_records], ["U001", "U002"])

    def test_strict_configuration_and_task_validation_happens_before_work(self):
        self.assertEqual(cu_workers({}), 2)
        self.assertEqual(cu_workers({"performance": {}}), 2)
        self.assertEqual(cu_workers(configuration(1)), 1)
        for value in (None, True, False, 0, 3, 2., "2", [], {}):
            with self.subTest(workers=value), self.assertRaises(ValueError):
                cu_workers(configuration(value))
        for value in (None, [], "", 2, True):
            with self.assertRaises(ValueError):
                cu_workers({"performance": value})
        with self.assertRaises(ValueError):
            cu_workers(None)
        self.client.config["performance"]["cu_workers"] = True
        with self.assertRaises(ValueError):
            run_cu_pair(self.client, {"old": lambda: self.fail("must not run")},
                        contexts={"old": {}})
        self.client.config["performance"]["cu_workers"] = 2
        with self.assertRaises(ValueError):
            run_cu_pair(self.client, {"old": lambda: self.fail("must not run")}, contexts={})
        self.assertEqual(run_cu_pair(self.client, {}, contexts={}), {})

    def test_public_execution_mode_uses_same_validation_and_capability(self):
        self.assertTrue(parallel_cu_enabled(self.client))
        self.client.config["performance"]["cu_workers"] = 1
        self.assertFalse(parallel_cu_enabled(self.client))
        self.client.config["performance"]["cu_workers"] = 2
        self.client.supports_parallel_cu = False
        self.assertFalse(parallel_cu_enabled(self.client))
        self.assertFalse(parallel_cu_enabled(SimpleNamespace()))
        self.assertFalse(parallel_cu_enabled(Mock()))
        self.assertFalse(parallel_cu_enabled(SimpleNamespace(supports_parallel_cu=True)))
        self.client.config["performance"]["cu_workers"] = True
        with self.assertRaises(ValueError):
            parallel_cu_enabled(self.client)

    def test_usage_scope_nested_and_exception_restores_main_context(self):
        original = {"stage": "outer", "nested": {"v": 1}}
        with self.client.usage_scope(original):
            original["nested"]["v"] = 2
            self.assertEqual(self.client.usage_context["nested"]["v"], 1)
            with self.assertRaisesRegex(CUError, "stop"):
                with self.client.usage_scope({"stage": "inner"}):
                    raise CUError("stop")
            self.assertEqual(self.client.usage_context["stage"], "outer")
        self.assertEqual(self.client.usage_context, {"stage": "main"})

    def test_duplicate_content_single_post_same_cache_key_and_cached_replay(self):
        paths = {side: self.root / f"{side}.pdf" for side in contexts()}
        for path in paths.values():
            path.write_bytes(b"same synthetic document")
        barrier = threading.Barrier(2)
        response = {"status": "Succeeded", "result": {"contents": [{"markdown": "synthetic"}]},
                    "usage": {"documentPagesStandard": 1}}
        cache = self.root / "cache"
        posted = []

        def request(method, url, body=None):
            self.assertEqual(method, "POST")
            posted.append(copy.deepcopy(body))
            return {}, {"Operation-Location": self.client.url("analyzerResults/synthetic")}

        def poll(*args, **kwargs):
            time.sleep(.03)
            return copy.deepcopy(response)

        def task(side):
            barrier.wait(3)
            return self.client.analyze(paths[side], cache, "synthetic", {})

        with patch.object(self.client, "request", side_effect=request), patch.object(
                self.client, "poll", side_effect=poll):
            results = run_cu_pair(
                self.client, {side: lambda side=side: task(side) for side in paths},
                contexts=contexts())
        self.assertEqual(len(posted), 1)
        self.assertEqual(set(posted[0]), {"inputs", "modelDeployments"})
        self.assertEqual(results["old"][1]["cache_key"], results["new"][1]["cache_key"])
        self.assertEqual(sorted(result[1]["cache_hit"] for result in results.values()), [False, True])
        self.assertEqual(len(list(cache.glob("*.response.json"))), 1)
        self.assertEqual(len(list(cache.glob("*.metadata.json"))), 1)
        self.assertEqual(list(cache.glob("*.tmp")), [])
        self.assertEqual(list(cache.glob("*.operation.json")), [])
        self.assertEqual({entry["stage"] for entry in self.client.usage_records},
                         {"cu_full_old", "cu_full_new"})
        metadata = json.loads(next(cache.glob("*.metadata.json")).read_text(encoding="utf-8"))
        provenance = {key: metadata[key] for key in (
            "document_sha256", "byte_length", "endpoint", "api_version", "analyzer_id",
            "analyzer", "model_deployments", "selected_completion_model",
            "deployment_versions", "processing_location")}
        self.assertEqual(metadata["cache_key"], digest(canonical(provenance)))
        with patch.object(self.client, "request") as request:
            cached = run_cu_pair(self.client, {
                side: lambda side=side: self.client.analyze(
                    paths[side], cache, "synthetic", {}, allow_submit=False)
                for side in paths}, contexts=contexts())
            request.assert_not_called()
        self.assertTrue(all(result[1]["cache_hit"] for result in cached.values()))

    def test_duplicate_ambiguous_post_is_not_retried_by_waiting_sibling(self):
        path = self.root / "synthetic.pdf"
        path.write_bytes(b"same synthetic document")
        barrier = threading.Barrier(2)

        def task():
            barrier.wait(3)
            return self.client.analyze(path, self.root / "cache", "synthetic", {})

        with patch.object(self.client, "request", side_effect=CUError("ambiguous")) as request:
            with self.assertRaises(CUError):
                run_cu_pair(self.client, {"old": task, "new": task}, contexts=contexts())
            request.assert_called_once()
        self.assertEqual(len(self.client.usage_records), 1)
        self.assertEqual(self.client.usage_records[0]["cache_state"], "unknown")

    def test_cache_only_misses_never_submit_and_drain_both_sides(self):
        path = self.root / "synthetic.pdf"
        path.write_bytes(b"synthetic document")
        progress = []

        def task():
            return self.client.analyze(path, self.root / "cache", "synthetic", {}, allow_submit=False)

        with patch.object(self.client, "request") as request:
            with self.assertRaises(CacheMiss):
                run_cu_pair(self.client, {"old": task, "new": task}, contexts=contexts(),
                            progress=lambda side, status: progress.append((side, status)))
            request.assert_not_called()
        self.assertEqual({side for side, status in progress if status == "failed"}, {"old", "new"})
        self.assertEqual(self.client.usage_records, [])

    def test_auth_refresh_is_serialized_and_cached_without_exposing_token(self):
        barrier = threading.Barrier(2)

        def authenticate():
            barrier.wait(3)
            return self.client._authenticate()

        def result(*args, **kwargs):
            time.sleep(.03)
            return SimpleNamespace(returncode=0, stdout="synthetic-not-a-real-token")

        with patch("cu_diff.client.shutil.which", return_value="synthetic-az"), patch(
                "cu_diff.client.subprocess.run", side_effect=result) as refresh:
            with ThreadPoolExecutor(max_workers=2) as executor:
                values = [future.result() for future in [executor.submit(authenticate),
                                                        executor.submit(authenticate)]]
            refresh.assert_called_once()
            self.assertEqual(values[0], values[1])
        self.assertEqual(self.client.events, [])

    def test_observer_does_not_hold_journal_lock_and_supports_reentry(self):
        snapshots = []

        def observer(snapshot):
            acquired = []

            def lock_probe():
                with self.client._usage_lock:
                    acquired.append(True)

            thread = threading.Thread(target=lock_probe)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive(), "Observer called while the journal lock was held")
            self.assertEqual(acquired, [True])
            snapshots.append(snapshot)
            if len(snapshots) == 1:
                begin_usage(self.client, "cu", "reentrant", "cached", {})

        self.client.usage_observer = observer
        begin_usage(self.client, "cu", "first", "new", {})
        self.assertEqual([len(snapshot) for snapshot in snapshots], [1, 2])
        self.assertEqual([entry["id"] for entry in self.client.usage_records], ["U001", "U002"])

    def test_progress_exception_joins_already_submitted_work(self):
        completed = threading.Event()
        statuses = []

        def task():
            time.sleep(.03)
            completed.set()
            return "done"

        def progress(side, status):
            statuses.append((side, status))
            if side == "new" and status == "running":
                raise CUError("progress stopped")

        with self.assertRaisesRegex(CUError, "progress stopped"):
            run_cu_pair(self.client, {"old": task, "new": task}, contexts=contexts(),
                        progress=progress)
        self.assertTrue(completed.is_set())
        self.assertEqual(statuses, [("old", "running"), ("new", "running"), ("old", "completed")])

    def test_completion_progress_exception_drains_both_started_workers(self):
        barrier = threading.Barrier(2)
        slow_finished = threading.Event()
        statuses = []

        def old():
            barrier.wait(3)
            return "old"

        def new():
            barrier.wait(3)
            time.sleep(.04)
            entry = begin_usage(self.client, "cu", "new", "new", {})
            finish_usage(self.client, entry, {"usage": {"documentPagesStandard": 1}})
            slow_finished.set()
            return "new"

        def progress(side, status):
            self.assertEqual(threading.get_ident(), self.coordinator)
            statuses.append((side, status))
            if side == "old" and status == "completed":
                raise CUError("completion observer failed")

        with self.assertRaisesRegex(CUError, "completion observer failed"):
            run_cu_pair(self.client, {"old": old, "new": new}, contexts=contexts(),
                        progress=progress)
        self.assertTrue(slow_finished.is_set())
        self.assertEqual(self.client.usage_records[-1]["stage"], "cu_full_new")
        self.assertEqual(self.client.usage_records[-1]["usage"]["documentPagesStandard"], 1)
        self.assertIn(("new", "completed"), statuses)

    def test_serial_cancellation_before_new_preserves_order_and_does_not_start_new(self):
        self.client.config["performance"]["cu_workers"] = 1
        calls, statuses = [], []

        def task(side):
            calls.append(side)
            return side

        def progress(side, status):
            statuses.append((side, status))
            if side == "new" and status == "running":
                raise CUError("invalidated before new")

        with self.assertRaisesRegex(CUError, "invalidated before new"):
            run_cu_pair(self.client, {side: lambda side=side: task(side) for side in contexts()},
                        contexts=contexts(), progress=progress)
        self.assertEqual(calls, ["old"])
        self.assertEqual(statuses, [("old", "running"), ("old", "completed"), ("new", "running")])


if __name__ == "__main__":
    unittest.main()
