"""Refine paired BOM rows using original CU table cells, never invented boxes."""

from collections import defaultdict
from copy import deepcopy
from difflib import SequenceMatcher
import re

from .evidence import normalize_text, parse_source


_HEADERS = {
    "no": "row", "number": "row", "itemno": "row", "item": "row", "序号": "row",
    "bomitem": "description", "description": "description", "品名": "description",
    "qty": "quantity", "quantity": "quantity", "quantty": "quantity", "数量": "quantity",
    "unit": "unit", "units": "unit", "um": "unit", "单位": "unit",
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
_QUANTITY = re.compile(r"(\d+(?:\.\d+)?)\s*(pcs|g|kg|ea|sets|m|mm|cm)\b", re.I)
_QUANTITY_IN_ROW = re.compile(r"(?<!\w)(\d+(?:\.\d+)?)\s*(pcs|g|kg|ea|sets|m|mm|cm)\b", re.I)


def _row_text(value):
    value = _QUANTITY_IN_ROW.sub(r"\1 \2", normalize_text(value))
    return re.sub(r"\b(As list)\s*(EA)\b", r"\1 \2", value, flags=re.I)


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


def _columns(table):
    cells = table["cells"]
    headers = [cell for cell in cells if cell.get("kind") == "columnHeader"]
    inferred = not headers
    if inferred:
        # Some CU tables label even the bottom header as ordinary content.
        nr = table.get("rowCount")
        if not isinstance(nr, int) or isinstance(nr, bool) or nr < 2:
            return None
        candidates = []
        for index in (0, nr - 1):
            row = [cell for cell in cells if cell.get("rowIndex") == index]
            roles = [_HEADERS.get(re.sub(r"[\W_]", "", normalize_text(
                cell.get("content", "")).casefold())) for cell in row
                if isinstance(cell.get("content"), str)]
            if (len(row) == table.get("columnCount") and len(roles) == len(row)
                    and all(roles) and {"row", "description", "quantity"}.issubset(roles)):
                candidates.append(row)
        if len(candidates) != 1:
            return None
        headers = candidates[0]
    if (not headers or len(headers) != table.get("columnCount")
            or any(not isinstance(cell.get("rowIndex"), int) or isinstance(cell.get("rowIndex"), bool)
                   for cell in headers)
            or len({cell.get("rowIndex") for cell in headers}) != 1
            or any(cell.get("columnSpan", 1) != 1 or cell.get("rowSpan", 1) != 1
                   for cell in headers)):
        return None
    columns = {}
    for header in headers:
        value, index = header.get("content"), header.get("columnIndex")
        if not isinstance(value, str) or not isinstance(index, int) or isinstance(index, bool):
            return None
        key = re.sub(r"[\W_]", "", normalize_text(value).casefold())
        if not key or index in columns:
            return None
        columns[index] = (_HEADERS.get(key, f"header:{key}"), header)
    if (set(columns) != set(range(len(headers)))
            or len({role for role, _ in columns.values()}) != len(columns)
            or not {"description", "quantity"}.issubset(role for role, _ in columns.values())):
        return None
    return columns


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
            columns = _columns(table)
            if columns is None:
                continue
            header_row = next(iter(columns.values()))[1].get("rowIndex")
            grouped = defaultdict(list)
            for cell in cells:
                row = cell.get("rowIndex")
                if row != header_row and isinstance(row, int) and not isinstance(row, bool):
                    grouped[row].append(cell)
            for ri, row in grouped.items():
                if (len(row) != len(columns)
                        or any(cell.get("columnSpan", 1) != 1 or cell.get("rowSpan", 1) != 1
                               or not isinstance(cell.get("content"), str)
                               or not isinstance(cell.get("columnIndex"), int)
                               or isinstance(cell.get("columnIndex"), bool) for cell in row)
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


def _row_identity(row):
    groups = _groups(row)
    return tuple((key, _value(groups[key], key))
                 for key in ("description", "part_number") if key in groups)


def _complete_tables(operation, side, warnings):
    if not isinstance(operation, dict) or operation.get("status") != "Succeeded":
        warnings.append(f"{side}: BOM table reconciliation requires a Succeeded extraction.")
        return []
    grouped = defaultdict(list)
    for row in _rows(operation):
        grouped[(row["content_index"], row["table_index"])].append(row)
    tables = []
    result = operation.get("result")
    contents = result.get("contents", []) if isinstance(result, dict) else []
    for ci, content in enumerate(contents if isinstance(contents, list) else []):
        raw_tables = content.get("tables", []) if isinstance(content, dict) else []
        for ti, table in enumerate(raw_tables if isinstance(raw_tables, list) else []):
            label = f"{side}: content {ci} table {ti}"
            if not isinstance(table, dict):
                continue
            cells = table.get("cells")
            nr, nc = table.get("rowCount"), table.get("columnCount")
            if (not isinstance(cells, list) or not all(isinstance(c, dict) for c in cells)
                    or any(not isinstance(n, int) or isinstance(n, bool) or n < 1
                           for n in (nr, nc))):
                warnings.append(f"{label}: invalid table grid; no row presence claims.")
                continue
            columns = _columns(table)
            if columns is None:
                continue
            coordinates = [(c.get("rowIndex"), c.get("columnIndex")) for c in cells]
            sources = [parse_source(c.get("source")) for c in cells]
            rows = grouped[(ci, ti)]
            if (nr < 2 or len(cells) != nr * nc
                    or any(not isinstance(n, int) or isinstance(n, bool)
                           for coordinate in coordinates for n in coordinate)
                    or set(coordinates) != {(r, c) for r in range(nr) for c in range(nc)}
                    or len(rows) != nr - 1 or not all(sources)
                    or any(c.get("columnSpan", 1) != 1 or c.get("rowSpan", 1) != 1
                           or not isinstance(c.get("content"), str) for c in cells)
                    or _box([p for polygons in sources for p in polygons]) is None):
                warnings.append(f"{label}: incomplete grid or cell sources; no row presence claims.")
                continue
            identities = [_row_identity(row) for row in rows]
            numbers = [_value(_groups(row)["row"], "row") for row in rows
                       if "row" in _groups(row)]
            if (len(set(identities)) != len(identities)
                    or any(not dict(identity).get("description") for identity in identities)
                    or (numbers and (len(set(numbers)) != len(numbers) or not all(numbers)))):
                warnings.append(f"{label}: duplicate or empty row identities; reconciliation withheld.")
                continue
            tables.append({"content_index": ci, "table_index": ti, "rows": rows,
                           "columns": columns, "cells": cells, "content": content,
                           "roles": set(_groups(rows[0])), "identities": set(identities),
                           "row_count": nr, "column_count": nc})
    return tables


def _table_entry(table, row, side):
    cells = table["cells"] if row is None else row["cells"]
    sources = [deepcopy(cell["source"]) for cell in cells]
    polygons = parse_source(sources)
    pages = defaultdict(list)
    raw_pages = table["content"].get("pages", [])
    for page in raw_pages if isinstance(raw_pages, list) else []:
        if isinstance(page, dict):
            pages[page.get("pageNumber")].append(page)
    contexts = []
    for polygon in polygons:
        number = polygon["page_number"]
        page = pages[number][0] if len(pages[number]) == 1 else {}
        contexts.append({"page_number": number, "width": page.get("width"),
                         "height": page.get("height"),
                         "unit": page.get("unit", table["content"].get("unit"))})
    ri = row["row_index"] if row is not None else "context"
    return {
        "id": f"{side}:table:{table['content_index']}:{table['table_index']}:{ri}",
        "content_index": table["content_index"], "category": "BOM", "region": "BOM",
        "key": f"CU table {table['table_index']} row {ri}",
        "raw_text": " ".join(cell["content"] for cell in cells),
        "source": sources, "polygons": polygons, "page_context": contexts,
        "detail": "", "confidence": None, "field_evidence": {},
        "review_reasons": ["CU table cells have no independent row extraction confidence."],
        "raw": {"cells": deepcopy(cells)}, "table_supplement": True,
        "schema_item_ids": [],
    }


def _changed_row_candidate(old, new):
    left, right = _groups(old), _groups(new)
    if ("row" not in left or "row" not in right
            or _value(left["row"], "row") != _value(right["row"], "row")):
        return False
    a, b = _value(left["description"], "description"), _value(right["description"], "description")
    # Numeric specification edits retain the literal cell values in the report.
    numeric_shape = lambda value: re.sub(r"\d+(?:\.\d+)?", "#", value)
    if a != b and numeric_shape(a) == numeric_shape(b) and len(re.findall(r"[A-Za-z]", a)) >= 6:
        return True
    common = set(a.split()) & set(b.split())
    return (SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.88
            and (a == b or len(common) >= 2))


def reconcile_bom(records, old_operation, new_operation):
    """Mutate comparator records using complete, uniquely corresponding CU tables.

    Return ``list[str]`` diagnostics. Row presence is an extraction-based candidate,
    never proof of absence in a drawing. ``table_comparison`` records grid/source
    coverage and consumed schema IDs; ``table_context`` is context, not a diff box.
    Call ``refine_bom`` afterwards for actual per-cell changes on paired rows.
    """
    warnings = []
    tables = {side: _complete_tables(op, side, warnings)
              for side, op in (("old", old_operation), ("new", new_operation))}
    if not any(tables.values()) and not warnings:
        return []
    candidates = [(i, j) for i, old in enumerate(tables["old"])
                  for j, new in enumerate(tables["new"])
                  if old["content_index"] == new["content_index"] and old["roles"] == new["roles"]
                  and old["identities"] & new["identities"]]
    paired = [(i, j) for i, j in candidates
              if sum(a == i for a, _ in candidates) == 1
              and sum(b == j for _, b in candidates) == 1]
    if len(paired) < len(candidates):
        warnings.append("BOM tables have ambiguous correspondence; ambiguous tables were not reconciled.")
    if not paired:
        warnings.append("No complete, uniquely anchored BOM table pair; no table row presence claims.")
        return warnings
    consumed, replacements = set(), []
    for i, j in paired:
        pair = {"old": tables["old"][i], "new": tables["new"][j]}
        all_rows = {side: [row for table in tables[side] for row in table["rows"]] for side in pair}
        associated = {side: defaultdict(list) for side in pair}
        blocked = False
        relevant = set()
        for index, record in enumerate(records):
            if str(record.get("category", "")).casefold() != "bom":
                continue
            membership = {}
            for side in pair:
                entry = record.get(side)
                row = _find_row(entry, all_rows[side]) if entry else None
                if row is None or row not in pair[side]["rows"]:
                    membership[side] = False
                    continue
                membership[side] = True
                if _row_text(entry["raw_text"]) != _row_text(" ".join(c["content"] for c in row["cells"])):
                    blocked = True
                associated[side][row["row_index"]].append((index, entry))
                relevant.add(index)
            if any(membership.values()) and any(record.get(side) is not None
                                                and not membership[side] for side in pair):
                blocked = True
        if blocked:
            warnings.append("BOM table/schema source or text conflict; existing evidence retained without reconciliation.")
            continue
        old_rows, new_rows = pair["old"]["rows"], pair["new"]["rows"]
        new_by_identity = {_row_identity(row): row for row in new_rows}
        matches = [(row, new_by_identity[_row_identity(row)]) for row in old_rows
                   if _row_identity(row) in new_by_identity]
        old_free = [row for row in old_rows if not any(row is a for a, _ in matches)]
        new_free = [row for row in new_rows if not any(row is b for _, b in matches)]
        changed = [(a, b) for a in old_free for b in new_free if _changed_row_candidate(a, b)]
        changed = [(a, b) for a, b in changed
                   if sum(a is x for x, _ in changed) == 1
                   and sum(b is y for _, y in changed) == 1]
        matches.extend(changed)
        old_free = [row for row in old_free if not any(row is a for a, _ in changed)]
        new_free = [row for row in new_free if not any(row is b for _, b in changed)]
        # Unresolved substitutions are not silently relabeled as additions/deletions.
        if old_free and new_free:
            warnings.append("BOM rows have unresolved substitutions; table reconciliation withheld.")
            continue
        context = {side: _table_entry(table, None, side) for side, table in pair.items()}
        coverage = {side: {
            "content_index": table["content_index"], "table_index": table["table_index"],
            "row_count": table["row_count"], "column_count": table["column_count"],
            "expected_cells": table["row_count"] * table["column_count"],
            "observed_cells": len(table["cells"]), "sourced_cells": len(table["cells"]),
            "data_rows": len(table["rows"]),
            "header_roles": [role for role, _ in table["columns"].values()],
            "header_evidence": [deepcopy(header) for _, header in table["columns"].values()],
        } for side, table in pair.items()}
        for old, new in matches + [(row, None) for row in old_free] + [(None, row) for row in new_free]:
            entries, origins, supplements = {}, {}, {}
            for side, row in (("old", old), ("new", new)):
                origins[side] = associated[side][row["row_index"]] if row is not None else []
                entries[side] = (deepcopy(origins[side][0][1]) if origins[side]
                                 else _table_entry(pair[side], row, side) if row is not None else None)
                supplements[side] = row is not None and not origins[side]
                if entries[side] is not None:
                    entries[side]["schema_item_ids"] = list(dict.fromkeys(
                        identity for _, original in origins[side]
                        for identity in original.get("schema_item_ids", [original.get("id")])
                        if identity is not None))
            old_indexes = {index for index, _ in origins["old"]}
            new_indexes = {index for index, _ in origins["new"]}
            existing = old_indexes & new_indexes
            if existing:
                record = deepcopy(records[min(existing)])
                record.update(entries)
            else:
                entry = entries["old"] or entries["new"]
                record = {"region": entry["region"], "key": entry["key"], **entries,
                          "detail_changed": bool(old and new and entries["old"]["detail"] != entries["new"]["detail"]),
                          "change": "unchanged" if old and new and entries["old"]["raw_text"] == entries["new"]["raw_text"]
                          else "modified", "review_reasons": [], "review_required": True}
            record["category"] = "BOM"
            if old is None or new is None:
                record["change"] = "table_row_added" if old is None else "table_row_removed"
            record["match"] = {"method": "complete_cu_table", "score": 1.0, "certainty": "high"}
            record["review_required"] = True
            record["review_reasons"].append(
                "Extraction-based CU table candidate; complete extracted grids do not prove absence in the drawing.")
            record["table_comparison"] = {
                "status": "complete", "coverage": deepcopy(coverage),
                "shared_anchor_count": len(pair["old"]["identities"] & pair["new"]["identities"]),
                "supplemented": supplements,
                "schema_entry_ids": {side: [entry.get("id") for _, entry in origins[side]] for side in pair},
            }
            record["table_context"] = deepcopy(context)
            record["schema_entries"] = {side: [deepcopy(entry) for _, entry in origins[side]] for side in pair}
            replacements.append(record)
        consumed.update(relevant)
    records[:] = [record for index, record in enumerate(records) if index not in consumed] + replacements
    return warnings


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
                        old_cells = {item["role"]: item for item in old_group}
                        new_cells = {item["role"]: item for item in new_group}
                        if set(old_cells) == set(new_cells):
                            changed_roles = {role for role in old_cells
                                             if normalize_text(old_cells[role]["cell"]["content"])
                                             != normalize_text(new_cells[role]["cell"]["content"])}
                            selected = {side: [item for item in group if item["role"] in changed_roles]
                                        for side, group in (("old", old_group), ("new", new_group))}
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
