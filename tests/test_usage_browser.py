"""Offline usage-tab regressions: synthetic API payloads, no server or Azure calls."""

import copy
import unittest
from urllib.parse import urlparse

from tests import test_graphics_browser as graphics

expect = graphics.expect


def metrics(**values):
    return dict(cu_pages=0, contextualization_tokens=0, input_tokens=0,
                cached_input_tokens=0, output_tokens=0, model_tokens=0,
                estimated_cost=0, known_cost=0, unpriced_meters=0,
                unknown_usage_calls=0) | values


def meter(category, label, quantity, price, unit="tokens", unit_quantity=1000):
    return {
        "key": label, "category": category, "label": label, "quantity": quantity,
        "unit": unit, "estimated_cost": quantity / unit_quantity * price, "reason": None,
        "rate": {"price": price, "unit_quantity": unit_quantity, "currency": "USD",
                 "source": "https://prices.example.test/public?sku=synthetic",
                 "as_of": "2026-09-23", "region": "synthetic-region",
                 "model_version": "synthetic-version", "sku": "synthetic-sku"},
    }


def usage_fixture():
    cu = metrics(cu_pages=2, contextualization_tokens=100, input_tokens=20,
                 model_tokens=20, estimated_cost=.04, known_cost=.04)
    direct = metrics(input_tokens=100, cached_input_tokens=40, output_tokens=30,
                     model_tokens=130, estimated_cost=.08, known_cost=.08)
    history = metrics(cu_pages=7, input_tokens=999, output_tokens=1,
                      model_tokens=1000, estimated_cost=9.5, known_cost=9.5)
    return {
        "version": 1, "currency": "USD", "status": "complete", "price_as_of": "2026-09-23",
        "summary": {
            "requests": {"new": 2, "cached": 1, "resumed": 0, "unknown": 0},
            "current": metrics(cu_pages=2, contextualization_tokens=100, input_tokens=120,
                               cached_input_tokens=40, output_tokens=30, model_tokens=150,
                               estimated_cost=.12, known_cost=.12),
            "reused": history,
        },
        "entries": [
            {"id": "cu-new", "service": "cu", "stage": "全文提取", "region_index": None,
             "cache_state": "new", "usage_status": "reported", "model": "synthetic-cu",
             "deployment": "synthetic-cu-deployment", "metrics": cu, "current_cost": .04,
             "reference_cost": None, "known_cost": .04, "warnings": [],
             "meters": [meter("cu_extraction", "提取页面", 2, .01, "pages", 1),
                        meter("cu_contextualization", "上下文处理", 100, .1),
                        meter("cu_model", "CU 内部输入", 20, .5)]},
            {"id": "direct-new", "service": "model", "stage": "区域精比对", "region_index": 0,
             "cache_state": "new", "usage_status": "reported", "model": "synthetic-model",
             "deployment": "synthetic-comparison", "metrics": direct, "current_cost": .08,
             "reference_cost": None, "known_cost": .08, "warnings": [],
             "meters": [meter("direct_model", "直接模型输入", 100, .5),
                        meter("direct_model", "直接模型输出（包含推理）", 30, 1)]},
            {"id": "cu-cached", "service": "cu", "stage": "局部复读", "region_index": 1,
             "cache_state": "cached", "usage_status": "reported", "model": None,
             "deployment": None, "metrics": history, "current_cost": 0,
             "reference_cost": 9.5, "known_cost": 9.5, "warnings": [],
             "meters": [meter("cu_model", "历史内部模型", 1000, 9.5)]},
        ],
        "warnings": [],
    }


