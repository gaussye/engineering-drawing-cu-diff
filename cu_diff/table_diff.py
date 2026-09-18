"""Refine paired BOM rows using original CU table cells, never invented boxes."""

from collections import defaultdict
from copy import deepcopy
import re

from .evidence import normalize_text, parse_source


_HEADERS = {
    "no": "row", "number": "row", "itemno": "row", "序号": "row",
    "bomitem": "description", "description": "description", "品名": "description",
    "qty": "quantity", "quantity": "quantity", "quantty": "quantity", "数量": "quantity",
    "unit": "unit", "units": "unit", "单位": "unit",
    "pn": "part_number", "partno": "part_number", "partnumber": "part_number", "料号": "part_number",
    "flameclass": "flame_class", "fireproofingrank": "flame_class", "阻燃等级": "flame_class",
    "material": "material", "材质": "material", "材料": "material",
    "manufacturer": "manufacturer", "supplier": "manufacturer", "厂商": "manufacturer",
}
_LABELS = {
    "row": "行号", "description": "品名/规格", "quantity": "数量/单位",
    "part_number": "料号 P/N", "flame_class": "阻燃等级",
    "material": "材料", "manufacturer": "厂商",
}
_QUANTITY = re.compile(r"(\d+(?:\.\d+)?)\s*(pcs|g|kg|ea|sets|m|mm|cm)\b")
_QUANTITY_IN_ROW = re.compile(r"(?<!\w)(\d+(?:\.\d+)?)\s*(pcs|g|kg|ea|sets|m|mm|cm)\b")


def _row_text(value):
    return _QUANTITY_IN_ROW.sub(r"\1 \2", normalize_text(value))


def _box(polygons):
    if not polygons or len({p["page_number"] for p in polygons}) != 1:
        return None
    points = [point for polygon in polygons for point in polygon["points"]]
    return (polygons[0]["page_number"], min(x for x, _ in points), min(y for _, y in points),
            max(x for x, _ in points), max(y for _, y in points))


def _contained(polygon, outer):
    inner = _box([polygon])
    if inner is None or outer is None or inner[0] != outer[0]:
        return False
    _, x0, y0, x1, y1 = inner
    area = (x1 - x0) * (y1 - y0)
    overlap = max(0, min(x1, outer[3]) - max(x0, outer[1])) * max(
        0, min(y1, outer[4]) - max(y0, outer[2]))
    return area > 0 and overlap / area >= 0.9


def _rows(operation):
    result = operation.get("result", {}) if isinstance(operation, dict) else {}
    contents = result.get("contents", []) if isinstance(result, dict) else []
    rows = []
    for ci, content in enumerate(contents if isinstance(contents, list) else []):
        if not isinstance(content, dict):
            continue
        tables = content.get("tables", [])
        for ti, table in enumerate(tables if isinstance(tables, list) else []):
            if not isinstance(table, dict) or not isinstance(table.get("cells"), list):
                continue
            cells = table["cells"]
            if any(not isinstance(cell, dict) for cell in cells):
                continue
            headers = [cell for cell in cells if cell.get("kind") == "columnHeader"]
            if (not headers or len(headers) != table.get("columnCount")
                    or len({cell.get("rowIndex") for cell in headers}) != 1
                    or any(cell.get("columnSpan", 1) != 1 or cell.get("rowSpan", 1) != 1
                           for cell in headers)):
                continue
            columns = {}
            for header in headers:
                value, index = header.get("content"), header.get("columnIndex")
                if not isinstance(value, str) or not isinstance(index, int) or isinstance(index, bool):
                    break
                key = re.sub(r"[\W_]", "", normalize_text(value).casefold())
                if not key or index in columns:
                    break
                columns[index] = (_HEADERS.get(key, f"header:{key}"), header)
            if (set(columns) != set(range(len(headers)))
                    or len({role for role, _ in columns.values()}) != len(columns)
                    or not {"description", "quantity"}.issubset(role for role, _ in columns.values())):
                continue
            grouped = defaultdict(list)
            for cell in cells:
                row = cell.get("rowIndex")
                if cell.get("kind") != "columnHeader" and isinstance(row, int) and not isinstance(row, bool):
                    grouped[row].append(cell)
            for ri, row in grouped.items():
                if (len(row) != len(columns)
                        or any(cell.get("columnSpan", 1) != 1 or cell.get("rowSpan", 1) != 1
                               or not isinstance(cell.get("content"), str) for cell in row)
                        or {cell.get("columnIndex") for cell in row} != set(columns)):
                    continue
                ordered = sorted(row, key=lambda cell: cell["columnIndex"])
                sources = [parse_source(cell.get("source")) for cell in ordered]
                if not all(sources):
                    continue
                bounds = _box([polygon for polygons in sources for polygon in polygons])
                if bounds is not None:
                    rows.append({"content_index": ci, "table_index": ti, "row_index": ri,
                                 "columns": columns, "cells": ordered, "bounds": bounds})
    return rows


