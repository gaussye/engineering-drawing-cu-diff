import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cu_diff.client import CacheMiss, Client, CUError
from cu_diff.model_client import complete_json
from cu_diff.usage import begin_usage, finish_usage, usage_report, validate_pricing


def configuration():
    return {
        "endpoint": "https://synthetic.services.ai.azure.com",
        "completion_model": "synthetic",
        "model_deployments": {"synthetic": "deployment"},
        "deployment_versions": {"deployment": "synthetic:1:GlobalStandard"},
        "processing_location": "geography",
    }


def prices():
    base = {"currency": "USD", "region": "westus", "as_of": "2026-01-01",
            "source": "https://prices.azure.com/"}
    rates = []
    for key, price, unit, basis in (
        ("cu.documentPagesStandard", 5, "pages", 1000),
        ("cu.contextualizationTokens", .001, "tokens", 1000),
        ("model.synthetic.input", 2, "tokens", 1000000),
        ("model.synthetic.cached_input", .2, "tokens", 1000000),
        ("model.synthetic.output", 4, "tokens", 1000000),
        ("model.synthetic.cache_write", 2.5, "tokens", 1000000),
    ):
        rate = dict(base, key=key, price=price, unit=unit, unit_quantity=basis)
        if key.startswith("model."):
            rate.update(sku="GlobalStandard", model_version="1")
        rates.append(rate)
    return {"currency": "USD", "region": "westus", "rates": rates}


def record(service="model", state="new", key="synthetic", **changes):
    meta = dict(configuration(), selected_completion_model="synthetic")
    meta.update(model="synthetic", deployment="deployment", deployment_version="synthetic:1:GlobalStandard")
    usage = ({"documentPagesStandard": 1, "contextualizationTokens": 1000,
              "tokens": {"synthetic-input": 1000, "synthetic-cached-input": 100, "synthetic-output": 200}}
             if service == "cu" else
             {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200,
              "prompt_tokens_details": {"cached_tokens": 100},
              "completion_tokens_details": {"reasoning_tokens": 150}})
    return dict({"id": "U001", "service": service, "cache_key": key, "cache_state": state,
                 "stage": "model_fine", "region_index": 1, "metadata": meta,
                 "usage": usage, "outcome": "response_received"}, **changes)