def scenario_fixture():
    usage = usage_fixture()
    usage["status"] = "partial"
    usage["summary"]["current"].update(
        estimated_cost=None, estimated_cost_range={"min": .045, "max": .135},
        known_cost=.02, model_tokens=None, input_tokens=None,
        cache_write_tokens=77, unknown_usage_calls=1, unpriced_meters=2)
    usage["summary"]["reused"].update(
        estimated_cost=None, estimated_cost_range={"min": 7, "max": 12},
        cache_write_tokens=88)
    entry = usage["entries"][0]
    entry.update(current_cost=None, current_cost_range={"min": .03, "max": .09},
                 metrics=metrics(input_tokens=None, model_tokens=None, cache_write_tokens=77),
                 raw_usage={"inputTokens": 100, "cachedInputTokens": 40,
                            "cacheWriteTokens": 77, "pages": 2})
    unknown = entry["meters"][2]
    short = copy.deepcopy(unknown["rate"])
    short.update(context_tier="short", meter_id="short-id", meter_name="Short input",
                 source="https://prices.example.test/short", price=.5)
    long = copy.deepcopy(short)
    long.update(context_tier="long", meter_id="long-id", meter_name="Long input",
                source="https://prices.example.test/long", price=1)
    unknown.update(quantity=None, quantity_range=[60, 100], rate=None,
                   rate_candidates=[short, long], estimated_cost=None,
                   estimated_cost_range={"min": .03, "max": .1},
                   reason="uncertain_usage_semantics")
    usage["entries"][1]["meters"][0].update(
        rate=None, rate_candidates=[short, long], estimated_cost=None,
        estimated_cost_range={"min": .05, "max": .1}, reason="ambiguous_rate")
    usage["entries"][2].update(
        reference_cost=None, reference_cost_range={"min": 7, "max": 12})
    return usage


class UsageBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        graphics.GraphicsBrowserTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        graphics.GraphicsBrowserTests.tearDownClass.__func__(cls)

    open_context = graphics.GraphicsBrowserTests.open_context
    tearDown = graphics.GraphicsBrowserTests.tearDown
    compare = graphics.GraphicsBrowserTests.compare
    select = graphics.GraphicsBrowserTests.select
    assert_source_columns = graphics.GraphicsBrowserTests.assert_source_columns

    def setUp(self):
        self.usage = usage_fixture()
        self.job_status = "succeeded"
        self.polls = 0
        self.response_sequence = []
        self.result_only_usage = False
        graphics.GraphicsBrowserTests.setUp(self)

    def route(self, route):
        url = urlparse(route.request.url)
        if url.netloc == "graphics-ui.test" and url.path == "/api/jobs/synthetic-job" and not self.hold_jobs:
            self.polls += 1
            if self.response_sequence:
                route.fulfill(json=self.response_sequence.pop(0))
                return
            result = copy.deepcopy(self.result)
            payload = {"status": self.job_status, "error": "Synthetic interrupted analysis",
                       "result": result}
            if self.usage is not None:
                if self.result_only_usage:
                    result["usage_cost"] = self.usage
                else:
                    payload["usage_cost"] = self.usage
            route.fulfill(json=payload)
        else:
            graphics.GraphicsBrowserTests.route(self, route)

    def usage_tab(self):
        self.page.get_by_role("tab", name="用量与费用").click()
        expect(self.page.locator("#usage-panel")).to_be_visible()

    def open_call(self, index=0):
        self.page.locator(f"#usage-call-{index} > summary").click()

    def test_tab_roles_keyboard_and_original_evidence(self):
        evidence = self.page.get_by_role("tab", name="证据详情")
        usage = self.page.get_by_role("tab", name="用量与费用")
        expect(evidence).to_have_attribute("aria-controls", "evidence-panel")
        expect(usage).to_have_attribute("aria-controls", "usage-panel")
        expect(evidence).to_have_attribute("aria-selected", "true")
        expect(usage).to_have_attribute("tabindex", "-1")
        evidence.focus()
        for key, selected in [("ArrowRight", usage), ("ArrowRight", evidence),
                              ("ArrowLeft", usage), ("Home", evidence), ("End", usage)]:
            self.page.keyboard.press(key)
            expect(selected).to_be_focused()
            expect(selected).to_have_attribute("aria-selected", "true")
            expect(selected).to_have_attribute("tabindex", "0")
        expect(self.page.get_by_role("tabpanel", name="用量与费用")).to_be_visible()
        expect(self.page.locator("#evidence-panel")).to_be_hidden()
        expect(self.page.locator("#detail-meta")).to_be_hidden()
        self.compare()
        expect(usage).to_have_attribute("aria-selected", "true")
        self.select("D001")
        expect(evidence).to_have_attribute("aria-selected", "true")
        expect(self.page.locator("#usage-panel")).to_be_hidden()
        self.assert_source_columns()
        original = self.page.locator("#detail-content").inner_text()
        self.usage_tab()
        evidence.click()
        self.assertEqual(original, self.page.locator("#detail-content").inner_text())
        self.assert_source_columns()

    def test_mixed_usage_is_not_recalculated_and_prices_are_inspectable(self):
        # Backend owns deduplication: the client must display this authoritative total.
        self.usage["summary"]["current"]["estimated_cost"] = .12345678
        self.compare()
        self.usage_tab()
        expect(self.page.locator("#usage-current-cost")).to_contain_text("US$0.12345678")
        expect(self.page.locator("#usage-current-model")).to_contain_text("150")
        expect(self.page.locator("#usage-current-model")).to_contain_text("输入 120")
        expect(self.page.locator("#usage-current-model")).to_contain_text("缓存输入 40")
        expect(self.page.locator("#usage-current-model")).to_contain_text("输出 30")
        expect(self.page.locator("#usage-current-cu")).to_contain_text("2 页")
        expect(self.page.locator("#usage-history")).to_contain_text("US$9.50")
        expect(self.page.locator("#usage-history")).to_contain_text("非本轮新增计费")
        expect(self.page.locator("#usage-requests")).to_contain_text("本地缓存 1")
        expect(self.page.locator("#usage-content")).to_contain_text("推理 token 已包含在输出内")
        for index in range(3):
            self.open_call(index)
        for label in ("CU 页面提取", "CU 上下文 token", "CU 内部模型 token", "直接对比模型 token"):
            expect(self.page.locator("#usage-content")).to_contain_text(label)
        expect(self.page.locator("#usage-call-0")).to_contain_text("2 ÷ 1 × US$0.01")
        expect(self.page.locator("#usage-call-2")).to_contain_text("历史 / 非新提交参考用量")
        expect(self.page.locator("#usage-call-2 > summary")).to_contain_text("新增估算 US$0.00")
        self.page.locator("#usage-call-0-meter-0 > summary").click()
        link = self.page.locator("#usage-call-0-meter-0 a")
        expect(link).to_have_attribute("href", "https://prices.example.test/public?sku=synthetic")
        expect(link).to_have_attribute("rel", "noopener noreferrer")
        expect(link).to_have_attribute("referrerpolicy", "no-referrer")
        expect(self.page.locator("#usage-call-0-meter-0")).to_contain_text("2026-09-23")
        expect(self.page.locator("#usage-call-0-meter-0")).to_contain_text("synthetic-version")
        expect(self.page.locator("#usage-call-0 .usage-table th[scope=col]")).to_have_count(5)

    def test_all_cache_zero_current_even_when_history_is_partial(self):
        self.usage["status"] = "partial"
        self.usage["summary"]["current"] = metrics()
        self.usage["summary"]["reused"].update(estimated_cost=None, unpriced_meters=1)
        self.usage["summary"]["requests"] = {"new": 0, "cached": 1, "resumed": 1, "unknown": 0}
        self.usage["entries"] = [self.usage["entries"][2]]
        self.usage["entries"][0]["cache_state"] = "resumed"
        self.compare()
        self.usage_tab()
        expect(self.page.locator("#usage-current-cost .usage-value")).to_have_text("US$0.00")
        expect(self.page.locator("#usage-current-model .usage-value")).to_have_text("0")
        expect(self.page.locator("#usage-history")).to_contain_text("历史参考费用：未知")
        expect(self.page.locator("#usage-history")).to_contain_text("US$9.50")
        expect(self.page.locator("#usage-requests")).to_contain_text("恢复 1")
        expect(self.page.locator("#usage-call-0 > summary")).to_contain_text("恢复历史操作")

    def test_unknown_price_and_usage_preserve_known_subtotal(self):
        self.usage["status"] = "partial"
        self.usage["summary"]["current"].update(estimated_cost=None, known_cost=.04,
                                               unpriced_meters=1, unknown_usage_calls=1,
                                               model_tokens=None, output_tokens=None)
        self.usage["entries"][1]["meters"][0].update(rate=None, estimated_cost=None)
        self.usage["entries"][1]["meters"][1].update(quantity=None, estimated_cost=None)
        self.usage["entries"][1].update(usage_status="missing", current_cost=None)
        self.compare()
        self.usage_tab()
        expect(self.page.locator("#usage-current-cost .usage-value")).to_have_text("费用未完整估算")
        expect(self.page.locator("#usage-current-cost")).to_contain_text("已知费用小计（不等于完整总额）：US$0.04")
        expect(self.page.locator("#usage-current-model .usage-value")).to_have_text("未提供")
        self.open_call(1)
        expect(self.page.locator("#usage-call-1")).to_contain_text("单价待配置，无法估算")
        expect(self.page.locator("#usage-call-1")).to_contain_text("用量未提供，无法估算")
        expect(self.page.locator("#usage-call-1")).to_contain_text("计量估算：未知")
        self.assertNotIn("US$0.00", self.page.locator("#usage-call-1").inner_text())

    def test_failed_and_stale_jobs_keep_partial_consumption(self):
        self.usage["status"] = "partial"
        self.usage_tab()
        for state, message in [("failed", "对比未完成"), ("stale", "本次结果已作废")]:
            with self.subTest(state=state):
                self.job_status = state
                self.page.locator("#compare-button").click()
                expect(self.page.locator("#job-status")).to_contain_text(message)
                expect(self.page.locator("#usage-current-cost .usage-value")).to_have_text("US$0.12")
                expect(self.page.locator("#usage-tab")).to_have_attribute("aria-selected", "true")
                expect(self.page.locator("#results-list .result-item")).to_have_count(0)

    def test_polling_updates_usage_without_switching_tab_or_closing_details(self):
        running = copy.deepcopy(self.usage)
        running["status"] = "partial"
        running["summary"]["current"]["estimated_cost"] = .01
        self.response_sequence = [
            {"status": "running", "usage_cost": running},
            {"status": "running", "usage_cost": running},
        ]
        self.usage_tab()
        self.page.locator("#compare-button").click()
        expect(self.page.locator("#usage-current-cost .usage-value")).to_have_text("US$0.01")
        self.open_call()
        expect(self.page.locator("#job-status")).to_contain_text("对比完成", timeout=10000)
        self.assertGreaterEqual(self.polls, 3)
        expect(self.page.locator("#usage-current-cost .usage-value")).to_have_text("US$0.12")
        expect(self.page.locator("#usage-tab")).to_have_attribute("aria-selected", "true")
        expect(self.page.locator("#usage-call-0")).to_have_attribute("open", "")

    def test_new_run_and_upload_clear_previous_usage_before_response(self):
        self.compare()
        self.usage_tab()
        self.hold_jobs = True
        self.page.locator("#compare-button").click()
        expect(self.page.locator("#usage-content")).to_contain_text("暂无用量数据")
        expect(self.page.locator("#usage-current-cost")).to_have_count(0)
        self.page.wait_for_timeout(100)
        self.assertEqual(len(self.held_jobs), 1)
        self.held_jobs.pop().fulfill(json={"status": "succeeded", "result": self.result,
                                         "usage_cost": self.usage})
        expect(self.page.locator("#usage-current-cost")).to_be_visible()
        self.hold_uploads = True
        self.page.locator("#old-upload").set_input_files(
            {"name": "replacement.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-synthetic"})
        expect(self.page.locator("#usage-content")).to_contain_text("暂无用量数据")
        expect(self.page.locator("#usage-current-cost")).to_have_count(0)
        expect(self.page.locator("#usage-tab")).to_have_attribute("aria-selected", "true")
        self.page.wait_for_timeout(100)
        self.assertEqual(len(self.held_uploads), 1)
        route, payload = self.held_uploads.pop()
        route.fulfill(json=payload)
        self.page.wait_for_load_state("networkidle")

    def test_legacy_payload_and_missing_optional_fields_are_not_zero(self):
        self.usage = None
        self.compare()
        self.usage_tab()
        expect(self.page.locator("#usage-content")).to_contain_text("暂无用量数据")
        self.assertNotIn("US$0", self.page.locator("#usage-content").inner_text())
        self.usage = {"status": "unavailable", "entries": [{"service": "cu"}]}
        self.compare()
        expect(self.page.locator("#usage-current-cost")).to_contain_text("费用未完整估算")
        expect(self.page.locator("#usage-current-model .usage-value")).to_have_text("未提供")
        self.open_call()
        expect(self.page.locator("#usage-call-0")).to_contain_text("模型 未提供")
        expect(self.page.locator("#usage-call-0")).to_contain_text("计量与价格明细：未提供")
        self.assertNotIn("US$0", self.page.locator("#usage-content").inner_text())

    def test_result_usage_fallback_and_top_level_precedence(self):
        self.result_only_usage = True
        self.compare()
        self.usage_tab()
        expect(self.page.locator("#usage-current-cost .usage-value")).to_have_text("US$0.12")
        self.result_only_usage = False
        self.result["usage_cost"] = copy.deepcopy(self.usage)
        self.result["usage_cost"]["summary"]["current"]["estimated_cost"] = 888
        self.compare()
        expect(self.page.locator("#usage-current-cost .usage-value")).to_have_text("US$0.12")

    def test_untrusted_prose_and_source_urls_are_inert(self):
        attack = '<img src=x onerror="window.usageXss=1"><svg onload="window.usageXss=2">'
        self.usage["warnings"] = [attack]
        entry = self.usage["entries"][0]
        entry.update(stage=attack, model=attack, warnings=[attack])
        bad_urls = ["javascript:window.usageXss=3", "data:text/html,<script>alert(1)</script>",
                    "//attacker.example", "http://attacker.example",
                    "https://user:password@attacker.example", "https:\\\\attacker.example",
                    "https://good.example/\nattack", attack]
        prototype = entry["meters"][0]
        entry["meters"] = []
        for url in bad_urls:
            malicious = copy.deepcopy(prototype)
            malicious.update(label=attack, reason=attack)
            malicious["rate"].update(source=url, sku=attack)
            entry["meters"].append(malicious)
        self.compare()
        self.usage_tab()
        self.open_call()
        for index in range(len(bad_urls)):
            self.page.locator(f"#usage-call-0-meter-{index} > summary").click()
        expect(self.page.locator("#usage-content")).to_contain_text(attack)
        expect(self.page.locator("#usage-call-0 a")).to_have_count(0)
        expect(self.page.locator("#usage-content img, #usage-content svg, #usage-content script")).to_have_count(0)
        self.assertIsNone(self.page.evaluate("window.usageXss"))

    def test_scenario_ranges_keep_subtotals_unknown_status_and_raw_counts_separate(self):
        self.usage = scenario_fixture()
        self.compare()
        self.usage_tab()
        expect(self.page.locator("#usage-current-cost .usage-value")).to_have_text(
            "US$0.045 – US$0.135（场景估算）")
        expect(self.page.locator("#usage-current-cost")).to_contain_text(
            "已知费用小计（不等于完整总额）：US$0.02")
        expect(self.page.locator("#usage-current-cost")).to_contain_text("用量待核调用 1")
        expect(self.page.locator("#usage-current-cost")).to_contain_text("价格待核计量项 2")
        expect(self.page.locator(".usage-heading")).to_contain_text("部分数据 / 估算不完整")
        expect(self.page.locator("#usage-current-model .usage-value")).to_have_text("未提供")
        expect(self.page.locator("#usage-current-model")).to_contain_text("缓存写入 77 token")
        expect(self.page.locator("#usage-history")).to_contain_text(
            "历史参考费用：US$7.00 – US$12.00（场景估算）")
        expect(self.page.locator("#usage-history")).to_contain_text("缓存写入 88 token")
        expect(self.page.locator("#usage-history")).to_contain_text("按当前配置的价格快照重估历史用量")
        expect(self.page.locator("#usage-history")).to_contain_text("不是原始日期的账单")
        self.open_call()
        expect(self.page.locator("#usage-call-0 > summary")).to_contain_text(
            "新增估算 US$0.03 – US$0.09（场景估算）")
        expect(self.page.locator("#usage-call-0 .usage-note").first).to_contain_text(
            "CU 内部模型 token 未提供")
        self.page.locator("#usage-call-0-raw > summary").click()
        expect(self.page.locator("#usage-call-0-raw pre")).to_contain_text('"inputTokens": 100')
        expect(self.page.locator("#usage-call-0-raw pre")).to_contain_text('"cachedInputTokens": 40')
        expect(self.page.locator("#usage-call-0-raw")).to_contain_text("字段可能重叠")
        row = self.page.locator("#usage-call-0 .usage-table tbody tr").nth(2)
        expect(row).to_contain_text("60 – 100（场景用量） token")
        expect(row).to_contain_text("计量估算：US$0.03 – US$0.10（场景估算）")
        expect(row).to_contain_text("60 – 100（场景用量） ÷ 1,000 × US$0.50")
        expect(row).to_contain_text("60 – 100（场景用量） ÷ 1,000 × US$1.00")
        self.open_call(2)
        expect(self.page.locator("#usage-call-2")).to_contain_text(
            "参考费用 US$7.00 – US$12.00（场景估算）")
        expect(self.page.locator("#usage-call-2")).to_contain_text("非历史账单，非本轮新增")
        expect(self.page.locator("#usage-call-2 > summary")).to_contain_text("新增估算 US$0.00")
        # Neither raw counters, cache writes, meter ranges, nor history alter backend totals.
        expect(self.page.locator("#usage-current-cost .usage-value")).to_have_text(
            "US$0.045 – US$0.135（场景估算）")
        expect(self.page.locator("#usage-current-model .usage-value")).to_have_text("未提供")

    def test_candidate_price_tiers_and_individual_provenance_are_accessible(self):
        self.usage = scenario_fixture()
        self.compare()
        self.usage_tab()
        self.open_call()
        self.page.locator("#usage-call-0-meter-2 > summary").click()
        detail = self.page.locator("#usage-call-0-meter-2")
        for phrase in ("短上下文（short）", "长上下文（long）", "适用性未确认",
                       "short-id", "long-id", "Short input", "Long input", "2026-09-23",
                       "CU 输入与缓存输入是否重叠尚未确认"):
            expect(detail).to_contain_text(phrase)
        links = detail.locator("a")
        expect(links).to_have_count(2)
        for index, tier in enumerate(("short", "long")):
            expect(links.nth(index)).to_have_attribute("href", f"https://prices.example.test/{tier}")
            expect(links.nth(index)).to_have_attribute("rel", "noopener noreferrer")
        expect(self.page.locator("#usage-call-0 .usage-rate").nth(2)).to_contain_text("未选择；不相加")
        self.open_call(1)
        self.page.locator("#usage-call-1-meter-0 > summary").click()
        expect(self.page.locator("#usage-call-1-meter-0")).to_contain_text(
            "短 / 长上下文适用档位未确认")
        self.page.set_viewport_size({"width": 390, "height": 1000})
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 390)
        table = self.page.locator("#usage-call-0 .usage-table-scroll")
        self.assertGreater(table.evaluate("node => node.scrollWidth"), table.evaluate("node => node.clientWidth"))

    def test_raw_usage_and_candidate_metadata_never_become_html_or_unsafe_links(self):
        self.usage = scenario_fixture()
        attack = '<img src=x onerror="window.usageXss=1"><svg onload="window.usageXss=2">'
        self.usage["entries"][0]["raw_usage"] = {attack: attack, "inputTokens": 100}
        candidates = self.usage["entries"][0]["meters"][2]["rate_candidates"]
        candidates[0].update(source="javascript:window.usageXss=3", context_tier=attack,
                             meter_id=attack, meter_name=attack)
        candidates[1].update(source="https://user:password@attacker.example")
        self.compare()
        self.usage_tab()
        self.open_call()
        self.page.locator("#usage-call-0-raw > summary").click()
        self.page.locator("#usage-call-0-meter-2 > summary").click()
        expect(self.page.locator("#usage-call-0-raw pre")).to_contain_text('"inputTokens": 100')
        expect(self.page.locator("#usage-call-0-raw pre")).to_contain_text("<img src=x")
        expect(self.page.locator("#usage-call-0-meter-2")).to_contain_text(attack)
        expect(self.page.locator("#usage-call-0-meter-2 a")).to_have_count(0)
        expect(self.page.locator("#usage-content img, #usage-content svg, #usage-content script")).to_have_count(0)
        self.assertIsNone(self.page.evaluate("window.usageXss"))

    def test_exact_zero_overrides_scenario_history_and_cache_write_is_not_added(self):
        self.usage = scenario_fixture()
        self.usage["summary"]["current"] = metrics(
            cache_write_tokens=0, estimated_cost_range={"min": 10, "max": 20})
        self.usage["summary"]["requests"] = {"new": 0, "cached": 1, "resumed": 0, "unknown": 0}
        self.usage["entries"] = [self.usage["entries"][2]]
        self.compare()
        self.usage_tab()
        expect(self.page.locator("#usage-current-cost .usage-value")).to_have_text("US$0.00")
        expect(self.page.locator("#usage-current-model .usage-value")).to_have_text("0")
        expect(self.page.locator("#usage-current-model")).to_contain_text("缓存写入 0 token")
        expect(self.page.locator("#usage-history")).to_contain_text("US$7.00 – US$12.00（场景估算）")
        expect(self.page.locator("#usage-call-0 > summary")).to_contain_text("新增估算 US$0.00")
        self.open_call()
        self.page.locator("#usage-call-0-raw > summary").click()
        expect(self.page.locator("#usage-call-0-raw pre")).to_have_text("未提供")

    def test_invalid_ranges_do_not_invent_totals_and_zero_lower_bound_is_valid(self):
        self.usage = scenario_fixture()
        self.usage["summary"]["current"]["estimated_cost_range"] = {"min": 4, "max": 2}
        self.usage["entries"][0]["current_cost_range"] = {"min": -1, "max": 2}
        self.usage["entries"][0]["meters"][2]["quantity_range"] = [100, 60]
        self.usage["entries"][0]["meters"][2]["estimated_cost_range"] = {"min": "0", "max": 1}
        self.usage["summary"]["reused"]["estimated_cost_range"] = {"min": 0, "max": 1}
        self.compare()
        self.usage_tab()
        expect(self.page.locator("#usage-current-cost .usage-value")).to_have_text("费用未完整估算")
        expect(self.page.locator("#usage-call-0 > summary")).to_contain_text("新增估算 未知")
        expect(self.page.locator("#usage-history")).to_contain_text("US$0.00 – US$1.00（场景估算）")
        self.open_call()
        row = self.page.locator("#usage-call-0 .usage-table tbody tr").nth(2)
        expect(row).to_contain_text("用量未提供，无法估算")
        expect(row).to_contain_text("计量估算：未知")

    def test_desktop_and_narrow_tables_scroll_without_changing_evidence_columns(self):
        self.compare()
        for width in (1600, 900, 390):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 1000})
                self.select("D001")
                self.assert_source_columns()
                drawings = self.page.locator(".drawing-panel")
                old, new = [drawing.bounding_box() for drawing in drawings.all()]
                if width > 780:
                    self.assertAlmostEqual(old["y"], new["y"], delta=1)
                    self.assertLess(old["x"], new["x"])
                else:
                    self.assertLess(old["y"], new["y"])
                self.usage_tab()
                if not self.page.locator("#usage-call-0").get_attribute("open") == "":
                    self.open_call()
                expect(drawings.nth(0)).to_be_visible()
                expect(drawings.nth(1)).to_be_visible()
                self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), width)
                if width == 390:
                    table = self.page.locator("#usage-call-0 .usage-table-scroll")
                    self.assertGreater(table.evaluate("node => node.scrollWidth"),
                                       table.evaluate("node => node.clientWidth"))
                    table.evaluate("node => node.scrollLeft = 300")
                    self.assertGreater(table.evaluate("node => node.scrollLeft"), 0)


if __name__ == "__main__":
    unittest.main()
