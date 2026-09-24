"""Map CU evidence to normalized, rotation-normalized PDF preview coordinates."""

import math


def response_geometry_matches(response: dict, pages: list[dict]) -> bool:
    """Accept original cached CU evidence only when it describes displayed page geometry."""
    expected = {page["number"]: page for page in pages}
    seen = set()
    for content in response.get("result", {}).get("contents", []):
        for page in content.get("pages", []):
            number = page.get("pageNumber")
            width, height = page.get("width"), page.get("height")
            if (number not in expected or page.get("unit", content.get("unit")) != "inch"
                    or not isinstance(width, (int, float)) or not isinstance(height, (int, float))
                    or not math.isfinite(width) or not math.isfinite(height)
                    or abs(width * 72 - expected[number]["width_pt"]) > 1
                    or abs(height * 72 - expected[number]["height_pt"]) > 1):
                return False
            seen.add(number)
    return bool(expected) and seen == set(expected)


def locations(entry: dict | None, pages: list[dict]) -> tuple[list[dict], str | None]:
    if entry is None:
        return [], "无对应证据；不能在另一侧推测位置"
    if not entry.get("polygons"):
        return [], "CU未提供可解析的来源坐标，无法定位"
    page_index = {page["number"]: page for page in pages}
    contexts = {}
    for context in entry.get("page_context", []):
        number = context.get("page_number")
        if number in contexts and contexts[number] != context:
            return [], "CU同页尺寸信息冲突，无法可靠定位"
        contexts[number] = context
    mapped, failures = [], []
    for polygon in entry["polygons"]:
        number = polygon.get("page_number")
        page, context = page_index.get(number), contexts.get(number, {})
        width, height = context.get("width"), context.get("height")
        if (not page or not isinstance(width, (int, float))
                or not isinstance(height, (int, float))
                or not math.isfinite(width) or not math.isfinite(height)
                or width <= 0 or height <= 0):
            failures.append("来源页码或页面尺寸缺失")
            continue
        unit = context.get("unit")
        if unit == "inch":
            valid_size = (abs(width * 72 - page["width_pt"]) <= 1
                          and abs(height * 72 - page["height_pt"]) <= 1)
        elif unit == "pixel":
            valid_size = abs(width / height - page["width_pt"] / page["height_pt"]) < 0.005
        else:
            valid_size = False
        if not valid_size:
            failures.append("CU尺寸/单位与预览页不符；未进行猜测缩放或旋转")
            continue
        points = polygon.get("points", [])
        if len(points) < 4 or any(
            len(point) != 2 or any(not isinstance(v, (int, float)) or not math.isfinite(v)
                                   for v in point) for point in points
        ):
            failures.append("坐标格式无效")
            continue
        normalized = [[x / width, y / height] for x, y in points]
        if any(x < -0.001 or x > 1.001 or y < -0.001 or y > 1.001 for x, y in normalized):
            failures.append("来源坐标超出页面范围")
            continue
        # Only clamp subpixel rounding at the page edge, never infer missing evidence.
        normalized = [[max(0, min(1, x)), max(0, min(1, y))] for x, y in normalized]
        x0 = min(p[0] for p in normalized)
        x1 = max(p[0] for p in normalized)
        y0 = min(p[1] for p in normalized)
        y1 = max(p[1] for p in normalized)
        if x1 <= x0 or y1 <= y0:
            failures.append("来源区域面积为零")
            continue
        mapped.append({"page": number, "x": x0, "y": y0, "width": x1 - x0,
                       "height": y1 - y0, "polygon": normalized})
    error = "；".join(dict.fromkeys(failures)) if failures else None
    if not mapped and not error:
        error = "无法定位"
    return mapped, error


