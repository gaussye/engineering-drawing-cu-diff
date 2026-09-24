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


def luna_prices():
    example = Path(__file__).resolve().parents[1] / "config.example.json"
    return json.loads(example.read_text(encoding="utf-8"))["pricing"]


def luna_record(**changes):
    entry = record(**changes)
    entry["metadata"].update(model="gpt-6-luna", deployment="synthetic-luna",
                             deployment_version="gpt-6-luna:2026-09-22:GlobalStandard",
                             response_model="gpt-6-luna-2026-09-22")
    return entry


def latency_checkpoint():
    return {
        "engine_tbt_ms": 5, "engine_ttft_ms": 397, "engine_ttlt_ms": 21156,
        "pre_inference_ms": 726, "service_tbt_ms": 5, "service_ttft_ms": 1299,
        "service_ttlt_ms": 21993, "user_visible_ttft_ms": 572,
    }


def layout_record(**changes):
    entry = record("cu", usage={"documentPagesStandard": 1}, **changes)
    entry["metadata"] = {
        "extraction_profile": "layout", "model_deployments": {},
        "deployment_versions": {}, "selected_completion_model": None,
    }
    return entry


class CurrentBreakdownTests(unittest.TestCase):
    def pricing(self):
        pricing = prices()
        pricing["rates"] += luna_prices()["rates"]
        return pricing

    def assert_costs_add_up(self, report):
        def check(total, groups):
            self.assertEqual(set(groups), {"cu_analysis", "cu_model", "analysis_model"})
            for field in ("known_cost", "estimated_cost"):
                amounts = [group[field] for group in groups.values()]
                if None in amounts:
                    self.assertIsNone(total[field])
                else:
                    self.assertEqual(total[field], sum(amounts))
            ranges = [group["estimated_cost_range"] for group in groups.values()]
            if None in ranges:
                self.assertIsNone(total["estimated_cost_range"])
            else:
                self.assertEqual(total["estimated_cost_range"],
                                 {key: sum(bounds[key] for bounds in ranges) for key in ("min", "max")})
        check(report["summary"]["current"], report["summary"]["current_breakdown"])
        for entry in report["entries"]:
            groups = entry["current_breakdown"]
            check({"estimated_cost": entry["current_cost"],
                   "estimated_cost_range": entry["current_cost_range"],
                   "known_cost": sum(group["known_cost"] for group in groups.values())}, groups)

    def test_ten_layout_pages_and_eight_luna_calls_have_exact_three_part_total(self):
        entries = [layout_record(key=f"page-{i}") for i in range(10)]
        entries += [luna_record(key=f"model-{i}") for i in range(8)]
        original = copy.deepcopy(entries)
        report = usage_report(entries, self.pricing())
        current, groups = report["summary"]["current"], report["summary"]["current_breakdown"]
        self.assertAlmostEqual(current["estimated_cost"], .051528)
        self.assertEqual(current["cu_pages"], 10)
        self.assertEqual(current["contextualization_tokens"], 0)
        self.assertEqual(current["model_tokens"], 8*1200)
        self.assertAlmostEqual(groups["cu_analysis"]["estimated_cost"], .05)
        self.assertAlmostEqual(groups["analysis_model"]["estimated_cost"], 8*.000191)
        self.assertEqual(groups["cu_model"]["estimated_cost"], 0)
        self.assertEqual(groups["cu_model"]["status"], "not_applicable")
        self.assertIn("layout", groups["cu_model"]["explanation"])
        for entry in report["entries"][:10]:
            self.assertEqual([m["key"] for m in entry["meters"]], ["cu.documentPagesStandard"])
            self.assertEqual(entry["current_breakdown"]["cu_model"]["status"], "not_applicable")
        self.assertEqual(entries, original)
        self.assertEqual([entry["raw_usage"] for entry in report["entries"]], [entry["usage"] for entry in original])
        self.assert_costs_add_up(report)

    def test_layout_plus_astra_range_has_no_artificial_missing_cu_costs(self):
        pricing = self.pricing()
        for tier, values in (("short", (10, 1, 50)), ("long", (20, 2, 75))):
            for kind, price in zip(("input", "cached_input", "output"), values):
                pricing["rates"].append({
                    "key": f"model.gpt-6-astra.{kind}", "price": price, "unit_quantity": 1_000_000,
                    "unit": "tokens", "currency": "USD", "as_of": "2026-09-24",
                    "source": "https://example.com/synthetic-astra", "context_tier": tier,
                })
        entries = [layout_record(key=f"page-{i}") for i in range(10)]
        for i in range(8):
            entry = record(key=f"astra-{i}")
            entry["metadata"].update(model="gpt-6-astra", deployment_version="gpt-6-astra:1:GlobalStandard")
            entries.append(entry)
        report = usage_report(entries, pricing)
        current, groups = report["summary"]["current"], report["summary"]["current_breakdown"]
        self.assertIsNone(current["estimated_cost"])
        self.assertAlmostEqual(current["estimated_cost_range"]["min"], .2028)
        self.assertAlmostEqual(current["estimated_cost_range"]["max"], .3156)
        self.assertEqual(groups["cu_model"]["status"], "not_applicable")
        self.assertEqual(groups["analysis_model"]["status"], "partial")
        self.assertEqual(groups["analysis_model"]["unpriced_meters"], 24)
        self.assertEqual(current["unknown_usage_calls"], 0)
        self.assert_costs_add_up(report)

    def test_legacy_gpt54_internal_model_is_separate_from_selected_analysis_model(self):
        pricing = self.pricing()
        pricing["cu_input_includes_cached"] = True
        for kind, price in (("input", 2.5), ("cached_input", .25), ("output", 15)):
            pricing["rates"].append({
                "key": f"model.gpt-5.4.{kind}", "price": price, "unit_quantity": 1_000_000,
                "unit": "tokens", "currency": "USD", "as_of": "2026-09-24",
                "source": "https://example.com/synthetic-gpt54",
            })
        legacy = record("cu")
        legacy["usage"]["tokens"] = {
            "gpt-5.4-input": 1000, "gpt-5.4-cached-input": 100, "gpt-5.4-output": 200,
        }
        legacy["metadata"].update(selected_completion_model="gpt-5.4",
                                  model_deployments={"gpt-5.4": "legacy-cu"},
                                  deployment_versions={"legacy-cu": "gpt-5.4:2026-03-05:GlobalStandard"})
        report = usage_report([layout_record(key="layout"), legacy, luna_record()], pricing)
        groups = report["summary"]["current_breakdown"]
        self.assertAlmostEqual(groups["cu_analysis"]["estimated_cost"], .011)
        self.assertAlmostEqual(groups["cu_model"]["estimated_cost"], .005275)
        self.assertAlmostEqual(groups["analysis_model"]["estimated_cost"], .000191)
        self.assertEqual(groups["cu_model"]["status"], "complete")
        self.assertNotIn("explanation", groups["cu_model"])
        self.assert_costs_add_up(report)

    def test_unverified_or_contradictory_profile_never_implies_model_free(self):
        for profile in (None, "engineering", "unknown", True, "Layout"):
            entry = layout_record()
            entry["metadata"]["extraction_profile"] = profile
            report = usage_report([entry], self.pricing())
            self.assertIsNone(report["summary"]["current"]["estimated_cost"])
            self.assertIsNone(report["summary"]["current_breakdown"]["cu_model"]["estimated_cost"])
            self.assert_costs_add_up(report)
        for changes in ({"model_deployments": {"gpt-5.4": "legacy"}},
                        {"selected_completion_model": "gpt-5.4"},
                        {"deployment_versions": {"legacy": "gpt-5.4:1:GlobalStandard"}}):
            entry = layout_record()
            entry["metadata"].update(changes)
            self.assertIsNone(usage_report([entry], self.pricing())["summary"]["current"]["estimated_cost"])

    def test_cached_resumed_and_duplicate_references_contribute_no_current_cost(self):
        entries = [layout_record(state="cached"), luna_record(state="resumed"),
                   luna_record(state="cached"), layout_record(state="resumed"),
                   luna_record(state="cached", key="missing", usage=None)]
        report = usage_report(entries, self.pricing())
        self.assertEqual(report["summary"]["current"]["estimated_cost"], 0)
        self.assertGreater(report["summary"]["reused"]["known_cost"], 0)
        for groups in [report["summary"]["current_breakdown"]] + [
                entry["current_breakdown"] for entry in report["entries"]]:
            for group in groups.values():
                self.assertEqual(group, {
                    "estimated_cost": 0, "estimated_cost_range": {"min": 0, "max": 0}, "known_cost": 0,
                    "unknown_usage_calls": 0, "unpriced_meters": 0, "status": "not_used",
                })
        self.assertFalse(report["entries"][2]["counted_in_summary"])
        self.assertFalse(report["entries"][3]["counted_in_summary"])
        self.assert_costs_add_up(report)

    def test_new_call_plus_duplicate_cache_references_is_charged_once(self):
        entries = [luna_record(state="cached"), luna_record(), luna_record(state="cached")]
        report = usage_report(entries, self.pricing())
        self.assertAlmostEqual(report["summary"]["current_breakdown"]["analysis_model"]["estimated_cost"], .000191)
        self.assertEqual(report["summary"]["reused"]["estimated_cost"], 0)
        self.assertEqual([entry["counted_in_summary"] for entry in report["entries"]], [False, True, False])
        self.assert_costs_add_up(report)
        # Separate newly submitted calls retain their charges, even with the same cache key.
        twice = usage_report([luna_record(), luna_record()], self.pricing())
        self.assertAlmostEqual(twice["summary"]["current_breakdown"]["analysis_model"]["estimated_cost"], 2*.000191)

    def test_unknown_cache_ownership_is_not_assumed_historical(self):
        for state in ("unknown", "unrecognized", None):
            report = usage_report([luna_record(state=state, usage=None)], self.pricing())
            self.assertIsNone(report["summary"]["current"]["estimated_cost"])
            self.assertIsNone(report["summary"]["current_breakdown"]["analysis_model"]["estimated_cost"])
            self.assertEqual(report["summary"]["requests"]["unknown"], 1)
            self.assert_costs_add_up(report)

    def test_missing_failed_or_partial_layout_usage_retains_known_costs_not_fake_zero(self):
        for changes in ({"usage": None}, {"usage": {}}, {"outcome": "operation_incomplete"},
                        {"usage_incomplete": True}):
            entry = layout_record()
            entry.update(changes)
            report = usage_report([entry], self.pricing())
            groups = report["summary"]["current_breakdown"]
            self.assertIsNone(groups["cu_analysis"]["estimated_cost"])
            self.assertIsNone(groups["cu_model"]["estimated_cost"])
            self.assertEqual(groups["cu_model"]["status"], "partial")
            self.assertEqual(groups["cu_analysis"]["unknown_usage_calls"], 1)
            if entry["usage"]:
                self.assertAlmostEqual(groups["cu_analysis"]["known_cost"], .005)
            self.assertEqual(report["entries"][0]["raw_usage"], entry["usage"])
            self.assert_costs_add_up(report)
        report = usage_report([luna_record(outcome="operation_incomplete")], self.pricing())
        self.assertIsNone(report["summary"]["current_breakdown"]["analysis_model"]["estimated_cost"])
        self.assertEqual(report["summary"]["current_breakdown"]["cu_analysis"]["status"], "not_used")
        self.assert_costs_add_up(report)

    def test_layout_never_suppresses_reported_tokens_unknown_meters_or_invalid_pages(self):
        for extra in ({"tokens": None}, {"tokens": {"unknown-token": 10}},
                      {"tokens": {"synthetic-input": 1000, "synthetic-output": 200}},
                      {"documentPagesStandard": -1}, {"newMeter": 10},
                      {"contextualizationTokens": 1000}):
            entry = layout_record()
            entry["usage"].update(extra)
            report = usage_report([entry], self.pricing())
            self.assertIsNone(report["summary"]["current"]["estimated_cost"])
            self.assertNotEqual(report["summary"]["current_breakdown"]["cu_model"]["status"], "not_applicable")
            self.assertEqual(report["entries"][0]["raw_usage"], entry["usage"])
            self.assert_costs_add_up(report)
        entry = layout_record()
        entry["usage"].update(record("cu")["usage"])
        entry["metadata"].update(model_deployments={"synthetic": "deployment"},
                                  deployment_versions={"deployment": "synthetic:1:GlobalStandard"})
        report = usage_report([entry], dict(self.pricing(), cu_input_includes_cached=True))
        groups = report["summary"]["current_breakdown"]
        self.assertAlmostEqual(groups["cu_model"]["estimated_cost"], .00262)
        self.assertAlmostEqual(groups["cu_analysis"]["estimated_cost"], .006)
        self.assert_costs_add_up(report)

    def test_missing_journal_all_groups_unknown_but_empty_journal_is_not_used(self):
        unavailable = usage_report(None, self.pricing())
        for group in unavailable["summary"]["current_breakdown"].values():
            self.assertIsNone(group["estimated_cost"])
            self.assertEqual(group["status"], "partial")
        self.assert_costs_add_up(unavailable)
        empty = usage_report([], self.pricing())
        self.assertTrue(all(group["status"] == "not_used"
                            for group in empty["summary"]["current_breakdown"].values()))
        self.assert_costs_add_up(empty)


