"""Chinese evidence tables without external services or active HTML."""

import json
from pathlib import Path


CHANGE_LABELS = {
    "modified": "提取原文不同",
    "relocated": "行号/位置重排",
    "interpretation_only": "仅生成解释不同（不是原文变更）",
    "unpaired_old": "仅旧侧未配对（不确认删除）",
    "unpaired_new": "仅新侧未配对（不确认新增）",
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
        lines.append("| " + " | ".join([
            str(number), cell(f"{diff.get('region', '')} / {diff.get('key', '')}"),
            evidence(old), evidence(new),
            cell(CHANGE_LABELS.get(diff.get("change"), diff.get("change"))),
            cell(f"{(old or {}).get('source')} / {(new or {}).get('source')}"),
            cell(f"{(old or {}).get('confidence')} / {(new or {}).get('confidence')}"),
            cell(match) + ("；需人工复核" if diff.get("review_required") else "；候选匹配"),
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