def web_result(comparison: dict, documents: dict, metadata: dict) -> dict:
    profile = comparison.get("extraction_profile", comparison.get("coverage", {}).get(
        "extraction_profile", "engineering"))
    primary_text_channel = "ocr" if profile == "layout" else "schema"
    def side(entry, role):
        if entry is None:
            return None
        mapped, error = locations(entry, documents[role]["pages"])
        return {key: entry.get(key) for key in ("id", "raw_text", "detail", "confidence", "source")} | {
            "locations": mapped, "location_error": error,
        } | ({"schema_sources": entry["schema_sources"]} if "schema_sources" in entry else {})

    items = []
    channels = (
        ("schema", comparison["differences"]),
        ("ocr", comparison["ocr_differences"]),
        ("unchanged", [entry for entry in comparison["unchanged"] if entry["review_required"]]),
    )
    for channel, records in channels:
        for record in records:
            item = {key: record.get(key) for key in
                          ("region", "key", "change", "review_required", "review_reasons", "match")} | {
                "id": f"D{len(items) + 1:03d}", "channel": channel,
                "old": side(record.get("old"), "old"),
                "new": side(record.get("new"), "new"),
            }
            cells = record.get("cell_comparison")
            if record.get("annotation_comparison"):
                item["annotation_comparison"] = record["annotation_comparison"]
                item["annotation_context"] = {
                    role: side(record["annotation_context"][role], role) for role in ("old", "new")
                }
            if record.get("table_comparison"):
                item["table_comparison"] = record["table_comparison"]
                item["table_context"] = {
                    role: side(record.get("table_context", {}).get(role), role)
                    for role in ("old", "new")
                }
            if cells:
                fields = [{key: field[key] for key in ("key", "label", "change")} | {
                    role: side(field[role], role) for role in ("old", "new")
                } for field in cells["fields"]]
                item["cell_comparison"] = {
                    "status": cells["status"], "issues": cells["issues"], "fields": fields,
                }
                if record["change"] in ("modified", "relocated"):
                    for role in ("old", "new"):
                        entry = item[role]
                        entry["context_locations"] = entry["locations"]
                        changed = [field for field in fields if field["change"] in ("modified", "relocated")]
                        entry["locations"] = [
                            dict(location, field=field["key"], label=field["label"])
                            for field in changed for location in field[role]["locations"]
                        ]
                        errors = [field[role]["location_error"] for field in changed
                                  if field[role]["location_error"]]
                        if cells["status"] != "complete":
                            errors.extend(cells["issues"])
                        if not entry["locations"]:
                            errors.append("无法可靠定位变化单元格；仅保留整行原文，不将整行画成差异框")
                        entry["location_error"] = "；".join(dict.fromkeys(errors)) or None
            if record.get("semantic_pairing"):
                item["semantic_pairing"] = record["semantic_pairing"]
                if not item["key"]:
                    item["key"] = (item["old"] or item["new"] or {}).get("raw_text", "")
            words = record.get("text_comparison")
            if words is not None:
                mapped_words = {role: [side(word, role) for word in words.get(role, [])]
                                for role in ("old", "new")}
                errors = list(words.get("issues", []))
                errors.extend(word["location_error"] or "变化词缺少可定位来源"
                              for entries in mapped_words.values() for word in entries
                              if word["location_error"] or not word["locations"])
                complete = words.get("status") == "complete" and not errors
                item["text_comparison"] = {
                    "status": "complete" if complete else "unavailable",
                    "issues": errors, "changed_text": words.get("changed_text", {}),
                    **mapped_words,
                }
                for role in ("old", "new"):
                    entry = item[role]
                    if entry:
                        entry["context_locations"] = entry["locations"]
                        entry["locations"] = ([loc for word in mapped_words[role] for loc in word["locations"]]
                                              if complete else [])
                        entry["location_error"] = (None if complete else
                            "；".join(dict.fromkeys(errors)) or "无法可靠定位变化词；仅导航完整原文，不画整行变化框")
            items.append(item)
    return {
        "extraction_profile": profile, "primary_text_channel": primary_text_channel,
        "items": items, "coverage": comparison["coverage"], "warnings": comparison["warnings"],
        "metadata": {role: {key: data.get(key) for key in
                           ("cache_hit", "usage", "elapsed_this_run_seconds",
                            "selected_completion_model", "document_sha256", "coordinate_basis",
                            "extraction_profile")}
                     for role, data in metadata.items()} | {
                         "extraction_profile": profile, "primary_text_channel": primary_text_channel},
        "documents": documents,
        "limitations": [
            "差异是提取证据候选，不是已签核工程变更；未配对不确认新增或删除。",
            "只绘制CU返回且与页面尺寸一致的证据框；没有来源时不推测另一侧位置。",
            "纯图形、细小认证图标和全部几何变化未保证覆盖，请结合原图人工复核。",
            "解释文字不同不计为原文变更；提取置信度与匹配分数都不是准确率。",
        ] + list(comparison.get("coverage", {}).get("notices", [])),
    }
