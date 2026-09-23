"""Offline model-guided UI checks: synthetic sources, mocked API, no Azure or files."""

import unittest
from urllib.parse import urlparse

from tests import test_graphics_browser as graphics

expect = graphics.expect


def model_source(value, page=1, x=.2, y=.3):
    result = graphics.source([graphics.location(page, x, y, .12, .04)], raw_text=value)
    result.update(detail="CU局部复读说明", confidence=.72,
                  source={"kind": "cu-fine-crop", "page": page})
    return result


def model_item(identifier="M001", review=False):
    return {
        "id": identifier, "channel": "model", "key": "合成额定值", "region": "合成标注",
        "change": "model_review" if review else "model_text_modified",
        "old": model_source("3A 125V"), "new": model_source("8A 125V", x=.6, y=.55),
        "review_required": True, "review_reasons": ["模型提议的对应关系仍需确认"],
        "match": {"method": "model_semantic_pairing_and_cu_crop_evidence",
                  "certainty": "model_proposed", "score": None},
        "model_comparison": {
            "status": "review_only" if review else "source_grounded",
            "stage": "coarse" if review else "fine",
            "rationale": "可能对应同一额定值标注", "pair_label": "额定值",
            "issues": ["局部预算已用完"] if review else [],
        },
    }


class ModelBrowserTests(unittest.TestCase):
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
    visible_ids = graphics.GraphicsBrowserTests.visible_ids
    assert_geometry = graphics.GraphicsBrowserTests.assert_geometry
    assert_source_columns = graphics.GraphicsBrowserTests.assert_source_columns

    def setUp(self):
        self.model_enabled = True
        graphics.GraphicsBrowserTests.setUp(self)
        self.result["items"].extend([model_item(), model_item("M002", review=True)])
        self.result["model_coverage"] = {
            "enabled": True, "status": "completed_with_limits",
            "coarse": {"paired": 3, "max_pairs": 3},
            "fine": {"processed": 1, "crops": 2, "max_crops": 2},
            "unprocessed": [{"pair_label": "合成区域3", "issues": ["预算上限，缺少可靠来源"]}],
            "usage": {"model_tokens": 123, "cu_crops": 2},
            "warnings": ["未处理区域不代表无变化"],
        }

    def route(self, route):
        url = urlparse(route.request.url)
        if url.netloc == "graphics-ui.test" and url.path == "/api/bootstrap":
            payload = {
                "csrf_token": "synthetic-csrf", "revision": self.revision,
                "azure_enabled": False, "model": "synthetic-local",
                "documents": self.documents, "storage_notice": "合成测试会话",
                "graphics_enabled": self.graphics_enabled,
                "limits": {"max_bytes": 20971520, "max_pages": 20, "session_ttl_hours": 24},
            }
            if self.model_enabled is not None:
                payload["model_comparison_enabled"] = self.model_enabled
            route.fulfill(json=payload)
        else:
            graphics.GraphicsBrowserTests.route(self, route)

    def test_defaults_keep_legacy_evidence_and_show_separate_model_counts(self):
        expect(self.page.locator("#model-status")).to_contain_text("模型引导对比已启用")
        expect(self.page.locator("#model-status")).to_contain_text("模型及CU费用")
        expect(self.page.locator("#model-status")).to_contain_text("不保证没有遗漏")
        expect(self.page.locator('#channel-filter option[value="primary"]')).to_have_text(
            "模型 + 字段 + 表格 + 图形")
        self.compare()
        self.assertEqual(set(self.visible_ids()), {"M001", "M002", "D001", "G001", "G002", "G007"})
        self.assertEqual(self.visible_ids()[:2], ["M001", "M002"])
        expect(self.page.locator("#job-status")).to_contain_text("5 条已配对差异候选")
        expect(self.page.locator("#model-count")).to_contain_text("1 条局部原文不同候选 / 1 条待复核")
        expect(self.page.locator("#model-count")).to_contain_text("不相加为独立变更数")
        self.page.locator("#channel-filter").select_option("model")
        self.assertEqual(self.visible_ids(), ["M001", "M002"])
        self.page.locator("#review-filter").select_option("uncertain")
        self.assertEqual(self.visible_ids(), ["M001", "M002"])
        self.page.locator("#review-filter").select_option("review")
        self.assertEqual(self.visible_ids(), ["M001", "M002"])
        self.page.locator(".coverage-panel > summary").click()
        for phrase in ("粗比对覆盖", "局部复读覆盖", "未处理 / 预算及来源限制",
                       "预算上限，缺少可靠来源", "用量（不是金额）", "123", "不保证没有遗漏"):
            expect(self.page.locator(".model-coverage")).to_contain_text(phrase)
        self.assertEqual(self.compare_requests, 1)

    def test_fine_evidence_geometry_and_source_columns_across_viewports(self):
        self.compare()
        self.page.locator("#channel-filter").select_option("model")
        self.select("M001")
        expect(self.page.locator(".model-detail")).to_contain_text(
            "模型提出对应关系，CU局部复读提供原文；不是模型文字直接作为证据")
        expect(self.page.locator("#detail-meta")).to_contain_text("模型语义配对 + CU局部复读证据")
        expect(self.page.locator("#detail-meta")).not_to_contain_text("model_semantic_pairing")
        expect(self.page.locator("#detail-meta")).to_contain_text("不是独立确认的工程事实")
        row = self.result["items"][-2]
        for width in (1600, 1024, 720):
            self.page.set_viewport_size({"width": width, "height": 1050})
            self.assert_source_columns()
            for side in ("old", "new"):
                expect(self.page.locator(f'.source-detail[data-side="{side}"] .source-text')).to_have_text(
                    row[side]["raw_text"])
                for zoom in ("fit", "150", "200"):
                    self.page.locator(f"#{side}-zoom").select_option(zoom)
                    self.assert_geometry(side, "M001", row[side]["locations"][0])
                    expect(self.page.locator(f'#{side}-stage rect[data-id="M001"]')).to_have_class(
                        "evidence-box selected")

    def test_review_frames_are_yellow_even_with_two_different_texts(self):
        self.compare()
        self.page.locator("#channel-filter").select_option("model")
        self.select("M002")
        self.assert_source_columns()
        expect(self.page.locator(".model-detail")).to_contain_text("预算/定位限制")
        expect(self.page.locator(".model-detail")).to_contain_text("局部预算已用完")
        for side in ("old", "new"):
            rect = self.page.locator(f'#{side}-stage rect[data-id="M002"]')
            expect(rect).to_have_class("evidence-box review-evidence selected")
            self.assertTrue(rect.evaluate("n => getComputedStyle(n).strokeDasharray !== 'none'"))
            self.assertEqual(rect.evaluate("n => getComputedStyle(n).stroke"),
                             self.page.locator(f'#{side}-stage .evidence-label.review-evidence').evaluate(
                                 "n => getComputedStyle(n).fill"))
            expect(self.page.locator(f"#{side}-evidence-note")).to_contain_text("黄色虚框")

    def test_review_shows_separate_visual_and_text_suggestions_not_unchanged_boxes(self):
        item = model_item(review=True)
        item["model_context"] = {side: model_source("UNCHANGED DIMENSION", x=.1) for side in ("old", "new")}
        item["model_comparison"]["observations"] = [
            {"kind": "text_change", "description": "Synthetic rating differs", "check": "Read the rating."},
            {"kind": "visual_change", "description": "旧版有斜线填充，新版未见该填充",
             "check": "<img src=x onerror=alert(1)> 核对填充笔画，不推断部件删除"},
            {"kind": "unchanged_text", "description": "UNCHANGED DIMENSION仍存在", "check": "保留上下文"},
        ]
        self.result["items"] = [item]
        self.compare()
        self.select("M001")
        expect(self.page.locator(".model-visual-observation")).to_contain_text("旧版有斜线填充")
        expect(self.page.locator(".model-visual-observation")).to_contain_text("不是OCR原文")
        expect(self.page.locator(".model-observations img")).to_have_count(0)
        self.page.locator(".model-unchanged-context summary").click()
        expect(self.page.locator(".model-unchanged-context")).to_contain_text("不高亮")
        for side in ("old", "new"):
            expect(self.page.locator(f'#{side}-stage rect[data-id="M001"]')).to_have_count(1)
            self.assert_geometry(side, "M001", item[side]["locations"][0])
        self.assert_source_columns()

    def test_no_text_change_is_hidden_by_default_and_has_no_highlight(self):
        item = model_item(review=True)
        item["change"] = "model_no_text_change"
        item["model_comparison"].update(stage="fine", status="no_text_change_observed")
        item["model_context"] = {side: model_source("SYN SAME", page=2) for side in ("old", "new")}
        for side in ("old", "new"):
            item[side]["locations"] = []
            item[side]["raw_text"] = "SYN SAME"
        self.result["items"] = [item]
        self.compare()
        self.assertEqual(self.visible_ids(), [])
        self.page.locator("#review-filter").select_option("review")
        self.assertEqual(self.visible_ids(), ["M001"])
        self.select("M001")
        expect(self.page.locator(".model-text-cleared")).to_contain_text("默认不列入差异候选")
        for side in ("old", "new"):
            expect(self.page.locator(f'#{side}-stage rect[data-id="M001"]')).to_have_count(0)
            expect(self.page.locator(f"#{side}-page")).to_have_value("2")

    def test_missing_sources_navigate_context_without_fabricated_frames(self):
        for absent in ("old", "new"):
            with self.subTest(absent=absent):
                row = model_item(review=True)
                row[absent] = None
                row["model_context"] = {
                    side: model_source(f"{side}导航上下文", page=2, x=.4, y=.6)
                    for side in ("old", "new")
                }
                self.result["items"] = [row]
                self.compare()
                self.assertEqual(self.visible_ids(), ["M001"])
                self.select("M001")
                self.assert_source_columns()
                expect(self.page.locator(f"#{absent}-page")).to_have_value("2")
                expect(self.page.locator(f'#{absent}-stage rect[data-id="M001"]')).to_have_count(0)
                expect(self.page.locator(f"#{absent}-evidence-note")).to_contain_text(
                    "仅导航模型上下文，不绘制变化框")
                expect(self.page.locator(f'.source-detail[data-side="{absent}"]')).to_contain_text(
                    "未配对到证据（不代表原图没有）")
                self.page.locator(".model-context summary").click()
                expect(self.page.locator(".model-context")).to_contain_text(f"{absent}导航上下文")
                present = "new" if absent == "old" else "old"
                expect(self.page.locator(f"#{present}-page")).to_have_value("1")
                self.assert_geometry(present, "M001", row[present]["locations"][0])
        row["old"] = row["new"] = None
        self.compare()
        self.select("M001")
        expect(self.page.locator("rect.evidence-box")).to_have_count(0)
        for side in ("old", "new"):
            expect(self.page.locator(f"#{side}-page")).to_have_value("2")
        row.pop("model_context")
        self.compare()
        self.select("M001")
        expect(self.page.locator("rect.evidence-box")).to_have_count(0)
        for side in ("old", "new"):
            expect(self.page.locator(f"#{side}-evidence-note")).to_contain_text("不绘制推测框")

    def test_missing_locations_are_not_promoted_from_context_or_invalid_anchors(self):
        row = model_item()
        row["old"]["locations"] = []
        row["old"]["location_error"] = "局部来源定位失败"
        row["new"]["locations"] = [graphics.location(page=9), graphics.location(x=2)]
        row["model_context"] = {side: model_source("仅导航", page=2) for side in ("old", "new")}
        self.result["items"] = [row]
        self.compare()
        self.select("M001")
        expect(self.page.locator("rect.evidence-box")).to_have_count(0)
        for side in ("old", "new"):
            expect(self.page.locator(f"#{side}-page")).to_have_value("2")
            expect(self.page.locator(f"#{side}-evidence-note")).to_contain_text("本侧无变化词框，仅导航已提取上下文，非本侧变化证据")
        expect(self.page.locator("#old-evidence-note")).to_contain_text("局部来源定位失败")

    def test_inserted_or_deleted_word_only_frames_the_changed_side_not_phrase_context(self):
        for unchanged_side in ("old", "new"):
            with self.subTest(unchanged_side=unchanged_side):
                changed_side = "new" if unchanged_side == "old" else "old"
                row = model_item()
                row[unchanged_side] = model_source("CABLE", page=2)
                row[changed_side] = model_source("CABLE UL", page=2)
                word = graphics.location(2, .64, .3, .025, .04)
                phrase = graphics.location(2, .2, .3, .47, .04)
                row[unchanged_side]["locations"] = []
                row[changed_side]["locations"] = [word]
                row["model_context"] = {}
                for side in ("old", "new"):
                    row[side]["context_locations"] = [phrase]
                    row[side]["source"] = {"request_sha256": "synthetic-" * 250}
                    row["model_context"][side] = model_source(row[side]["raw_text"], page=2)
                    row["model_context"][side]["locations"] = [phrase]
                self.result["items"] = [row]
                self.compare()
                self.select("M001")
                for side in ("old", "new"):
                    expect(self.page.locator(f"#{side}-page")).to_have_value("2")
                    expect(self.page.locator(f'.source-detail[data-side="{side}"] .source-text')).to_have_text(
                        row[side]["raw_text"])
                expect(self.page.locator("rect.evidence-box")).to_have_count(1)
                expect(self.page.locator(f'#{changed_side}-stage rect[data-id="M001"]')).to_have_class(
                    "evidence-box selected")
                self.assert_geometry(changed_side, "M001", word)
                expect(self.page.locator(f"#{unchanged_side}-evidence-note")).to_contain_text(
                    "本侧无变化词框，仅导航已提取上下文，非本侧变化证据")
                expect(self.page.locator(".model-detail")).to_contain_text("不将整句或定位锚点标红")
                for width in (1600, 720):
                    self.page.set_viewport_size({"width": width, "height": 1050})
                    self.assert_source_columns()
                    for meta in self.page.locator(".source-meta").all():
                        self.assertTrue(meta.evaluate("n => n.scrollWidth <= n.clientWidth + 1"))

    def test_incomplete_fine_contract_never_gets_red_frames(self):
        for mutation in ("stage", "status", "missing", "same_text"):
            with self.subTest(mutation=mutation):
                row = model_item()
                if mutation == "stage":
                    row["model_comparison"]["stage"] = "coarse"
                elif mutation == "status":
                    row["model_comparison"]["status"] = "review_only"
                elif mutation == "missing":
                    row["new"] = None
                else:
                    row["new"]["raw_text"] = row["old"]["raw_text"]
                self.result["items"] = [row]
                self.compare()
                self.select("M001")
                expect(self.page.locator("#model-count")).to_contain_text("0 条局部原文不同候选 / 1 条待复核")
                for rect in self.page.locator("rect.evidence-box").all():
                    expect(rect).to_have_class("evidence-box review-evidence selected")

    def test_untrusted_model_and_source_strings_are_text_only(self):
        attack = '<img src=x onerror="window.modelXss=1"><script>window.modelXss=1</script>'
        row = model_item()
        row["key"] = attack
        for key in ("rationale", "pair_label"):
            row["model_comparison"][key] = attack
        row["model_comparison"]["issues"] = [attack]
        row["model_context"] = {"old": model_source(attack), "new": model_source(attack)}
        row["old"].update(raw_text=attack, detail=attack, location_error=attack)
        row["old"]["source"]["kind"] = attack
        row["review_reasons"] = [attack]
        self.result["items"] = [row]
        self.result["model_coverage"]["warnings"] = [attack]
        self.compare()
        self.select("M001")
        self.page.locator(".model-context summary").click()
        self.page.locator(".coverage-panel > summary").click()
        expect(self.page.locator(".model-detail")).to_contain_text(attack)
        expect(self.page.locator('.source-detail[data-side="old"]')).to_contain_text(attack)
        expect(self.page.locator(".model-coverage")).to_contain_text(attack)
        expect(self.page.locator("#detail-content img, #detail-content script, #coverage-content img, #results-list img")).to_have_count(0)
        self.assertIsNone(self.page.evaluate("window.modelXss"))

    def test_disabled_or_absent_model_flag_preserves_legacy_controls(self):
        for enabled in (False, None):
            with self.subTest(enabled=enabled):
                self.model_enabled = enabled
                self.result.pop("model_coverage", None)
                self.result["items"] = [row for row in self.result["items"] if row["channel"] != "model"]
                self.page.reload()
                self.page.wait_for_load_state("networkidle")
                expect(self.page.locator("#model-status")).to_be_hidden()
                expect(self.page.locator('#channel-filter option[value="primary"]')).to_have_text(
                    "字段 + 表格 + 图形候选")
                self.compare()
                self.assertEqual(set(self.visible_ids()), {"D001", "G001", "G002", "G007"})
                expect(self.page.locator("#model-count")).to_be_hidden()
                expect(self.page.locator(".model-coverage")).to_have_count(0)
        self.graphics_enabled = False
        self.page.reload()
        self.page.wait_for_load_state("networkidle")
        expect(self.page.locator('#channel-filter option[value="primary"]')).to_have_text("字段 + 表格（默认）")

    def test_enabled_without_coverage_does_not_claim_completion(self):
        self.result.pop("model_coverage")
        self.compare()
        self.page.locator(".coverage-panel > summary").click()
        expect(self.page.locator("#coverage-content")).to_contain_text(
            "本轮未返回模型覆盖统计，不能认定模型已完成全部比较或没有遗漏")

    def test_model_evidence_is_independent_of_graphics_availability(self):
        self.graphics_enabled = False
        self.page.reload()
        self.page.wait_for_load_state("networkidle")
        expect(self.page.locator('#channel-filter option[value="primary"]')).to_have_text("模型 + 字段 + 表格")
        self.compare()
        self.assertEqual(set(self.visible_ids()), {"D001", "M001", "M002"})
        self.select("M001")
        self.assert_source_columns()
        self.assert_geometry("old", "M001", self.result["items"][-2]["old"]["locations"][0])
