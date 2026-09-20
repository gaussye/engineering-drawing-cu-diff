"""Reconcile printed title labels when CU splits or combines label/value fields."""

from collections import defaultdict
from copy import deepcopy
import math
import re

from .evidence import normalize_text


_LABEL = re.compile(
    r"^(?P<owner>[\w][\w .&-]{0,60}?)\s+(?:p\s*/\s*n|part\s+(?:number|no\.?))"
    r"\s*[:：]\s*(?P<value>.*)$", re.IGNORECASE)
_TITLE = re.compile(r"title(?:\s*block)?|标题(?:栏)?|圖框|图框", re.IGNORECASE)


def _bounds(entry):
    polygons, contexts = entry.get("polygons", []), entry.get("page_context", [])
    if not polygons or not contexts or any(context != contexts[0] for context in contexts):
        return None
    context = contexts[0]
    if any(p["page_number"] != context["page_number"] for p in polygons):
        return None
    points = [point for polygon in polygons for point in polygon["points"]]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    width, height = context.get("width"), context.get("height")
    if (any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v)
            for v in (width, height)) or context.get("unit") not in ("inch", "pixel")
            or not 0 <= min(xs) < max(xs) <= width or not 0 <= min(ys) < max(ys) <= height):
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _contained(inner, outer):
    overlap = max(0, min(inner[2], outer[2])-max(inner[0], outer[0])) * max(
        0, min(inner[3], outer[3])-max(inner[1], outer[1]))
    return overlap >= .9 * (inner[2]-inner[0]) * (inner[3]-inner[1])


def _same_page(left, right):
    return (left["content_index"] == right["content_index"]
            and bool(left["page_context"]) and bool(right["page_context"])
            and left["page_context"][0] == right["page_context"][0])


def _bundles(entries, lines):
    scoped = [(i, e, _bounds(e)) for i, e in enumerate(entries)
              if _TITLE.fullmatch(normalize_text(e["region"]))
              and normalize_text(e["category"]).casefold() in ("title", "label")]
    bundles = defaultdict(list)
    for index, label, box in scoped:
        match = _LABEL.fullmatch(normalize_text(label["raw_text"]))
        if match is None or box is None:
            continue
        value_text = match["value"]
        if value_text:
            candidates = [line for line in lines if _same_page(label, line)
                          and normalize_text(line["raw_text"]) == value_text
                          and _bounds(line) is not None and _contained(_bounds(line), box)]
            if len(candidates) != 1:
                continue
            value = deepcopy(label)
            for key in ("raw_text", "source", "polygons", "page_context", "confidence"):
                value[key] = deepcopy(candidates[0][key])
            value["review_reasons"] = label["review_reasons"] + candidates[0]["review_reasons"]
            consumed = {index}
        else:
            h = box[3]-box[1]
            candidates = []
            for vi, candidate, value_box in scoped:
                if (vi == index or value_box is None or not _same_page(label, candidate)
                        or _LABEL.fullmatch(normalize_text(candidate["raw_text"]))
                        or not normalize_text(candidate["raw_text"])
                        or normalize_text(candidate["raw_text"]).endswith((":", "："))):
                    continue
                x0, y0, x1, y1 = value_box
                below = box[1]+.4*h <= y0 <= box[3]+2*h
                beside = x0 >= box[2]-.1*h and min(y1, box[3])-max(y0, box[1]) >= .5*h
                if ((below or beside) and x0 >= box[0]-h and x0 <= box[2]+12*h
                        and y1-y0 <= 3*h):
                    candidates.append((vi, candidate))
            if len(candidates) != 1:
                continue
            vi, candidate = candidates[0]
            value, consumed = deepcopy(candidate), {index, vi}
        label_text = normalize_text(label["raw_text"]).split(":")[0].split("：")[0].strip()
        value.update({
            "category": "title", "key": label_text, "region": "title",
            "schema_item_ids": [entries[i]["id"] for i in sorted(consumed)],
            "schema_sources": [
                {key: deepcopy(entries[i][key]) for key in ("id", "raw_text", "source", "confidence")}
                for i in sorted(consumed)],
            "printed_label": {"raw_text": label_text, "source": deepcopy(label["source"])},
        })
        identity = (label["content_index"], label["page_context"][0]["page_number"],
                    normalize_text(match["owner"]).casefold())
        bundles[identity].append((value, consumed))
    return bundles


def reconcile_title_fields(old, new, old_lines, new_lines):
    """Return unique source-grounded pairs and untouched remaining schema entries."""
    a, b = _bundles(old, old_lines), _bundles(new, new_lines)
    used_old, used_new, pairs, warnings = set(), set(), [], []
    proposals = [(a[key][0], b[key][0]) for key in sorted(a.keys() & b.keys())
                 if len(a[key]) == len(b[key]) == 1]
    for (left, oi), (right, ni) in proposals:
        if (any(oi & other[0][1] for other in proposals if other[0][0] is not left)
                or any(ni & other[1][1] for other in proposals if other[1][0] is not right)):
            warnings.append("Title label/value candidates overlap; ambiguous correspondence was not forced.")
            continue
        right["key"] = left["key"]
        pairs.append((left, right))
        used_old.update(oi)
        used_new.update(ni)
    return (pairs, [e for i, e in enumerate(old) if i not in used_old],
            [e for i, e in enumerate(new) if i not in used_new], warnings)
