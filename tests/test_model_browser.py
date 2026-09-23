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


def visual_item(identifier="V001", feature="左侧填充", x=.2, review=False):
    row = model_item(identifier, review)
    row.update(key=f"合成视图 / {feature}",
               change="model_visual_review" if review else "model_visual_modified")
    row["model_comparison"].update(
        stage="visual", status="review_only" if review else "visual_grounded",
        highlight_scope="nontext_residual_only")
    row["visual_comparison"] = {
        "status": "unresolved" if review else "localized", "kind": "fill",
        "description": "合成填充笔画不同，不推断部件删除", "limitations": ["栅格抗锯齿需复核"],
        "old_description": "旧图可见合成斜线", "new_description": "新图未见相同斜线",
        "alignment": {"accepted": not review, "method": "synthetic_translation"},
        "changed_pixels": {"old": 24, "new": 0},
        "source_sha256": {"old": "a" * 64, "new": "b" * 64},
    }
    for side in ("old", "new"):
        row[side] = graphics.source(
            [graphics.location(2, x, .5, .025, .035)] if side == "old" and not review else [],
            [graphics.location(2, x - .02, .48, .08, .1)], raw_text="")
        row[side].update(visual_description=row["visual_comparison"][f"{side}_description"],
                         confidence=None, source=[{"kind": "pdf_raster", "page": 2, "dpi": 144}])
    row["model_context"] = {side: model_source("UNCHANGED DIMENSION", page=1, x=.1)
                            for side in ("old", "new")}
    return row