class UserProvidedPricingTests(unittest.TestCase):
    def test_example_has_only_four_luna_prices_with_explicit_user_provenance(self):
        pricing = validate_pricing(luna_prices())
        self.assertEqual({r["key"]: r["price"] for r in pricing["rates"]}, {
            "model.gpt-6-luna.input": .10, "model.gpt-6-luna.cached_input": .01,
            "model.gpt-6-luna.cache_write": .125, "model.gpt-6-luna.output": .50,
        })
        for rate in pricing["rates"]:
            self.assertEqual(rate["unit_quantity"], 1_000_000)
            self.assertEqual(rate["unit"], "tokens")
            self.assertEqual(rate["currency"], "USD")
            self.assertEqual(rate["source_kind"], "user_provided")
            self.assertIn("not verified Azure retail", rate["source"])

    def test_million_token_prices_and_cached_split_preserve_raw_counts(self):
        entry = luna_record(usage={
            "prompt_tokens": 2_000_000, "completion_tokens": 1_000_000,
            "total_tokens": 3_000_000,
            "prompt_tokens_details": {"cached_tokens": 1_000_000},
            "completion_tokens_details": {"reasoning_tokens": 500_000},
        })
        report = usage_report([entry], luna_prices())
        current = report["summary"]["current"]
        self.assertEqual(current["input_tokens"], 2_000_000)
        self.assertEqual(current["model_tokens"], 3_000_000)
        self.assertAlmostEqual(current["estimated_cost"], .61)
        meters = {m["key"].rsplit(".", 1)[1]: m for m in report["entries"][0]["meters"]}
        for kind, cost in (("input", .10), ("cached_input", .01), ("output", .50)):
            self.assertEqual(meters[kind]["quantity"], 1_000_000)
            self.assertAlmostEqual(meters[kind]["estimated_cost"], cost)
            self.assertEqual(meters[kind]["rate"]["source_kind"], "user_provided")
        self.assertEqual(report["entries"][0]["raw_usage"], entry["usage"])
        self.assertEqual(report["price_sources"], [{
            "source_kind": "user_provided", "as_of": "2026-09-24",
            "source": "User-provided GPT-6 Luna rates; not verified Azure retail pricing",
        }])

    def test_small_usage_uses_per_million_not_per_thousand(self):
        report = usage_report([luna_record()], luna_prices())
        self.assertAlmostEqual(report["summary"]["current"]["estimated_cost"], .000191)

    def test_valid_latency_diagnostics_preserve_exact_cost_and_raw_values(self):
        entry = luna_record()
        entry["usage"]["latency_checkpoint"] = latency_checkpoint()
        original = copy.deepcopy(entry)
        report = usage_report([entry], luna_prices())
        current = report["summary"]["current"]
        self.assertEqual(report["status"], "complete")
        self.assertAlmostEqual(current["estimated_cost"], .000191)
        self.assertAlmostEqual(report["entries"][0]["current_cost"], .000191)
        self.assertEqual(current["model_tokens"], 1200)
        self.assertEqual(current["unknown_usage_calls"], 0)
        self.assertEqual(report["entries"][0]["raw_usage"], original["usage"])
        self.assertEqual(len(report["entries"][0]["meters"]), 3)
        self.assertEqual(entry, original)

    def test_invalid_latency_schema_is_explicit_not_silently_discarded(self):
        valid = latency_checkpoint()
        invalid = [None, [], "timing", 10, {}, dict(valid, unknown_tokens=10),
                   {key: value for key, value in valid.items() if key != "engine_tbt_ms"}]
        invalid.extend(dict(valid, engine_tbt_ms=value)
                       for value in (-1, True, "5", None, {}, float("nan"), float("inf"), 2**53))
        for latency in invalid:
            with self.subTest(latency=latency):
                entry = luna_record()
                entry["usage"]["latency_checkpoint"] = latency
                report = usage_report([entry], luna_prices())
                self.assertIsNone(report["summary"]["current"]["estimated_cost"])
                self.assertIsNone(report["entries"][0]["current_cost"])
                self.assertIn("latency_checkpoint", report["entries"][0]["raw_usage"])
                meter = next(m for m in report["entries"][0]["meters"]
                             if m["key"] == "model.unknown.latency_checkpoint")
                self.assertEqual(meter["reason"], "missing_usage")
                json.dumps(report, allow_nan=False)

    def test_valid_latency_does_not_hide_unknown_or_invalid_token_meters(self):
        for mutate in (
            lambda usage: usage.update(unknown_tokens=10),
            lambda usage: usage["prompt_tokens_details"].update(unknown_cache_tokens=10),
            lambda usage: usage["prompt_tokens_details"].update(cached_tokens=-1),
            lambda usage: usage.update(completion_tokens=None),
        ):
            entry = luna_record()
            entry["usage"]["latency_checkpoint"] = latency_checkpoint()
            mutate(entry["usage"])
            report = usage_report([entry], luna_prices())
            self.assertIsNone(report["summary"]["current"]["estimated_cost"])
            self.assertEqual(report["entries"][0]["raw_usage"], entry["usage"])
            self.assertFalse(any(m["key"] == "model.unknown.latency_checkpoint"
                                 for m in report["entries"][0]["meters"]))

    def test_cache_write_price_does_not_invent_prompt_overlap_semantics(self):
        entry = luna_record(usage={
            "prompt_tokens": 1_000_000, "completion_tokens": 0, "total_tokens": 1_000_000,
            "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 1_000_000},
        })
        report = usage_report([entry], luna_prices())
        current = report["summary"]["current"]
        write = next(m for m in report["entries"][0]["meters"] if m["key"].endswith(".cache_write"))
        self.assertEqual(current["cache_write_tokens"], 1_000_000)
        self.assertEqual(current["model_tokens"], 1_000_000)
        self.assertAlmostEqual(write["estimated_cost"], .125)
        self.assertAlmostEqual(current["known_cost"], .125)
        self.assertIsNone(current["estimated_cost"])
        self.assertEqual(report["entries"][0]["raw_usage"], entry["usage"])

    def test_response_cache_replay_is_free_with_historical_price_reference(self):
        entries = [luna_record(state="cached"), luna_record(state="cached")]
        report = usage_report(entries, luna_prices())
        self.assertEqual(report["summary"]["current"]["estimated_cost"], 0)
        self.assertAlmostEqual(report["summary"]["reused"]["estimated_cost"], .000191)
        self.assertTrue(all(entry["current_cost"] == 0 for entry in report["entries"]))
        self.assertFalse(report["entries"][1]["counted_in_summary"])

    def test_missing_malformed_and_unknown_usage_are_not_zero(self):
        for usage in (None, {}, {"prompt_tokens": 1000},
                      {"prompt_tokens": 1000, "completion_tokens": 200, "new_meter": 10},
                      {"prompt_tokens": 1000, "completion_tokens": 200, "prompt_tokens_details": []},
                      {"prompt_tokens": 1000, "completion_tokens": 200,
                       "prompt_tokens_details": {"unknown_cache_tokens": 10}}):
            with self.subTest(usage=usage):
                report = usage_report([luna_record(usage=usage)], luna_prices())
                self.assertIsNone(report["summary"]["current"]["estimated_cost"])
                self.assertEqual(report["entries"][0]["raw_usage"], usage)

    def test_no_luna_price_leaks_to_other_models_or_mismatched_response(self):
        for model in ("gpt-6-astra", "gpt-5.4", "unpriced-model"):
            entry = record()
            entry["metadata"].update(model=model, deployment_version=f"{model}:1:GlobalStandard")
            with self.subTest(model=model):
                self.assertIsNone(usage_report([entry], luna_prices())["summary"]["current"]["estimated_cost"])
                mismatch = luna_record()
                mismatch["metadata"]["response_model"] = model
                report = usage_report([mismatch], luna_prices())
                self.assertIsNone(report["summary"]["current"]["estimated_cost"])
                self.assertTrue(all(m["rate"] is None for m in report["entries"][0]["meters"]))

    def test_official_snapshot_rates_remain_unchanged_with_user_rates(self):
        official = {"snapshot": "azure-retail-westus-2026-09-23", "region": "westus"}
        combined = dict(official, rates=luna_prices()["rates"])
        baseline = validate_pricing(official)["rates"]
        self.assertEqual(validate_pricing(combined)["rates"][:len(baseline)], baseline)
        self.assertEqual(len(validate_pricing(combined)["rates"]), len(baseline)+4)
        for model, version in (("gpt-6-astra", "2026-09-03"), ("gpt-5.4", "2026-03-05")):
            entry = record()
            entry["metadata"].update(model=model, deployment_version=f"{model}:{version}:GlobalStandard")
            self.assertEqual(usage_report([entry], combined), usage_report([entry], official))
        self.assertAlmostEqual(usage_report([luna_record()], combined)["summary"]["current"]["estimated_cost"],
                               .000191)

    def test_reference_sources_still_require_https_and_user_source_is_explicit(self):
        for changes in ({"source_kind": "azure_retail"}, {"source_kind": None}, {"source": ""},
                        {"source": "   "}, {"source": None}):
            pricing = luna_prices()
            pricing["rates"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_pricing(pricing)
        pricing = luna_prices()
        del pricing["rates"][0]["source_kind"]
        with self.assertRaises(ValueError):
            validate_pricing(pricing)
        for source in ("user supplied", "http://example.com", "https://user:password@example.com"):
            pricing = prices()
            pricing["rates"][0]["source"] = source
            with self.subTest(source=source), self.assertRaises(ValueError):
                validate_pricing(pricing)


class AccountingTests(unittest.TestCase):
    def test_semantic_pairing_has_named_usage_and_cache_replay_does_not_rebill(self):
        report = usage_report([record(stage="model_text_pairing")], prices())
        self.assertEqual(report["entries"][0]["stage"], "模型 · 字段与OCR语义配对")
        self.assertEqual(report["summary"]["current"]["model_tokens"], 1200)
        replay = usage_report([record(stage="model_text_pairing", state="cached")], prices())
        self.assertEqual(replay["summary"]["current"]["estimated_cost"], 0)
        self.assertEqual(replay["summary"]["reused"]["model_tokens"], 1200)

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

    def test_layout_transport_emits_model_free_provenance_and_preserves_cache_identity(self):
        client = Client(dict(configuration(), extraction_profile="layout"))
        raw = {"status": "Succeeded", "usage": {"documentPagesStandard": 1},
               "result": {"contents": [{
                   "unit": "inch", "pages": [{"pageNumber": 1, "width": 1, "height": 1,
                                              "lines": [], "words": []}],
               }]}}
        with patch.object(client, "request", return_value=({}, {"Operation-Location": "operation"})) as request:
            with patch.object(client, "poll", return_value=raw):
                _, submitted = client.analyze(self.pdf, self.root, "layout-test", {})
            _, cached = client.analyze(self.pdf, self.root, "layout-test", {}, allow_submit=False)
        self.assertEqual(request.call_count, 1)
        self.assertNotIn("modelDeployments", request.call_args.args[2])
        self.assertEqual(submitted["cache_key"], cached["cache_key"])
        self.assertEqual(client.usage_records[0]["metadata"], {
            "model_deployments": {}, "selected_completion_model": None,
            "deployment_versions": {}, "extraction_profile": "layout",
        })
        report = usage_report(client.usage_records, prices())
        self.assertAlmostEqual(report["summary"]["current"]["estimated_cost"], .005)
        self.assertEqual(report["summary"]["current_breakdown"]["cu_model"]["status"], "not_applicable")
        self.assertEqual(report["entries"][1]["current_cost"], 0)
        self.assertFalse(report["entries"][1]["counted_in_summary"])

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
