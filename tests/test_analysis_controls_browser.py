"""Synthetic per-analysis controls and live timing UI checks; no Azure traffic."""

import copy
import unittest
from urllib.parse import urlparse

from tests import test_graphics_browser as graphics
from tests.test_usage_browser import usage_fixture

expect = graphics.expect


def timing(status="completed", elapsed=65.25):
    return {"total_seconds": elapsed, "queue_seconds": .25, "stages": [
        {"id": "cu_old", "label": "旧版 CU 提取", "status": "completed", "elapsed_seconds": .125},
        {"id": "model", "label": "模型区域对比", "status": status, "elapsed_seconds": 62.5},
    ]}


class AnalysisControlsBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        graphics.GraphicsBrowserTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        graphics.GraphicsBrowserTests.tearDownClass.__func__(cls)

    open_context = graphics.GraphicsBrowserTests.open_context
    tearDown = graphics.GraphicsBrowserTests.tearDown
    compare = graphics.GraphicsBrowserTests.compare

    def setUp(self):
        self.azure = True
        self.options = {"use_cache": True, "model": "gpt-6-astra", "models": [
            {"id": "gpt-6-astra", "label": "GPT-6 Astra"},
            {"id": "gpt-6-luna", "label": "GPT-6 Luna"},
        ]}
        self.requests = []
        self.job_options = None
        self.job_status = "succeeded"
        self.job_timing = timing()
        self.result_only_timing = False
        self.override_options = None
        self.polls = 0
        graphics.GraphicsBrowserTests.setUp(self)

    def route(self, route):
        url = urlparse(route.request.url)
        if url.netloc == "graphics-ui.test" and url.path == "/api/bootstrap":
            route.fulfill(json={
                "csrf_token": "synthetic-csrf", "revision": self.revision, "azure_enabled": self.azure,
                "model": "gpt-5.4", "model_comparison_deployment": "gpt-6-astra",
                "model_comparison_enabled": True, "semantic_text_pairing_enabled": True,
                "graphics_enabled": True, "documents": self.documents,
                "analysis_options": self.options,
                "limits": {"max_bytes": 20971520, "max_pages": 20, "session_ttl_hours": 24},
            })
        elif url.netloc == "graphics-ui.test" and url.path == "/api/compare":
            payload = route.request.post_data_json
            self.requests.append(payload)
            self.compare_requests += 1
            self.assertEqual(route.request.headers.get("x-csrf-token"), "synthetic-csrf")
            self.job_options = dict(use_cache=payload["use_cache"], model=payload["model"],
                                    model_label=next(m["label"] for m in self.options["models"]
                                                     if m["id"] == payload["model"]))
            route.fulfill(json={"job_id": "synthetic-job", "status": "queued"})
        elif url.netloc == "graphics-ui.test" and url.path == "/api/jobs/synthetic-job":
            self.polls += 1
            if self.hold_jobs:
                self.held_jobs.append(route)
                return
            result = copy.deepcopy(self.result)
            payload = {"status": self.job_status, "result": result,
                       "usage_cost": usage_fixture(), "error": "Synthetic analysis failure"}
            target = result if self.result_only_timing else payload
            target["timing"] = self.job_timing
            target["analysis_options"] = self.override_options or self.job_options
            route.fulfill(json=payload)
        else:
            graphics.GraphicsBrowserTests.route(self, route)

    def test_defaults_submit_cache_and_model_without_calling_when_selection_changes(self):
        expect(self.page.get_by_role("switch", name="使用缓存")).to_be_checked()
        expect(self.page.locator("#analysis-model")).to_have_value("gpt-6-astra")
        self.assertEqual(self.page.locator("#analysis-model option").all_text_contents(),
                         ["GPT-6 Astra", "GPT-6 Luna"])
        self.page.locator("#analysis-model").select_option("gpt-6-luna")
        expect(self.page.locator("#analysis-settings-note")).to_contain_text("CU 提取仍使用原模型")
        self.assertEqual(self.requests, [])
        self.compare()
        self.assertEqual(self.requests, [{"revision": 2, "use_cache": True, "model": "gpt-6-luna"}])
        expect(self.page.locator("#model-tag")).to_contain_text("CU：gpt-5.4 · 模型对比：GPT-6 Luna")

    def test_cache_off_is_explicit_and_controls_lock_during_analysis(self):
        self.page.get_by_role("switch", name="使用缓存").uncheck()
        expect(self.page.locator("#cache-state-label")).to_have_text("关")
        expect(self.page.locator("#analysis-settings-note")).to_contain_text("可能产生费用")
        self.job_status = "running"
        self.page.locator("#compare-button").click()
        expect(self.page.get_by_role("switch", name="使用缓存")).to_be_disabled()
        expect(self.page.locator("#analysis-model")).to_be_disabled()
        self.assertEqual(self.requests[0]["use_cache"], False)
        self.assertFalse(self.page.locator("#error-banner").is_visible())
        self.job_status = "succeeded"
        expect(self.page.locator("#analysis-model")).to_be_enabled()

    def test_read_only_mode_keeps_cache_on_but_allows_selecting_cached_model(self):
        self.context.close()
        self.azure = False
        self.open_context()
        expect(self.page.get_by_role("switch", name="使用缓存")).to_be_checked()
        expect(self.page.get_by_role("switch", name="使用缓存")).to_be_disabled()
        expect(self.page.locator("#analysis-model")).to_be_enabled()
        self.page.locator("#analysis-model").select_option("gpt-6-luna")
        self.compare()
        self.assertTrue(self.requests[0]["use_cache"])

    def test_completed_timing_and_changed_settings_do_not_relabel_previous_result(self):
        self.compare()
        self.page.get_by_role("tab", name="用量与费用").click()
        expect(self.page.locator("#timing-total")).to_have_text("1 分 5.3 秒")
        expect(self.page.locator('[data-stage-id="cu_old"]')).to_contain_text("0.125 秒")
        expect(self.page.locator("#timing-content")).to_contain_text("GPT-6 Astra")
        self.page.locator("#analysis-model").select_option("gpt-6-luna")
        self.page.get_by_role("switch", name="使用缓存").uncheck()
        expect(self.page.locator("#analysis-settings-note")).to_contain_text("仅用于下一次对比")
        expect(self.page.locator("#timing-content")).to_contain_text("GPT-6 Astra")
        expect(self.page.locator("#timing-content")).to_contain_text("缓存：开启")
        expect(self.page.locator("#model-tag")).to_contain_text("GPT-6 Astra")
        self.assertEqual(len(self.requests), 1)

    def test_live_timing_preserves_tab_open_usage_details_and_partial_failure(self):
        self.job_status = "running"
        self.job_timing = timing("running", 9)
        self.page.locator("#compare-button").click()
        self.page.get_by_role("tab", name="用量与费用").click()
        expect(self.page.locator("#timing-total")).to_have_text("9.00 秒")
        expect(self.page.locator('[data-stage-id="model"]')).to_contain_text("进行中")
        self.page.locator("#usage-call-0 > summary").click()
        self.job_timing = timing("failed", 12)
        self.job_status = "failed"
        expect(self.page.locator("#error-message")).to_have_text("Synthetic analysis failure")
        expect(self.page.locator("#timing-total")).to_have_text("12.00 秒")
        expect(self.page.locator('[data-stage-id="model"]')).to_contain_text("失败，保留已耗时间")
        expect(self.page.locator("#usage-call-0")).to_have_attribute("open", "")
        expect(self.page.get_by_role("tab", name="用量与费用")).to_have_attribute("aria-selected", "true")
        expect(self.page.locator("#analysis-model")).to_be_enabled()
        self.assertGreaterEqual(self.polls, 2)

    def test_result_only_timing_unknown_values_and_untrusted_labels(self):
        self.result_only_timing = True
        self.job_timing = {"total_seconds": -3, "queue_seconds": None, "stages": [
            {"id": '"><img>', "label": '<img src=x onerror="alert(1)">',
             "status": "<script>", "elapsed_seconds": "123"},
        ]}
        self.compare()
        self.page.get_by_role("tab", name="用量与费用").click()
        expect(self.page.locator("#timing-total")).to_have_text("未提供")
        expect(self.page.locator("#timing-content img, #timing-content script")).to_have_count(0)
        expect(self.page.locator(".timing-stage")).to_contain_text("状态未提供")
        expect(self.page.locator(".timing-stage-time")).to_have_text("未提供")
        self.assertEqual(self.page.locator(".timing-stage-label").text_content(),
                         '<img src=x onerror="alert(1)">')

    def test_missing_timing_is_not_historical_usage_or_zero_and_new_upload_clears_it(self):
        self.job_timing = None
        self.compare()
        self.page.get_by_role("tab", name="用量与费用").click()
        expect(self.page.locator("#timing-content")).to_contain_text("暂无耗时数据")
        expect(self.page.locator("#timing-total")).to_have_count(0)
        self.job_timing = timing()
        self.compare()
        expect(self.page.locator("#timing-total")).to_have_text("1 分 5.3 秒")
        self.page.locator("#old-upload").set_input_files(
            {"name": "synthetic.pdf", "mimeType": "application/pdf", "buffer": b"synthetic"})
        expect(self.page.locator("#timing-content")).to_contain_text("暂无耗时数据")
        expect(self.page.locator("#timing-total")).to_have_count(0)

    def test_wrong_result_model_or_cache_option_is_rejected(self):
        self.override_options = {"use_cache": True, "model": "gpt-6-luna", "model_label": "GPT-6 Luna"}
        self.page.locator("#compare-button").click()
        expect(self.page.locator("#error-message")).to_contain_text("分析设置与本轮请求不一致")
        expect(self.page.locator(".result-item")).to_have_count(0)

    def test_controls_do_not_overflow_mobile_header(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        for selector in ("#use-cache", "#analysis-model", "#compare-button", "#export-button"):
            box = self.page.locator(selector).bounding_box()
            self.assertGreaterEqual(box["x"], 0)
            self.assertLessEqual(box["x"] + box["width"], 390)
        self.page.get_by_role("switch", name="使用缓存").focus()
        self.page.keyboard.press("Space")
        expect(self.page.get_by_role("switch", name="使用缓存")).not_to_be_checked()