def presence_item(identifier="P001"):
    row = visual_item(identifier, review=True)
    row.update(key="合成单侧详图", change="model_visual_presence_review", old=None)
    row["model_comparison"].update(status="presence_review", route="single_sided",
                                   highlight_scope="object_review_only")
    row["visual_comparison"] = {
        "status": "presence_review", "description": "新图可见合成对象，旧图所查页未找到对应。",
        "counterpart_status": "not_found", "presence_side": "new",
        "search_coverage": {"searched_pages": [1], "total_pages": 3, "complete": False},
        "measurement_status": "object_extent_only", "changed_pixels": {"old": None, "new": None},
        "limitations": ["未覆盖页待核；不是新增实物的证据"],
    }
    row["new"]["locations"] = [graphics.location(2, .55, .35, .2, .15)]
    row["new"]["source"] = [{"kind": "pdf_object_extent", "page": 2}]
    row["new"]["visual_description"] = "局部渲染可见笔画，模型对象仍需核对"
    row["model_context"]["old"] = None
    return row


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
                "model_comparison_deployment": getattr(self, "comparison_deployment", None),
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

    def test_model_tag_distinguishes_cu_and_comparison_deployments(self):
        self.comparison_deployment = "synthetic-next"
        self.page.reload()
        self.page.wait_for_load_state("networkidle")
        expect(self.page.locator("#model-tag")).to_contain_text("CU：synthetic-local")
        expect(self.page.locator("#model-tag")).to_contain_text("模型对比：synthetic-next")

    def test_visual_coverage_distinguishes_budget_deferred_features_and_direct_usage(self):
        attack = '<img src=x onerror="window.modelXss=1">'
        self.result["model_coverage"]["visual"] = {
            "enabled": True, "max_regions": 1, "regions": [
                {"label": "合成区域甲", "status": "reviewed", "features": 2, "localized": 1,
                 "model": {"usage": {"input_tokens": 17, "output_tokens": 13, "total_tokens": 30}},
                 "limitations": ["另一特征仍需人工定位"], "no_visual_change_observed": False},
                {"label": "合成区域乙", "status": "unprocessed", "features": 0, "localized": 0,
                 "limitations": [attack], "no_visual_change_observed": False,
                 "reason": "视觉区域预算已耗尽，特征延后"},
                {"label": "合成区域丙", "status": "reviewed", "features": 0, "localized": 0,
                 "limitations": [], "no_visual_change_observed": True},
            ],
        }
        self.compare()
        self.page.locator(".coverage-panel > summary").click()
        visual = self.page.locator(".model-visual-coverage")
        for phrase in ("视觉区域预算上限：1", "与CU文字局部复读裁切数独立",
                       "合成区域甲", "合成区域乙", "合成区域丙", "已执行视觉复核",
                       "模型观察特征", "已局部定位特征", "另一特征仍需人工定位",
                       "未处理 / 延后（不代表无变化）", "视觉区域预算已耗尽，特征延后",
                       "未观察到视觉变化（不保证无遗漏）", "直接视觉模型调用", "输入 token",
                       "17", "输出 token", "13", "该来源返回的 token 合计", "30",
                       "不计算跨通道总用量", "缺失用量不按零计", attack):
            expect(visual).to_contain_text(phrase)
        expect(self.page.locator(".model-coverage")).to_contain_text("局部复读覆盖")
        expect(visual.locator("img, script")).to_have_count(0)
        self.assertIsNone(self.page.evaluate("window.modelXss"))
        self.result["model_coverage"]["visual"] = {"enabled": False, "max_regions": 0, "regions": []}
        self.compare()
        expect(visual).to_contain_text("视觉复核启用：否")
        expect(visual).to_contain_text("视觉区域预算上限：0")
        expect(visual).not_to_contain_text("该来源返回的 token 合计")

    def test_minimal_deferred_visual_item_navigates_model_context_without_frames(self):
        row = visual_item(review=True)
        row["visual_comparison"] = {
            "status": "unresolved", "description": "合成非文字特征延后复核",
            "limitations": ["视觉预算限制"], "changed_pixels": {"old": 0, "new": 0},
        }
        for side in ("old", "new"):
            row[side].pop("context_locations")
            row[side].pop("visual_description")
            row["model_context"][side] = model_source("UNCHANGED DIMENSION", page=2)
        self.result["items"] = [row]
        for missing_sources in (False, True):
            if missing_sources:
                row["old"] = row["new"] = None
            self.compare()
            self.select("V001")
            expect(self.page.locator(".model-visual-detail")).to_contain_text("类型：未提供")
            expect(self.page.locator(".model-visual-detail")).to_contain_text("合成非文字特征延后复核")
            expect(self.page.locator(".model-visual-detail")).to_contain_text("视觉预算限制")
            expect(self.page.locator("rect.evidence-box")).to_have_count(0)
            for side in ("old", "new"):
                expect(self.page.locator(f"#{side}-page")).to_have_value("2")
                expect(self.page.locator(f"#{side}-evidence-note")).to_contain_text("不绘制变化框")
                expect(self.page.locator(f'.source-detail[data-side="{side}"]')).to_contain_text(
                    "未提供模型描述（不代表原图没有该特征）")
            self.assert_source_columns()

    def test_visual_subfeatures_have_independent_cards_precise_boxes_and_blank_side_navigation(self):
        left, right = visual_item(), visual_item("V002", "右侧轮廓", .7)
        self.result["items"] = [model_item(), left, right]
        self.compare()
        self.assertEqual(self.visible_ids(), ["M001", "V001", "V002"])
        expect(self.page.locator("#model-count")).to_contain_text("1 条局部原文不同候选 / 0 条待复核")
        expect(self.page.locator("#model-count")).to_contain_text("非文字：2 条填充／轮廓变化候选 / 0 条定位未解决")
        for item in (left, right):
            self.select(item["id"])
            expect(self.page.locator(f'.result-button[data-id="{item["id"]}"]')).to_contain_text(item["key"])
            expect(self.page.locator("#detail-meta")).to_contain_text("图纸填充／轮廓变化候选")
            for width in (1600, 720):
                self.page.set_viewport_size({"width": width, "height": 1050})
                self.assert_source_columns()
                for side in ("old", "new"):
                    section = self.page.locator(f'.source-detail[data-side="{side}"]')
                    expect(section).to_contain_text("模型生成描述（非OCR）")
                    expect(section.locator(".source-text")).to_have_text(item[side]["visual_description"])
                    expect(section).not_to_contain_text("原始文本")
                    expect(section).not_to_contain_text("置信度")
                    expect(self.page.locator(f"#{side}-page")).to_have_value("2")
                self.assert_geometry("old", item["id"], item["old"]["locations"][0])
            expect(self.page.locator(f'#old-stage rect[data-id="{item["id"]}"]')).to_have_class("evidence-box selected")
            expect(self.page.locator(f'#new-stage rect[data-id="{item["id"]}"]')).to_have_count(0)
            expect(self.page.locator("#new-evidence-note")).to_contain_text("仅导航搜索区域上下文，不绘制变化框")
            expect(self.page.locator("#old-evidence-note")).to_contain_text("非CU词框")
            expect(self.page.locator('#old-stage rect[data-channel="model"][data-change="model_visual_modified"]')).to_have_count(2)
        # Neither the broad search region nor the unchanged dimension's page-1 anchor becomes a frame.
        for side in ("old", "new"):
            self.page.locator(f"#{side}-page").select_option("1")
            expect(self.page.locator(f'#{side}-stage rect[data-change="model_visual_modified"]')).to_have_count(0)

    def test_single_side_object_shows_yellow_extent_not_red_or_phantom_counterpart(self):
        row = presence_item()
        self.result["items"] = [row]
        self.compare()
        self.select("P001")
        self.assert_geometry("new", "P001", row["new"]["locations"][0])
        rect = self.page.locator('#new-stage rect[data-id="P001"]')
        expect(rect).to_have_class("evidence-box review-evidence selected")
        expect(rect).to_have_attribute("data-evidence-role", "object_review_extent")
        expect(self.page.locator('#old-stage rect[data-id="P001"]')).to_have_count(0)
        expect(self.page.locator("#new-evidence-note")).to_contain_text("对象待核范围，非变化残差")
        expect(self.page.locator("#old-evidence-note")).to_contain_text("不代表不存在")
        expect(self.page.locator(".model-visual-detail")).to_contain_text("对侧已查 1 / 3 页")
        expect(self.page.locator(".model-visual-detail")).to_contain_text("未覆盖页面仍需检查")
        expect(self.page.locator(".visual-measurement")).to_contain_text("未测量")
        expect(self.page.locator('.source-detail[data-side="new"]')).to_contain_text("非变化残差")
        expect(self.page.locator("#model-count")).to_contain_text("1 条单侧范围待核")
        self.page.locator("#review-filter").select_option("unpaired")
        self.assertEqual(self.visible_ids(), ["P001"])
        self.assertEqual(self.compare_requests, 1)

    def test_context_and_model_no_change_are_not_default_differences_but_deferred_is_visible(self):
        rows = []
        for identifier, change in (("V001", "model_visual_context"),
                                   ("V002", "model_visual_no_change"),
                                   ("V003", "model_visual_deferred")):
            row = visual_item(identifier, review=True)
            row["change"] = change
            row["visual_comparison"].update(measurement_status="unmeasured")
            rows.append(row)
        self.result["items"] = rows
        self.compare()
        self.assertEqual(self.visible_ids(), ["V003"])
        self.select("V003")
        expect(self.page.locator(".model-visual-detail")).to_contain_text("高清复核尚未执行")
        expect(self.page.locator("#model-count")).to_contain_text("1 条尚未执行")
        expect(self.page.locator(".model-visual-detail")).not_to_contain_text("模型已发现疑点")
        expect(self.page.locator("rect.evidence-box")).to_have_count(0)
        self.page.locator("#review-filter").select_option("review")
        self.assertEqual(self.visible_ids(), ["V001", "V002", "V003"])
        self.select("V001")
        expect(self.page.locator(".model-visual-detail")).to_contain_text("粗模型未见明确内容变化")
        self.select("V002")
        expect(self.page.locator(".model-visual-detail")).to_contain_text("不代表已证明整个区域完全相同")
        expect(self.page.locator("rect.evidence-box")).to_have_count(0)

    def test_visual_unresolved_and_incomplete_grounding_never_use_confirmed_red_boxes(self):
        row = visual_item(review=True)
        self.result["items"] = [row]
        self.compare()
        self.select("V001")
        expect(self.page.locator(".model-visual-detail")).to_contain_text("定位未解决")
        expect(self.page.locator("rect.evidence-box")).to_have_count(0)
        expect(self.page.locator("#model-count")).to_contain_text("非文字：0 条填充／轮廓变化候选 / 1 条定位未解决")
        for mutation in ("stage", "status", "scope", "visual_status", "source"):
            with self.subTest(mutation=mutation):
                row = visual_item()
                if mutation == "stage":
                    row["model_comparison"]["stage"] = "fine"
                elif mutation == "status":
                    row["model_comparison"]["status"] = "review_only"
                elif mutation == "scope":
                    row["model_comparison"]["highlight_scope"] = "context"
                elif mutation == "visual_status":
                    row["visual_comparison"]["status"] = "unresolved"
                else:
                    row["old"]["source"] = [{"kind": "cu-fine-crop"}]
                self.result["items"] = [row]
                self.compare()
                self.select("V001")
                expect(self.page.locator('#old-stage rect[data-id="V001"]')).to_have_class(
                    "evidence-box review-evidence selected")

    def test_visual_both_side_residuals_are_precise_and_missing_old_is_navigation_only(self):
        row = visual_item()
        row["new"]["locations"] = [graphics.location(2, .6, .52, .03, .045)]
        row["visual_comparison"]["changed_pixels"]["new"] = 32
        self.result["items"] = [row]
        self.compare()
        self.select("V001")
        for side in ("old", "new"):
            self.assert_geometry(side, "V001", row[side]["locations"][0])
            expect(self.page.locator(f'#{side}-stage rect[data-id="V001"]')).to_have_class(
                "evidence-box selected")
        row["old"]["locations"] = []
        row["visual_comparison"]["changed_pixels"]["old"] = 0
        self.compare()
        self.select("V001")
        expect(self.page.locator("#old-page")).to_have_value("2")
        expect(self.page.locator('#old-stage rect[data-id="V001"]')).to_have_count(0)
        expect(self.page.locator("#old-evidence-note")).to_contain_text("不绘制变化框")
        self.assert_geometry("new", "V001", row["new"]["locations"][0])

    def test_subfeature_fallback_and_unmeasured_are_not_displayed_as_zero_changes(self):
        row = visual_item()
        row["visual_comparison"].update(measurement_status="measured")
        row["visual_comparison"]["alignment"].update(
            scope="subfeature_neighborhood", whole_view_alignment={"accepted": False})
        self.result["items"] = [row]
        self.compare()
        self.select("V001")
        expect(self.page.locator(".visual-alignment-scope")).to_contain_text("已独立核验子特征周边公共轮廓")
        expect(self.page.locator(".visual-alignment-scope")).to_contain_text("不代表整幅视图一致")
        expect(self.page.locator(".visual-measurement")).to_contain_text("24 / 0")
        row = visual_item(review=True)
        row["visual_comparison"].update(measurement_status="unmeasured", changed_pixels={"old": None, "new": None})
        self.result["items"] = [row]
        self.compare()
        self.select("V001")
        expect(self.page.locator(".model-visual-detail")).to_contain_text("模型已发现疑点，但位置尚未核实")
        expect(self.page.locator(".visual-measurement")).to_contain_text("未测量")
        expect(self.page.locator(".visual-measurement")).to_contain_text("不是零变化")
        expect(self.page.locator("rect.evidence-box")).to_have_count(0)
        row["visual_comparison"].pop("measurement_status")
        row["visual_comparison"]["changed_pixels"] = {"old": 0, "new": 0}
        self.compare()
        self.select("V001")
        expect(self.page.locator(".visual-measurement")).to_contain_text("未测量")
        expect(self.page.locator(".visual-measurement")).not_to_contain_text("0 / 0")

    def test_visual_navigation_uses_model_context_only_when_search_context_is_missing(self):
        row = visual_item(review=True)
        for side in ("old", "new"):
            row[side]["context_locations"] = []
            row["model_context"][side]["locations"] = [graphics.location(2, .6, .6, .2, .2)]
        self.result["items"] = [row]
        self.compare()
        self.select("V001")
        for side in ("old", "new"):
            expect(self.page.locator(f"#{side}-page")).to_have_value("2")
        expect(self.page.locator("rect.evidence-box")).to_have_count(0)

    def test_visual_model_prose_and_provenance_are_text_not_html(self):
        attack = '<img src=x onerror="window.modelXss=1"><script>window.modelXss=1</script>'
        row = visual_item()
        row["visual_comparison"].update(description=attack, limitations=[attack],
                                        alignment={"method": attack})
        row["model_comparison"].update(pair_label=attack, rationale=attack, issues=[attack])
        row["old"]["visual_description"] = attack
        row["new"]["visual_description"] = attack
        row["visual_comparison"]["source_sha256"]["old"] = "synthetic-" * 250
        self.result["items"] = [row]
        self.compare()
        self.select("V001")
        self.page.locator(".visual-provenance summary").click()
        expect(self.page.locator(".model-visual-detail")).to_contain_text(attack)
        expect(self.page.locator(".source-detail").first).to_contain_text(attack)
        expect(self.page.locator("#detail-content img, #detail-content script")).to_have_count(0)
        self.assertIsNone(self.page.evaluate("window.modelXss"))
        self.page.set_viewport_size({"width": 720, "height": 1050})
        self.assert_source_columns()
        self.assertTrue(self.page.locator(".visual-provenance pre").evaluate(
            "n => n.scrollWidth <= n.clientWidth + 1"))

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