class AccountingTests(unittest.TestCase):
    def test_direct_cached_and_reasoning_tokens_are_not_added_twice(self):
        report = usage_report([record()], prices())
        current = report["summary"]["current"]
        self.assertEqual(current["model_tokens"], 1200)
        self.assertEqual(current["input_tokens"], 1000)
        self.assertEqual(current["cached_input_tokens"], 100)
        self.assertAlmostEqual(current["estimated_cost"], .00262)
        self.assertEqual(report["status"], "complete")

    def test_cu_cost_has_extraction_context_and_internal_model(self):
        for semantics, expected, tokens in ((True, .00862, 1200), (False, .00882, 1300)):
            with self.subTest(semantics=semantics):
                report = usage_report([record("cu")], dict(prices(), cu_input_includes_cached=semantics))
                self.assertAlmostEqual(report["summary"]["current"]["estimated_cost"], expected)
                self.assertEqual(report["summary"]["current"]["model_tokens"], tokens)
                self.assertEqual({m["category"] for m in report["entries"][0]["meters"]},
                                 {"cu_extraction", "cu_contextualization", "cu_model"})

    def test_unverified_cu_cache_semantics_uses_range_not_fake_total(self):
        report = usage_report([record("cu")], prices())
        current = report["summary"]["current"]
        self.assertIsNone(current["input_tokens"])
        self.assertIsNone(current["estimated_cost"])
        self.assertAlmostEqual(current["estimated_cost_range"]["min"], .00862)
        self.assertAlmostEqual(current["estimated_cost_range"]["max"], .00882)
        self.assertEqual(report["entries"][0]["raw_usage"]["tokens"]["synthetic-input"], 1000)

    def test_local_cache_is_free_and_references_are_deduplicated(self):
        entries = [record(state="cached"), record(state="cached"), record(state="resumed", key="second")]
        report = usage_report(entries, prices())
        self.assertEqual(report["summary"]["current"]["estimated_cost"], 0)
        self.assertEqual(report["summary"]["current"]["model_tokens"], 0)
        self.assertEqual(report["summary"]["reused"]["model_tokens"], 2400)
        self.assertFalse(report["entries"][1]["counted_in_summary"])
        self.assertEqual(report["summary"]["requests"], {"new": 0, "cached": 2, "resumed": 1, "unknown": 0})

    def test_current_request_reused_in_same_run_is_not_historical_charge(self):
        report = usage_report([record(state="cached"), record(), record(state="cached")], prices())
        self.assertEqual(report["summary"]["current"]["model_tokens"], 1200)
        self.assertEqual(report["summary"]["reused"]["model_tokens"], 0)

    def test_missing_usage_and_unavailable_journal_never_imply_free(self):
        for entries in (None, [record(usage=None)], [record(state="unknown", usage={})]):
            current = usage_report(entries, prices())["summary"]["current"]
            self.assertIsNone(current["estimated_cost"])
            self.assertIsNone(current["estimated_cost_range"])
            self.assertIsNone(current["input_tokens"])

    def test_missing_prices_do_not_erase_reported_usage(self):
        report = usage_report([record()])
        self.assertEqual(report["summary"]["current"]["model_tokens"], 1200)
        self.assertIsNone(report["summary"]["current"]["estimated_cost"])
        self.assertEqual(report["summary"]["current"]["unpriced_meters"], 3)

    def test_zero_quantity_needs_no_price(self):
        entry = record(usage={"prompt_tokens": 0, "completion_tokens": 0})
        self.assertEqual(usage_report([entry])["summary"]["current"]["estimated_cost"], 0)

    def test_unknown_meters_and_invalid_values_are_not_ignored(self):
        for mutate in (
            lambda u: u.update(newMeter=17),
            lambda u: u.update(documentPagesStandard=-1),
            lambda u: u.update(documentPagesStandard=10**400),
            lambda u: u["tokens"].update({"synthetic-output": float("nan")}),
            lambda u: u["tokens"].update({"unrecognized-type": 10}),
        ):
            entry = record("cu")
            mutate(entry["usage"])
            report = usage_report([entry], prices())
            json.dumps(report, allow_nan=False)
            current = report["summary"]["current"]
            self.assertIsNone(current["estimated_cost"])
            self.assertIsNone(current["estimated_cost_range"])

    def test_cache_write_overlap_is_not_assumed(self):
        entry = record()
        entry["usage"]["prompt_tokens_details"]["cache_write_tokens"] = 20
        report = usage_report([entry], prices())
        self.assertEqual(report["summary"]["current"]["cache_write_tokens"], 20)
        self.assertEqual(report["summary"]["current"]["model_tokens"], 1200)
        self.assertIsNone(report["summary"]["current"]["estimated_cost"])
        self.assertIn("cache_write", " ".join(m["key"] for m in report["entries"][0]["meters"]))

    def test_version_region_and_sku_mismatches_are_unpriced(self):
        for field, value in (("region", "eastus"), ("sku", "DataZoneStandard"), ("model_version", "other")):
            config = prices()
            for rate in config["rates"]:
                rate[field] = value
            self.assertIsNone(usage_report([record()], config)["summary"]["current"]["estimated_cost"])

    def test_mismatched_actual_model_is_unpriced(self):
        entry = record()
        entry["metadata"]["response_model"] = "other-version"
        self.assertIsNone(usage_report([entry], prices())["summary"]["current"]["estimated_cost"])

    def test_total_mismatch_and_invalid_cached_subset_are_explicit(self):
        for change in ({"total_tokens": 999}, {"prompt_tokens_details": {"cached_tokens": 2000}}):
            entry = record()
            entry["usage"].update(change)
            self.assertIsNone(usage_report([entry], prices())["summary"]["current"]["estimated_cost"])

    def test_partial_poll_usage_is_preserved_but_not_final_bill(self):
        report = usage_report([record(outcome="operation_incomplete")], prices())
        self.assertIsNone(report["summary"]["current"]["estimated_cost"])
        self.assertGreater(report["summary"]["current"]["known_cost"], 0)

    def test_bundled_snapshot_and_astra_tier_range(self):
        config = {"snapshot": "azure-retail-westus-2026-09-23", "region": "westus"}
        self.assertEqual(len(validate_pricing(config)["rates"]), 19)
        entry = record(usage={"prompt_tokens": 1000, "completion_tokens": 200})
        entry["metadata"].update(model="gpt-6-astra", deployment_version="gpt-6-astra:2026-09-03:GlobalStandard")
        current = usage_report([entry], config)["summary"]["current"]
        self.assertIsNone(current["estimated_cost"])
        self.assertAlmostEqual(current["estimated_cost_range"]["min"], .02)
        self.assertAlmostEqual(current["estimated_cost_range"]["max"], .035)

    def test_cu_aggregate_cannot_select_long_context_price(self):
        config = {"snapshot": "azure-retail-westus-2026-09-23", "region": "westus"}
        entry = record("cu", usage={"documentPagesStandard": 1, "contextualizationTokens": 1000,
                                   "tokens": {"gpt-5.4-input": 300000, "gpt-5.4-output": 20}})
        entry["metadata"].update(model_deployments={"gpt-5.4": "d"},
                                 deployment_versions={"d": "gpt-5.4:2026-03-05:GlobalStandard"})
        self.assertIsNone(usage_report([entry], config)["summary"]["current"]["estimated_cost"])
        entry["usage"]["tokens"]["gpt-5.4-input"] = 1000
        self.assertIsNotNone(usage_report([entry], config)["summary"]["current"]["estimated_cost"])

    def test_invalid_pricing_rejected_before_work(self):
        for change in ({"price": -1}, {"price": float("nan")}, {"price": True},
                       {"unit_quantity": 0}, {"currency": "CNY"}, {"source": "javascript:alert(1)"},
                       {"source": "https://user:password@example.com"}, {"unit": "unknown"},
                       {"min_input_tokens": 3, "max_input_tokens": 2}):
            config = prices()
            config["rates"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_pricing(config)
        with self.assertRaises(ValueError):
            validate_pricing({"snapshot": "../../unknown"})


class TransportJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pdf = self.root / "synthetic.pdf"
        self.pdf.write_bytes(b"synthetic fixture")
        self.client = Client(configuration())

    def test_observer_copies_usage_and_stage(self):
        observations = []
        self.client.usage_observer = lambda records: observations.append(copy.deepcopy(records))
        self.client.usage_context = {"stage": "model_fine", "region_index": 2}
        entry = begin_usage(self.client, "model", "key", "new", {})
        raw = {"usage": {"prompt_tokens": 10}}
        finish_usage(self.client, entry, raw)
        raw["usage"]["prompt_tokens"] = 99
        self.assertEqual(entry["usage"]["prompt_tokens"], 10)
        self.assertIsNone(observations[0][0]["usage"])
        self.assertEqual(entry["region_index"], 2)

    def test_cu_new_and_cache_journal_without_changing_fingerprint(self):
        raw = {"status": "Succeeded", "usage": record("cu")["usage"], "result": {"contents": [{}]}}
        with patch.object(self.client, "request", return_value=({}, {"Operation-Location": "operation"})) as request:
            with patch.object(self.client, "poll", return_value=raw):
                self.client.analyze(self.pdf, self.root, "test", {})
            self.client.config["pricing"] = prices()
            self.client.analyze(self.pdf, self.root, "test", {}, allow_submit=False)
        self.assertEqual(request.call_count, 1)
        self.assertEqual([r["cache_state"] for r in self.client.usage_records], ["new", "cached"])

    def test_later_missing_usage_does_not_erase_polled_counts(self):
        entry = begin_usage(self.client, "model", "key", "new", record()["metadata"])
        finish_usage(self.client, entry, {"usage": record()["usage"]})
        finish_usage(self.client, entry, {})
        report = usage_report(self.client.usage_records, prices())
        self.assertEqual(report["summary"]["current"]["model_tokens"], 1200)
        self.assertIsNone(report["summary"]["current"]["estimated_cost"])
        finish_usage(self.client, entry, {"usage": record()["usage"]})
        self.assertIsNotNone(usage_report(self.client.usage_records, prices())["summary"]["current"]["estimated_cost"])

    def test_cu_missing_content_still_records_consumption(self):
        with patch.object(self.client, "request", return_value=({}, {"Operation-Location": "operation"})):
            with patch.object(self.client, "poll", return_value={"status": "Succeeded", "usage": record("cu")["usage"]}):
                with self.assertRaises(CUError):
                    self.client.analyze(self.pdf, self.root, "test", {})
        self.assertEqual(self.client.usage_records[0]["usage"]["documentPagesStandard"], 1)

    def test_cu_failed_poll_preserves_returned_usage_and_resume_is_not_new(self):
        response = {"status": "Failed", "usage": record("cu")["usage"], "error": {"code": "Synthetic"}}
        with patch.object(self.client, "request", side_effect=[
            ({}, {"Operation-Location": "operation"}), (response, {})
        ]):
            with self.assertRaises(CUError):
                self.client.analyze(self.pdf, self.root, "test", {})
        self.assertEqual(self.client.usage_records[0]["usage"], response["usage"])
        next_client = Client(configuration())
        response.update(status="Succeeded", result={"contents": [{}]})
        with patch.object(next_client, "poll", return_value=response):
            next_client.analyze(self.pdf, self.root, "test", {}, allow_submit=False)
        self.assertEqual(next_client.usage_records[0]["cache_state"], "resumed")
        self.assertEqual(usage_report(next_client.usage_records)["summary"]["current"]["estimated_cost"], 0)

    def test_cu_cache_miss_is_not_billed_attempt(self):
        with self.assertRaises(CacheMiss):
            self.client.analyze(self.pdf, self.root, "test", {}, allow_submit=False)
        self.assertEqual(self.client.usage_records, [])

    def test_transport_exception_is_preserved_and_consumption_is_unknown(self):
        with patch.object(self.client, "request", side_effect=TimeoutError("synthetic")):
            with self.assertRaises(TimeoutError):
                self.client.analyze(self.pdf, self.root, "test", {})
        report = usage_report(self.client.usage_records, prices())
        self.assertEqual(report["summary"]["requests"]["unknown"], 1)
        self.assertIsNone(report["summary"]["current"]["estimated_cost"])

    def test_failed_model_content_and_cache_preserve_usage(self):
        body = {"model": "deployment", "messages": [{"role": "user", "content": "synthetic"}],
                "max_completion_tokens": 100, "response_format": {"type": "json_schema", "json_schema": {
                    "name": "synthetic", "strict": True, "schema": {"type": "object"}}}}
        response = {"model": "synthetic-1", "usage": record()["usage"],
                    "choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}
        with patch.object(self.client, "request", return_value=(response, {})) as request:
            for _ in range(2):
                with self.assertRaises(CUError):
                    complete_json(self.client, self.root, body)
        self.assertEqual(request.call_count, 1)
        self.assertEqual([r["cache_state"] for r in self.client.usage_records], ["new", "cached"])
        self.assertEqual(usage_report(self.client.usage_records, prices())["summary"]["current"]["model_tokens"], 1200)


if __name__ == "__main__":
    unittest.main()
