import unittest
from unittest.mock import Mock

from cu_diff.report import write_model_report


class ModelReportTests(unittest.TestCase):
    def render(self, items, coverage=None):
        path = Mock()
        write_model_report({"items": items, "coverage": coverage if coverage is not None else {"unprocessed": 2},
                            "warnings": ["Synthetic incomplete coverage."]}, path)
        return path.write_text.call_args.args[0]

    def test_visual_descriptions_provenance_limits_and_navigation_are_not_cu_text(self):
        attack = '<img src=x onerror="alert(1)">'
        item = {
            "id": "V001", "key": "合成左侧填充", "change": "model_visual_modified",
            "model_comparison": {"stage": "visual", "status": "visual_grounded",
                                 "highlight_scope": "nontext_residual_only",
                                 "rationale": attack, "issues": ["需复核"]},
            "visual_comparison": {"status": "localized", "kind": "fill",
                                  "description": attack, "limitations": ["合成抗锯齿限制"],
                                  "alignment": {"method": "synthetic"},
                                  "changed_pixels": {"old": 24, "new": 0},
                                  "source_sha256": {"old": "a" * 64}},
            "old": {"raw_text": "", "visual_description": "旧侧斜线", "confidence": None,
                    "source": [{"kind": "pdf_raster"}], "locations": [{"x": .2}]},
            "new": {"raw_text": "", "visual_description": "新侧未见斜线", "confidence": None,
                    "source": [{"kind": "pdf_raster"}], "locations": [],
                    "context_locations": [{"x": .1, "width": .4}]},
        }
        report = self.render([item])
        for phrase in ("模型生成描述（非OCR）：旧侧斜线", "模型生成描述（非OCR）：新侧未见斜线",
                       "非文字观察：1", "文字差异候选：0", "非CU词坐标", "合成抗锯齿限制",
                       "source_sha256", "changed_pixels", "不确认实体部件增删",
                       "仅导航搜索区域", "不等于已证明无变化", "Synthetic incomplete coverage."):
            self.assertIn(phrase, report)
        self.assertNotIn("<img", report)
        self.assertIn("&lt;img", report)
        item["change"] = "model_visual_review"
        item["model_comparison"]["status"] = "review_only"
        item["visual_comparison"]["status"] = "unresolved"
        self.assertIn("定位未解决", self.render([item]))
        self.assertIn("未测量", self.render([item]))
        self.assertIn("不是零变化", self.render([item]))
        item["visual_comparison"]["alignment"].update(accepted=True, scope="subfeature_neighborhood")
        item["visual_comparison"]["measurement_status"] = "measured"
        self.assertIn("已独立核验子特征周边公共轮廓", self.render([item]))
        self.assertIn("不代表整幅视图一致", self.render([item]))

    def test_visual_coverage_budget_usage_and_minimal_deferred_item(self):
        report = self.render([{
            "id": "V001", "key": "合成延后特征", "change": "model_visual_review",
            "model_comparison": {"stage": "visual", "status": "review_only"},
            "visual_comparison": {"status": "unresolved", "description": "合成非文字观察",
                                  "limitations": ["等待预算"], "changed_pixels": {"old": 0, "new": 0}},
            "old": None, "new": None,
            "model_context": {"old": {"locations": [{"page": 2}]}, "new": {"locations": [{"page": 2}]}},
        }], coverage={"visual": {
            "enabled": True, "max_regions": 1, "regions": [
                {"label": "合成区域甲", "status": "reviewed", "features": 2, "localized": 1,
                 "model": {"usage": {"input_tokens": 17, "output_tokens": 13, "total_tokens": 30}},
                 "limitations": ["另一特征仍需定位"], "no_visual_change_observed": False},
                {"label": "合成区域乙", "status": "unprocessed", "features": 0, "localized": 0,
                 "limitations": [], "no_visual_change_observed": False, "reason": "视觉预算耗尽"},
            ],
        }})
        for phrase in ("视觉区域预算上限：1", "与CU文字局部复读裁切数独立", "合成区域甲", "合成区域乙",
                       "已执行视觉复核", "未处理 / 延后（不代表无变化）", "视觉预算耗尽",
                       "另一特征仍需定位", "input_tokens", "total_tokens", "不计算跨通道总用量",
                       "未提供（不按零计）", "模型生成描述（非OCR）：未提供",
                       "model_context_navigation_only", "等待预算"):
            self.assertIn(phrase, report)
        disabled = self.render([], coverage={"visual": {"enabled": False, "max_regions": 0, "regions": []}})
        self.assertIn("视觉复核启用：否；视觉区域预算上限：0", disabled)

    def test_existing_text_evidence_and_coverage_are_preserved(self):
        report = self.render([{
            "id": "M001", "key": "合成额定值", "change": "model_text_modified",
            "model_comparison": {"stage": "fine", "status": "source_grounded"},
            "old": {"raw_text": "3A 125V", "locations": [{"x": .2}]},
            "new": {"raw_text": "8A 125V", "locations": [{"x": .6}]},
        }])
        for phrase in ("3A 125V", "8A 125V", "CU变化词来源", "文字差异候选：1",
                       "非文字观察：0", '"unprocessed": 2'):
            self.assertIn(phrase, report)

    def test_presence_report_never_calls_object_bounds_change_residuals(self):
        report = self.render([{
            "id": "M001", "key": "Synthetic one-sided drawing", "change": "model_visual_presence_review",
            "model_comparison": {"stage": "visual", "status": "presence_review", "route": "single_sided"},
            "visual_comparison": {"status": "presence_review", "measurement_status": "object_extent_only",
                                  "description": "Synthetic object observed", "counterpart_status": "not_found",
                                  "search_coverage": {"searched_pages": [1], "total_pages": 3, "complete": False},
                                  "changed_pixels": {"old": None, "new": None}},
            "old": None,
            "new": {"raw_text": "", "locations": [{"x": .2}], "visual_description": "Synthetic ink extent"},
        }])
        self.assertIn("对象待核范围（非变化残差", report)
        self.assertIn("未找到对应不证明不存在", report)
        self.assertIn("对侧搜索覆盖", report)
        self.assertIn("未测量", report)
        self.assertNotIn("实际残差像素：", report)
