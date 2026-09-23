"""Chinese evidence tables without external services or active HTML."""

import json
from html import escape
from pathlib import Path


CHANGE_LABELS = {
    "modified": "提取原文不同",
    "relocated": "行号/位置重排",
    "interpretation_only": "仅生成解释不同（不是原文变更）",
    "formatting_only": "仅日期空格/表格格式不同（不计变更）",
    "unpaired_old": "仅旧侧未配对（不确认删除）",
    "unpaired_new": "仅新侧未配对（不确认新增）",
    "table_row_added": "对应CU表格的新增行候选（需原图确认）",
    "table_row_removed": "对应CU表格的删除行候选（需原图确认）",
    "table_cell_modified": "表格单元格文字不同",
    "table_column_added": "表格新增列候选",
    "table_column_removed": "表格删除列候选",
    "table_grid_changed": "表格网格结构不同（不等于记录增删）",
    "annotation_occurrence_changed": "图外标注提取次数不同（不确认增删）",
    "unchanged": "提取原文相同",
    "split_merge": "OCR拆分/合并候选",
}


def cell(value: object) -> str:
    if value is None:
        return "未提供"
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    return value.replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def evidence(item: dict | None) -> str:
    if item is None:
        return "未配对（不等同于不存在）"
    return cell(item.get("raw_text", item.get("text", "")))