def _find_row(entry, rows):
    candidates = [row for row in rows if row["content_index"] == entry["content_index"]
                  and entry["polygons"]
                  and all(_contained(p, row["bounds"]) for p in entry["polygons"])]
    return candidates[0] if len(candidates) == 1 else None


def _groups(row):
    groups = defaultdict(list)
    for cell in row["cells"]:
        role, header = row["columns"][cell["columnIndex"]]
        groups["quantity" if role == "unit" else role].append(
            {"role": role, "cell": cell, "header": header})
    if "quantity" in groups:
        groups["quantity"].sort(key=lambda item: item["role"] == "unit")
    return groups


def _text(group):
    return normalize_text(" ".join(item["cell"]["content"] for item in group))


def _value(group, key):
    value = _text(group)
    match = _QUANTITY.fullmatch(value) if key == "quantity" else None
    return " ".join(match.groups()) if match else value


def _changed_quantity_sources(group, left, right):
    old, new = _QUANTITY.fullmatch(left), _QUANTITY.fullmatch(right)
    if old is None or new is None:
        return group
    number_changed, unit_changed = old[1] != new[1], old[2] != new[2]
    selected = []
    for item in group:
        if item["role"] == "unit":
            include = unit_changed
        elif _QUANTITY.fullmatch(normalize_text(item["cell"]["content"])):
            include = number_changed or unit_changed
        else:
            include = number_changed
        if include:
            selected.append(item)
    return selected


def _evidence(group, selected, entry, row):
    source = [item["cell"]["source"] for item in selected]
    return {
        "raw_text": _text(group), "source": source, "polygons": parse_source(source),
        "page_context": deepcopy(entry["page_context"]), "confidence": None,
        "table_index": row["table_index"], "row_index": row["row_index"],
        "raw_cells": [deepcopy(item["cell"]) for item in group],
        "headers": [deepcopy(item["header"]) for item in group],
    }


def refine_bom(records, old_operation, new_operation):
    rows = {"old": _rows(old_operation), "new": _rows(new_operation)}
    for record in records:
        if (record["category"] != "BOM" or record["change"] not in ("modified", "relocated")
                or record["old"] is None or record["new"] is None):
            continue
        refinement = {"status": "unavailable", "fields": [], "issues": []}
        record["cell_comparison"] = refinement
        found = {role: _find_row(record[role], rows[role]) for role in rows}
        if not all(found.values()):
            refinement["issues"].append("无法唯一关联CU表格行：表格/坐标缺失、跨行或来源不明确。")
        elif any(_row_text(" ".join(cell["content"] for cell in found[role]["cells"]))
                 != _row_text(record[role]["raw_text"]) for role in rows):
            refinement["issues"].append("CU单元格文字与结构化整行文字不一致；不能用其中一方静默覆盖另一方。")
        else:
            groups = {role: _groups(found[role]) for role in rows}
            if set(groups["old"]) != set(groups["new"]):
                refinement["issues"].append("两侧列标题不能完整对应；未按列号猜测列含义。")
            else:
                refinement["status"] = "complete"
                for key, old_group in groups["old"].items():
                    new_group = groups["new"][key]
                    left, right = _value(old_group, key), _value(new_group, key)
                    change = ("relocated" if key == "row" else "modified") if left != right else "unchanged"
                    selected = {"old": old_group, "new": new_group}
                    if key == "quantity" and change == "modified":
                        selected = {role: _changed_quantity_sources(groups[role][key], left, right)
                                    for role in rows}
                    refinement["fields"].append({
                        "key": key, "label": _LABELS.get(key, old_group[0]["header"]["content"]),
                        "change": change, "normalized_old": left, "normalized_new": right,
                        **{role: _evidence(groups[role][key], selected[role], record[role], found[role])
                           for role in rows},
                    })
                changes = {field["change"] for field in refinement["fields"]} - {"unchanged"}
                if not changes:
                    record["change"] = "formatting_only"
                    record["review_reasons"].append("表格列值一致，仅排版、分列或数量/单位空格不同。")
                elif changes == {"relocated"}:
                    record["change"] = "relocated"
                else:
                    record["change"] = "modified"
        if refinement["status"] != "complete":
            record["review_required"] = True
            record["review_reasons"].extend(refinement["issues"])
        elif any(field["change"] != "unchanged" for field in refinement["fields"]):
            record["review_required"] = True
            record["review_reasons"].append("单元格未提供独立提取置信度；列差异仍需按原图复核。")