def write_table_report(result: dict, path: Path) -> None:
    lines = [
        "# 非BOM表格专项证据", "",
        "使用原PDF的本地几何证据及已有CU提取，不调用Azure。"
        "文字、列结构和网格行数分别报告；网格行数包含表头与空白行，不等于业务记录数。"
        "未识别或未配对不代表无变化，OCR文字仍需复核。"
        "本通道可能与结构化字段/OCR重复，不相加为独立变更数量。", "",
        "| 编号 | 项目 | 分类 | 旧侧证据 | 新侧证据 | 旧 / 新坐标 |",
        "|---|---|---|---|---|---|",
    ]
    for item in result.get("items", []):
        old, new = item.get("old"), item.get("new")
        lines.append("| " + " | ".join([
            cell(item.get("id")), cell(item.get("key")),
            cell(CHANGE_LABELS.get(item.get("change"), item.get("change"))),
            evidence(old), evidence(new),
            cell({"old": (old or {}).get("locations"), "new": (new or {}).get("locations")}),
        ]) + " |")
    lines.extend(["", "## 覆盖与限制", "", "```json",
                  json.dumps(result.get("coverage", {}), ensure_ascii=False, indent=2), "```", ""])
    lines.extend("- " + cell(warning) for warning in result.get("warnings", []))
    lines.extend(["", "## 可追溯性", "", "```json",
                  json.dumps(result.get("provenance", {}), ensure_ascii=False, indent=2), "```", "",
                  "JSON保留每项的表格对应依据、实际来源以及独立的对侧表格导航上下文。"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_model_report(result: dict, path: Path) -> None:
    def report_cell(value: object) -> str:
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        return cell(escape(value))

    def visual_item(item: dict) -> bool:
        return item.get("model_comparison", {}).get("stage") == "visual" or item.get("change") in (
            "model_visual_modified", "model_visual_review")

    visual_items = [item for item in result["items"] if visual_item(item)]
    text_count = sum(item.get("change") == "model_text_modified" for item in result["items"])
    lines = ["# 模型配对、CU局部复读与非文字栅格证据", "",
             "模型仅提出语义对应关系。文字通道原文和词位置取自CU来源；"
             "非文字通道使用本地PDF栅格残差，模型生成描述明确标记非OCR，不是提取原文。差异仍是候选，"
             "未引用、未复读或模型认为未变的区域不等于已证明无变化。", "",
             f"文字差异候选：{text_count}；非文字观察：{len(visual_items)}"
             "（含定位未解决项，独立计数，不相加为已确认工程变更）。", "",
             "| 编号 | 项目 | 阶段 / 状态 | 旧侧证据（CU原文或非OCR描述） | 新侧证据（CU原文或非OCR描述） | 定位依据与来源坐标 |",
             "|---|---|---|---|---|---|"]
    for item in result["items"]:
        meta = item["model_comparison"]
        visual = visual_item(item)
        descriptions = []
        for side in ("old", "new"):
            source = item.get(side)
            descriptions.append(
                "模型生成描述（非OCR）：" + ((source or {}).get("visual_description")
                or item.get("visual_comparison", {}).get(f"{side}_description") or "未提供")
                if visual else (source.get("raw_text", source.get("text", ""))
                                if source is not None else "未配对（不等同于不存在）"))
        lines.append("| " + " | ".join([
            report_cell(item["id"]), report_cell(item["key"]), report_cell(f"{meta['stage']} / {meta['status']}"),
            *(report_cell(description) for description in descriptions),
            ("本地PDF栅格非文字残差（非CU词坐标）；" if visual else "CU变化词来源；") +
            report_cell({side: (item.get(side) or {}).get("locations") for side in ("old", "new")}),
        ]) + " |")
    if visual_items:
        lines.extend(["", "## 图纸填充／轮廓变化候选 · 非OCR", "",
                      "非文字描述是模型生成观察，不确认实体部件增删。仅 locations 中实际测量的局部残差可作变化框；"
                      "context_locations 和 model_context 仅导航搜索区域，不作为变化证据。"
                      "visual_grounded / localized 表示局部栅格支持，不是已确认工程结论；"
                      "review_only / unresolved 表示定位未解决，不作确认变更红框。"])
        for item in visual_items:
            visual = item.get("visual_comparison", {})
            lines.extend(["", f"### {report_cell(item['id'])} · {report_cell(item['key'])}", "",
                          "模型观察（非OCR）：" + report_cell(visual.get("description", "未提供")), "",
                          "定位限制：" + report_cell(visual.get("limitations", "未提供")), "",
                          "本地定位、对齐、来源哈希与模型提议（非CU词坐标）：", "",
                          report_cell({"model_comparison": item.get("model_comparison"),
                                       "visual_comparison": visual,
                                       "sources": {side: item.get(side) for side in ("old", "new")},
                                       "model_context_navigation_only": item.get("model_context")})])
    visual_coverage = result["coverage"].get("visual")
    if visual_coverage is not None:
        enabled = visual_coverage.get("enabled")
        lines.extend(["", "## 非文字视觉覆盖 · 独立预算与延后项", "",
                      f"视觉复核启用：{'是' if enabled is True else '否' if enabled is False else '未提供'}；"
                      "视觉区域预算上限：" + report_cell(visual_coverage.get("max_regions", "未提供")), "",
                      "视觉区域预算与CU文字局部复读裁切数独立；未处理、零特征或未观察到视觉变化不证明无变化。"
                      "用量按来源列示，保留各区域直接视觉模型调用返回的 token，不计算跨通道总用量或费用；缺失用量不按零计。", "",
                      "| 区域 | 视觉复核状态 | 模型观察特征 | 已局部定位特征 | 未观察到视觉变化（不保证无遗漏） | 限制 / 延后原因 | 直接视觉模型调用及用量 |",
                      "|---|---|---|---|---|---|---|"])
        statuses = {"reviewed": "已执行视觉复核（非全部变化已确认）",
                    "unprocessed": "未处理 / 延后（不代表无变化）"}
        for region in visual_coverage.get("regions", []):
            lines.append("| " + " | ".join(report_cell(value) for value in (
                region.get("label", "未提供"), statuses.get(region.get("status"), region.get("status", "未提供")),
                region.get("features", "未提供"), region.get("localized", "未提供"),
                region.get("no_visual_change_observed", "未提供"),
                {"limitations": region.get("limitations"), "reason": region.get("reason")},
                region.get("model", "未提供（不按零计）"),
            )) + " |")
    lines.extend(["", "## 覆盖、用量与未解决项", "", "```json",
                  json.dumps(result["coverage"], ensure_ascii=False, indent=2), "```", ""])
    lines.extend("- " + report_cell(warning) for warning in result["warnings"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_report(result: dict, path: Path) -> None:
    lines = [
        "# 工程图 CU 证据差异报告",
        "",
        "**范围与限制：** 两份文件按用户指定的新旧顺序独立进行 CU 全页提取，"
        "再在本地配对。下表是提取值差异候选，不是已验证的全部工程变更。"
        "OCR 和字段提取均可能遗漏或误读；未配对不直接证明新增/删除。"
        "置信度来自提取服务；配对分数是启发式相似度，不是正确率。",
        "",
        "**坐标：** 保留 CU 原始 source（D 编码）及页面单位；"
        "PDF 页面为 inch 时乘 72 得到 PDF pt。裁剪输入必须结合旁侧 mapping.json "
        "加回原始裁剪偏移；不得直接将裁剪坐标当作原页坐标。",
        "",
        "## 结构化字段差异",
        "",
        "| # | 区域 / 项目 | 旧图原文 | 新图原文 | 分类 | 旧 / 新证据坐标 | 提取置信度（旧 / 新） | 配对与复核 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for number, diff in enumerate(result.get("differences", []), 1):
        old, new = diff.get("old"), diff.get("new")
        match = diff.get("match", {})
        row_change = diff.get("change") in ("table_row_added", "table_row_removed")
        missing_row = "对应CU表格未提取到该行（需原图确认）"
        lines.append("| " + " | ".join([
            str(number), cell(f"{diff.get('region', '')} / {diff.get('key', '')}"),
            missing_row if row_change and old is None else evidence(old),
            missing_row if row_change and new is None else evidence(new),
            cell(CHANGE_LABELS.get(diff.get("change"), diff.get("change"))),
            cell(f"{(old or {}).get('source')} / {(new or {}).get('source')}"),
            cell(f"{(old or {}).get('confidence')} / {(new or {}).get('confidence')}"),
            cell(match) + ("；需人工复核" if diff.get("review_required") else "；候选匹配"),
        ]) + " |")
    lines.extend(["", "表格行增删候选要求两侧CU表格网格完整且对应关系可靠；"
                  "这不保证OCR无遗漏。JSON中的 `table_context` 保留对侧表格定位，"
                  "不伪造不存在的行坐标。", "", "### BOM单元格细化", "",
                  "仅列出配对后的变化列；缺失或冲突时保留整行原文，但不把整行当作变化位置。",
                  "", "| 项目 | 列 | 旧值 | 新值 | 旧 / 新单元格来源 |",
                  "|---|---|---|---|---|"])
    for diff in result.get("differences", []):
        cells = diff.get("cell_comparison")
        if not cells:
            continue
        if cells["status"] != "complete":
            lines.append(f"| {cell(diff['key'])} | 无法细化 | — | — | {cell(cells['issues'])} |")
        for field in cells["fields"]:
            if field["change"] not in ("modified", "relocated"):
                continue
            lines.append("| " + " | ".join([
                cell(diff["key"]), cell(field["label"]), evidence(field["old"]), evidence(field["new"]),
                cell(f"{field['old']['source']} / {field['new']['source']}"),
            ]) + " |")
    lines.extend([
        "", "## 覆盖与遗漏检查", "", "```json",
        json.dumps(result.get("coverage", {}), ensure_ascii=False, indent=2), "```", "",
        "原文相同但缺乏可靠证据的字段仍需复核；完整列表在JSON的 "
        "`unchanged` / `ocr_unchanged` 中。`interpretation_only` "
        "只表示模型生成解释不一致，不可计为图纸原文改变。",
        "",
        "## 不确定项及服务警告", "",
    ])
    for warning in result.get("warnings", []):
        lines.append("- " + cell(warning))
    lines.extend([
        "", "## 全量 OCR 辅助差异", "",
        "此通道独立于结构化字段，保留漏配、重复与分裂/合并候选。"
        "可能与上表重复，不可相加为独立工程变更数量。", "",
        "| # | 旧图 OCR | 新图 OCR | 状态 / 匹配 |",
        "|---|---|---|---|",
    ])
    for number, diff in enumerate(result.get("ocr_differences", []), 1):
        lines.append("| " + " | ".join([
            str(number), evidence(diff.get("old")), evidence(diff.get("new")),
            cell({"change": diff.get("change"), "match": diff.get("match")}),
        ]) + " |")
    lines.extend([
        "", "## 可追溯性", "",
        "完整字段、OCR、表格、图形描述、source、usage 与服务 warning 保留在本地 "
        "`old.response.json` / `new.response.json`。模型/版本、输入 SHA256、"
        "操作地址和耗时在 metadata 文件；HTTP 状态在 api-events.json。",
        "",
        "不推断认证有效性，不根据轮廓推断材质，不将图形描述差异当成确定形状变化。"
        "尺寸/位置需基于显式标注及原图复核，不做弹性对齐消除真实几何变化。",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
