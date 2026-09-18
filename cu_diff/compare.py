"""Conservative, offline comparison of full Content Understanding operations.

``compare_documents(old, new)`` compares custom ``Items`` and full OCR lines
independently. Unpaired evidence is never called a confirmed addition/deletion.
Only NFC and whitespace normalization are used for text equality. Coordinates
are kept in their original coordinate system; no scale, warp, or rotation is
applied. All input evidence is retained in each entry's ``raw`` member.
Page geometry inherits a content-level unit when the page has no explicit unit.
OCR line confidence is not inferred from unaligned word-level confidence.
BOM descriptions preceding a ``quantity pcs`` or ``weight g`` token take
precedence over positional row keys. Pure row moves are reported as ``relocated``;
changed quantities, weights, or part identifiers remain substantive differences.
Generated Detail or identity metadata changes alone are ``interpretation_only``,
not document modifications; both original interpretations remain in the report.
"""

from collections import Counter, defaultdict
from copy import deepcopy
from difflib import SequenceMatcher
import json
import math
import re
import unicodedata


_CATEGORIES = {
    "BOM", "certification", "packaging", "dimension", "label", "note",
    "drawing", "title", "other",
}
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_SOURCE = re.compile(r"D\(\s*(\d+)\s*((?:,\s*" + _NUMBER + r"\s*)+)\)")
_BOM_ROW_NUMBER = re.compile(r"^(\d+)[.)]?\s+(.+)$")
_BOM_QUANTITY = re.compile(r"(?<!\S)\d+(?:[.,]\d+)?\s*(?:pcs|g)\b", re.IGNORECASE)


def normalize_text(value):
    """Normalize whitespace and NFC, never case, punctuation, or identifiers."""
    return " ".join(unicodedata.normalize("NFC", value).split())


def parse_source(source):
    """Parse REST D(page,x,y,width,height) and four-vertex polygon sources.

    Multiple polygons separated by whitespace, commas, or semicolons, and lists
    of supported source strings are accepted. Unsupported/partially understood
    source syntax returns no polygons; the caller still retains the raw source.
    Rectangle widths/heights become polygon extents in the original units;
    they are not interpreted as bottom-right coordinates or scaled to a page.
    """
    if isinstance(source, list):
        if not source:
            return []
        parsed = [parse_source(part) for part in source]
        return [polygon for group in parsed for polygon in group] if all(parsed) else []
    if not isinstance(source, str) or not source.strip():
        return []
    matches = list(_SOURCE.finditer(source))
    if not matches or re.sub(r"[\s;,]", "", _SOURCE.sub("", source)):
        return []
    polygons = []
    for match in matches:
        page = int(match.group(1))
        coordinates = [float(value.strip()) for value in match.group(2).split(",")[1:]]
        if page < 1 or len(coordinates) not in (4, 8) or not all(map(math.isfinite, coordinates)):
            return []
        if len(coordinates) == 4:
            x, y, width, height = coordinates
            if width <= 0 or height <= 0 or not all(map(math.isfinite, (x + width, y + height))):
                return []
            coordinates = [x, y, x + width, y, x + width, y + height, x, y + height]
        polygons.append({
            "page_number": page,
            "points": [coordinates[index:index + 2] for index in range(0, 8, 2)],
        })
    return polygons


def _valid_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _confidence_issues(value, threshold):
    if value is None:
        return ["missing confidence"]
    if not _valid_number(value) or not 0 <= value <= 1:
        return ["invalid confidence"]
    return ["low extraction confidence"] if value < threshold else []


def _evidence_issues(source, confidence, threshold):
    issues = _confidence_issues(confidence, threshold)
    if source is None or source == "" or source == []:
        issues.append("missing source")
    elif not parse_source(source):
        issues.append("unsupported source")
    return issues


def _page_context(pages, content_unit=None):
    contexts = {}
    duplicates = set()
    for page in pages:
        if not isinstance(page, dict):
            continue
        number = page.get("pageNumber")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            continue
        if number in contexts:
            duplicates.add(number)
        contexts[number] = {
            "page_number": number,
            "width": deepcopy(page.get("width")),
            "height": deepcopy(page.get("height")),
            "unit": deepcopy(page.get("unit") if page.get("unit") is not None else content_unit),
        }
    # Ambiguous page dimensions must not enable geometric pairing.
    for number in duplicates:
        del contexts[number]
    return contexts


def _entry_context(polygons, contexts):
    return [deepcopy(contexts.get(polygon["page_number"], {
        "page_number": polygon["page_number"], "width": None, "height": None, "unit": None,
    })) for polygon in polygons]


def _diagnostics(operation, side):
    """Retain service diagnostics separately from correspondence/extraction scores."""
    uncertainties, service_warnings, warnings = [], [], []

    def collect_warnings(value, path):
        if isinstance(value, dict):
            if "warnings" in value:
                child = value["warnings"]
                child_path = f"{path}.warnings"
                entries = child if isinstance(child, list) else [child]
                for index, raw in enumerate(entries):
                    service_warnings.append({
                        "side": side, "path": f"{child_path}[{index}]",
                        "raw": deepcopy(raw), "review_required": True,
                    })
                    warnings.append(f"{side}: service warning at {child_path}[{index}]: "
                                    + json.dumps(raw, ensure_ascii=False))
            for key, child in value.items():
                if key != "warnings":
                    collect_warnings(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                collect_warnings(child, f"{path}[{index}]")

    collect_warnings(operation, "$")
    result = operation.get("result") if isinstance(operation, dict) else None
    contents = result.get("contents") if isinstance(result, dict) else None
    for content_index, content in enumerate(contents if isinstance(contents, list) else []):
        fields = content.get("fields") if isinstance(content, dict) else None
        if not isinstance(fields, dict) or "Uncertainties" not in fields:
            continue
        field = fields["Uncertainties"]
        entries = field.get("valueArray") if isinstance(field, dict) else None
        malformed_field = not isinstance(entries, list)
        if malformed_field:
            entries = [field]
        for index, raw in enumerate(entries):
            value = raw if isinstance(raw, dict) else {}
            text = value.get("valueString")
            malformed = malformed_field or not isinstance(text, str)
            # Raw string entries are non-schema-shaped but still visible evidence.
            if not isinstance(text, str):
                text = raw if isinstance(raw, str) else ""
            source = deepcopy(value.get("source"))
            uncertainties.append({
                "side": side, "content_index": content_index, "index": index,
                "text": text, "source": source, "confidence": deepcopy(value.get("confidence")),
                "polygons": parse_source(source), "raw": deepcopy(raw),
                "raw_field": deepcopy(field), "review_required": True,
                "malformed": malformed,
            })
            warnings.append(f"{side}: content {content_index} schema uncertainty {index}: "
                            + (text if text else "(no readable uncertainty text)")
                            + (" [malformed Uncertainties field/entry]" if malformed else ""))
    return uncertainties, service_warnings, warnings


def _extract(operation, side, threshold):
    items, lines, warnings = [], [], []
    counts = {"contents": 0, "pages": 0, "invalid_items": 0, "invalid_lines": 0}
    if not isinstance(operation, dict):
        return items, lines, [f"{side}: operation is not an object"], counts
    if operation.get("status") != "Succeeded":
        warnings.append(f"{side}: operation status is not Succeeded; extraction may be incomplete")
    result = operation.get("result")
    contents = result.get("contents") if isinstance(result, dict) else None
    if not isinstance(contents, list):
        return items, lines, warnings + [f"{side}: missing result.contents array"], counts
    if not contents:
        warnings.append(f"{side}: result.contents is empty")
    for content_index, content in enumerate(contents):
        counts["contents"] += 1
        if not isinstance(content, dict):
            warnings.append(f"{side}: content {content_index} is not an object")
            continue
        pages = content.get("pages")
        if not isinstance(pages, list):
            warnings.append(f"{side}: content {content_index} has no full OCR pages array")
            pages = []
        contexts = _page_context(pages, content.get("unit"))
        fields = content.get("fields")
        array = fields.get("Items") if isinstance(fields, dict) else None
        raw_items = array.get("valueArray") if isinstance(array, dict) else None
        if not isinstance(raw_items, list):
            warnings.append(f"{side}: content {content_index} has no Items.valueArray")
            raw_items = []
        for item_index, raw in enumerate(raw_items):
            identity = f"{side}:item:{content_index}:{item_index}"
            values = raw.get("valueObject") if isinstance(raw, dict) else None
            issues = []
            if not isinstance(values, dict):
                issues.append("invalid item valueObject")
                counts["invalid_items"] += 1
                values = {}
            strings = {}
            field_evidence = {}
            for name in ("Region", "Category", "Key", "RawText", "Detail"):
                field = values.get(name)
                value = field.get("valueString") if isinstance(field, dict) else None
                if not isinstance(value, str):
                    issues.append(f"missing or invalid {name}.valueString")
                    value = ""
                strings[name] = value
            for name, field in values.items():
                if not isinstance(field, dict):
                    continue
                field_evidence[name] = {
                    "source": deepcopy(field.get("source")),
                    "confidence": deepcopy(field.get("confidence")),
                    "polygons": parse_source(field.get("source")),
                }
                if name != "RawText" and "confidence" in field:
                    issues.extend(f"{name}: {issue}" for issue in
                                  _confidence_issues(field["confidence"], threshold))
                if name != "RawText" and "source" in field and not parse_source(field["source"]):
                    issues.append(f"{name}: missing or unsupported source")
            text_field = values.get("RawText")
            text_field = text_field if isinstance(text_field, dict) else {}
            source, confidence = text_field.get("source"), text_field.get("confidence")
            issues.extend(_evidence_issues(source, confidence, threshold))
            if normalize_text(strings["Category"]) not in _CATEGORIES:
                issues.append("unknown category")
            if not normalize_text(strings["Region"]) or not normalize_text(strings["Key"]):
                issues.append("missing stable region or key")
            polygons = parse_source(source)
            items.append({
                "id": identity, "content_index": content_index,
                "region": strings["Region"], "category": strings["Category"],
                "key": strings["Key"], "raw_text": strings["RawText"],
                "detail": strings["Detail"], "source": deepcopy(source),
                "confidence": deepcopy(confidence), "polygons": polygons,
                "page_context": _entry_context(polygons, contexts),
                "field_evidence": field_evidence, "raw": deepcopy(raw),
                "review_reasons": issues,
            })
            warnings.extend(f"{identity}: {issue}" for issue in issues)
        for page_index, page in enumerate(pages):
            counts["pages"] += 1
            if not isinstance(page, dict):
                warnings.append(f"{side}: content {content_index} page {page_index} is invalid")
                continue
            raw_lines = page.get("lines")
            if not isinstance(raw_lines, list):
                warnings.append(f"{side}: content {content_index} page {page_index} has no OCR lines")
                continue
            for line_index, raw in enumerate(raw_lines):
                identity = f"{side}:ocr:{content_index}:{page_index}:{line_index}"
                value = raw if isinstance(raw, dict) else {}
                text = value.get("content")
                issues = []
                if not isinstance(text, str):
                    text = ""
                    issues.append("missing or invalid OCR content")
                    counts["invalid_lines"] += 1
                source, confidence = value.get("source"), value.get("confidence")
                issues.extend(_evidence_issues(source, confidence, threshold))
                polygons = parse_source(source)
                if polygons and any(p["page_number"] != page.get("pageNumber") for p in polygons):
                    issues.append("OCR source page conflicts with containing page")
                lines.append({
                    "id": identity, "content_index": content_index,
                    "page_index": page_index, "page_number": deepcopy(page.get("pageNumber")),
                    "line_index": line_index, "region": "OCR", "category": "ocr",
                    "key": "", "raw_text": text, "detail": "",
                    "source": deepcopy(source), "confidence": deepcopy(confidence),
                    "polygons": polygons, "page_context": _entry_context(polygons, contexts),
                    "raw": deepcopy(raw), "review_reasons": issues,
                })
                warnings.extend(f"{identity}: {issue}" for issue in issues)
    return items, lines, warnings, counts


def _identity(entry):
    return tuple(normalize_text(entry[name]) for name in ("region", "category", "key"))


def _text(entry):
    return normalize_text(entry["raw_text"])


def _bom_parts(entry):
    if normalize_text(entry["category"]) != "BOM":
        return None, None, None
    payload = _text(entry)
    row = _BOM_ROW_NUMBER.match(payload)
    row_number = row.group(1) if row else None
    if row:
        payload = row.group(2)
    quantity = _BOM_QUANTITY.search(payload)
    role = payload[:quantity.start()].strip() if quantity else ""
    if not role or not any(character.isalpha() for character in role):
        role = None
    return role, payload, row_number


def _bom_identity(entry):
    role, _, _ = _bom_parts(entry)
    region = normalize_text(entry["region"])
    return (entry["content_index"], region, role) if role and region else None


def _bom_record(old, new, method, certainty="high"):
    record = _record(old, new, method, 1.0, certainty)
    role, old_payload, old_row = _bom_parts(old)
    _, new_payload, new_row = _bom_parts(new)
    relocated = (normalize_text(old["key"]) != normalize_text(new["key"])
                 or old_row != new_row)
    record["component_role"] = role
    record["row_relocated"] = relocated
    if relocated and old_payload == new_payload:
        record["change"] = "relocated"
    return record


def _same_page(left, right):
    # Contents may be separate documents, so their page numbers are not global.
    return (left["content_index"] == right["content_index"]
            and left.get("page_number") == right.get("page_number")
            and isinstance(left.get("page_number"), int)
            and not isinstance(left.get("page_number"), bool)
            and left["page_number"] > 0)


def _box(entry):
    if len(entry["polygons"]) != 1 or len(entry["page_context"]) != 1:
        return None
    if "OCR source page conflicts with containing page" in entry["review_reasons"]:
        return None
    context = entry["page_context"][0]
    if not all(_valid_number(context[key]) and context[key] > 0 for key in ("width", "height")):
        return None
    points = entry["polygons"][0]["points"]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    if min(xs) < 0 or min(ys) < 0 or max(xs) > context["width"] or max(ys) > context["height"]:
        return None
    box = min(xs), min(ys), max(xs), max(ys)
    return box if box[2] > box[0] and box[3] > box[1] else None


def _overlap(left, right):
    a, b = _box(left), _box(right)
    if (a is None or b is None or left["content_index"] != right["content_index"]
            or left["page_context"] != right["page_context"]):
        return 0.0
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / union if union else 0.0


def _similarity(left, right):
    return SequenceMatcher(None, _text(left), _text(right), autojunk=False).ratio()


def _record(old, new, method, score, certainty):
    entry = old if old is not None else new
    detail_changed = (old is not None and new is not None
                      and normalize_text(old["detail"]) != normalize_text(new["detail"]))
    if old is None:
        change = "unpaired_new"
    elif new is None:
        change = "unpaired_old"
    else:
        if _text(old) != _text(new):
            change = "modified"
        elif detail_changed or _identity(old) != _identity(new):
            change = "interpretation_only"
        else:
            change = "unchanged"
    reasons = []
    if detail_changed:
        reasons.append("generated Detail changed; retained separately from document text")
    if change == "interpretation_only":
        reasons.append("generated interpretation changed without a RawText modification")
    for side, value in (("old", old), ("new", new)):
        if value is not None:
            reasons.extend(f"{side}: {reason}" for reason in value["review_reasons"])
    if certainty != "high":
        reasons.append("unpaired evidence" if method == "unpaired" else "uncertain correspondence")
    return {
        "region": entry["region"], "category": entry["category"], "key": entry["key"],
        "old": old, "new": new, "change": change,
        "detail_changed": detail_changed,
        "match": {"method": method, "score": round(score, 6), "certainty": certainty},
        "review_required": bool(reasons), "review_reasons": reasons,
    }


def _mutual_candidates(old, new, old_free, new_free, candidate):
    candidates = []
    by_old, by_new = defaultdict(list), defaultdict(list)
    for i in sorted(old_free):
        for j in sorted(new_free):
            evidence = candidate(old[i], new[j])
            if evidence is not None:
                score, method = evidence
                pair = (score, i, j, method)
                candidates.append(pair)
                by_old[i].append(score)
                by_new[j].append(score)
    selected = []
    for score, i, j, method in candidates:
        a, b = sorted(by_old[i], reverse=True), sorted(by_new[j], reverse=True)
        if score == a[0] == b[0] and (len(a) == 1 or score - a[1] >= 0.15) and (
                len(b) == 1 or score - b[1] >= 0.15):
            selected.append((i, j, method, score))
    return selected


def _compare_items(old, new):
    old_free, new_free = set(range(len(old))), set(range(len(new)))
    records, warnings = [], []
    old_keys, new_keys = defaultdict(list), defaultdict(list)
    for i, entry in enumerate(old):
        old_keys[_identity(entry)].append(i)
    for j, entry in enumerate(new):
        new_keys[_identity(entry)].append(j)
    for key in sorted(set(old_keys) | set(new_keys)):
        left, right = old_keys[key], new_keys[key]
        if len(left) > 1 or len(right) > 1:
            warnings.append(f"Duplicate schema identity {key!r}; stable-key matching disabled for this identity")

    old_roles, new_roles = defaultdict(list), defaultdict(list)
    for entries, roles in ((old, old_roles), (new, new_roles)):
        for index, entry in enumerate(entries):
            identity = _bom_identity(entry)
            if identity is not None:
                roles[identity].append(index)
    # BOM row numbers are positions, not stable component identities.
    for identity in sorted(set(old_roles) & set(new_roles)):
        left, right = old_roles[identity], new_roles[identity]
        if len(left) == len(right) == 1:
            i, j = left[0], right[0]
            records.append(_bom_record(old[i], new[j], "unique_bom_component_role"))
            old_free.remove(i)
            new_free.remove(j)
        else:
            # Exact full row payload can disambiguate repeated descriptions,
            # without treating an arbitrary part identifier as a universal key.
            old_payloads, new_payloads = defaultdict(list), defaultdict(list)
            for i in left:
                old_payloads[_bom_parts(old[i])[1]].append(i)
            for j in right:
                new_payloads[_bom_parts(new[j])[1]].append(j)
            for payload in sorted(set(old_payloads) & set(new_payloads)):
                a, b = old_payloads[payload], new_payloads[payload]
                if len(a) == len(b) == 1:
                    i, j = a[0], b[0]
                    records.append(_bom_record(old[i], new[j], "unique_bom_row_payload"))
                    old_free.remove(i)
                    new_free.remove(j)

    for key in sorted(set(old_keys) | set(new_keys)):
        left, right = old_keys[key], new_keys[key]
        if all(key) and len(left) == len(right) == 1:
            i, j = left[0], right[0]
            if i not in old_free or j not in new_free:
                continue
            old_role, old_payload, _ = _bom_parts(old[i])
            new_role, new_payload, _ = _bom_parts(new[j])
            if old_role != new_role:
                warnings.append(f"BOM identity {key!r}: component descriptions conflict or are incomplete; "
                                "row-key matching disabled")
                continue
            identity = _bom_identity(old[i])
            if identity is not None and (len(old_roles[identity]) > 1 or len(new_roles[identity]) > 1):
                if old_payload != new_payload:
                    warnings.append(f"BOM identity {key!r}: repeated component description; "
                                    "row-key matching disabled for changed payload")
                    continue
            records.append(_record(old[i], new[j], "unique_region_category_key", 1.0, "high"))
            old_free.remove(i)
            new_free.remove(j)

    def candidate(left, right):
        if (not normalize_text(left["category"])
                or normalize_text(left["category"]) != normalize_text(right["category"])
                or left["content_index"] != right["content_index"]):
            return None
        left_role, _, _ = _bom_parts(left)
        right_role, _, _ = _bom_parts(right)
        if left_role and right_role and left_role != right_role:
            return None
        same_region = (normalize_text(left["region"]) == normalize_text(right["region"])
                       and bool(normalize_text(left["region"])))
        text_equal = bool(_text(left)) and _text(left) == _text(right)
        overlap = _overlap(left, right)
        similarity = _similarity(left, right)
        if overlap >= 0.65 and similarity >= 0.55:
            return 0.65 * overlap + 0.35 * similarity, "geometry_text"
        if same_region and text_equal:
            return 0.8, "unique_text_same_region"
        return None

    for i, j, method, score in _mutual_candidates(old, new, old_free, new_free, candidate):
        records.append(_record(old[i], new[j], method, score, "uncertain"))
        old_free.remove(i)
        new_free.remove(j)
    records.extend(_record(old[i], None, "unpaired", 0, "unpaired") for i in sorted(old_free))
    records.extend(_record(None, new[j], "unpaired", 0, "unpaired") for j in sorted(new_free))
    return records, warnings


def _line_group(entries):
    result = deepcopy(entries[0])
    result.update({
        "id": "+".join(entry["id"] for entry in entries),
        "raw_text": " ".join(entry["raw_text"] for entry in entries),
        "source": [entry["source"] for entry in entries],
        "confidence": [entry["confidence"] for entry in entries],
        "polygons": [polygon for entry in entries for polygon in entry["polygons"]],
        "page_context": [context for entry in entries for context in entry["page_context"]],
        "raw": [entry["raw"] for entry in entries],
        "review_reasons": list(dict.fromkeys(
            reason for entry in entries for reason in entry["review_reasons"])),
        "lines": entries,
    })
    return result


def _contiguous(entries):
    if not entries or not all(_same_page(entries[0], entry) for entry in entries):
        return False
    if any(entries[index + 1]["line_index"] != entries[index]["line_index"] + 1
           or entries[index + 1]["page_index"] != entries[index]["page_index"]
           for index in range(len(entries) - 1)):
        return False
    boxes = [_box(entry) for entry in entries]
    if any(box is None for box in boxes):
        return False
    for first, second in zip(boxes, boxes[1:]):
        height = max(first[3] - first[1], second[3] - second[1])
        # Reject columns, reversed reading order, and distant text blocks.
        if (second[1] < first[3] - 0.2 * height
                or second[1] - first[3] > 1.5 * height
                or abs(first[0] - second[0]) > 2 * height):
            return False
    return True


def _group_overlap(single, entries):
    if not _contiguous(entries) or _box(single) is None:
        return 0.0
    if any(single["page_context"] != entry["page_context"] for entry in entries):
        return 0.0
    boxes = [_box(entry) for entry in entries]
    x1, y1 = min(box[0] for box in boxes), min(box[1] for box in boxes)
    x2, y2 = max(box[2] for box in boxes), max(box[3] for box in boxes)
    envelope = deepcopy(entries[0])
    envelope["polygons"][0]["points"] = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
    return _overlap(single, envelope)


def _reconcile_lines(old, new, old_free, new_free):
    candidates = []
    # Bound the grouping search; longer or geometry-free runs remain uncertain.
    for reverse, singles, multiples, single_free, multi_free in (
        (False, old, new, old_free, new_free),
        (True, new, old, new_free, old_free),
    ):
        for i in sorted(single_free):
            for start in sorted(multi_free):
                for length in range(2, min(8, len(multiples) - start) + 1):
                    indices = tuple(range(start, start + length))
                    if any(index not in multi_free for index in indices):
                        break
                    entries = [multiples[index] for index in indices]
                    if not _same_page(singles[i], entries[0]):
                        continue
                    joined = normalize_text(" ".join(entry["raw_text"] for entry in entries))
                    if not joined or _text(singles[i]) != joined:
                        continue
                    overlap = _group_overlap(singles[i], entries)
                    if overlap < 0.65:
                        continue
                    a, b = (indices, (i,)) if reverse else ((i,), indices)
                    candidates.append((a, b, overlap))
    usage_old = Counter(i for a, _, _ in candidates for i in a)
    usage_new = Counter(j for _, b, _ in candidates for j in b)
    records = []
    for a, b, score in candidates:
        if any(usage_old[i] != 1 for i in a) or any(usage_new[j] != 1 for j in b):
            continue
        left = old[a[0]] if len(a) == 1 else _line_group([old[i] for i in a])
        right = new[b[0]] if len(b) == 1 else _line_group([new[j] for j in b])
        record = _record(left, right, "contiguous_exact_join", score, "uncertain")
        record["change"] = "reconciled"
        records.append(record)
        old_free.difference_update(a)
        new_free.difference_update(b)
    return records


def _compare_lines(old, new):
    old_free, new_free = set(range(len(old))), set(range(len(new)))
    records = []

    def exact_candidate(left, right):
        if not _same_page(left, right) or not _text(left) or _text(left) != _text(right):
            return None
        overlap = _overlap(left, right)
        return (0.8 + 0.2 * overlap, "exact_text_geometry" if overlap >= 0.65 else "unique_exact_text")

    for i, j, method, score in _mutual_candidates(old, new, old_free, new_free, exact_candidate):
        # Identical text alone establishes content equality, not certain location.
        certainty = "high" if method == "exact_text_geometry" else "uncertain"
        records.append(_record(old[i], new[j], method, score, certainty))
        old_free.remove(i)
        new_free.remove(j)
    records.extend(_reconcile_lines(old, new, old_free, new_free))

    def changed_candidate(left, right):
        if not _same_page(left, right):
            return None
        overlap, similarity = _overlap(left, right), _similarity(left, right)
        if overlap >= 0.65 and similarity >= 0.55 and _text(left) and _text(right):
            return 0.65 * overlap + 0.35 * similarity, "geometry_text"
        return None

    for i, j, method, score in _mutual_candidates(old, new, old_free, new_free, changed_candidate):
        records.append(_record(old[i], new[j], method, score, "uncertain"))
        old_free.remove(i)
        new_free.remove(j)
    records.extend(_record(old[i], None, "unpaired", 0, "unpaired") for i in sorted(old_free))
    records.extend(_record(None, new[j], "unpaired", 0, "unpaired") for j in sorted(new_free))
    return records


def _coverage(records, old_count, new_count):
    def size(value):
        return len(value["lines"]) if "lines" in value else 1

    paired = [record for record in records if record["old"] is not None and record["new"] is not None]
    return {
        "old_total": old_count, "new_total": new_count,
        "matched_old": sum(size(record["old"]) for record in paired),
        "matched_new": sum(size(record["new"]) for record in paired),
        "unpaired_old": sum(record["change"] == "unpaired_old" for record in records),
        "unpaired_new": sum(record["change"] == "unpaired_new" for record in records),
        "modified_pairs": sum(record["change"] == "modified" for record in records),
        "interpretation_only_pairs": sum(record["change"] == "interpretation_only" for record in records),
        "unchanged_pairs": sum(record["change"] == "unchanged" for record in records),
        "reconciled_groups": sum(record["change"] == "reconciled" for record in records),
        "relocated_pairs": sum(record["change"] == "relocated" for record in records),
        "review_required": sum(record["review_required"] for record in records),
    }


def compare_documents(old, new, *, confidence_threshold=0.8):
    """Return a JSON-compatible report without mutating either raw operation.

    Match scores are heuristics, not extraction confidence or probabilities.
    ``unchanged``/``ocr_unchanged`` may still require review. OCR split/merge
    reconciliations appear in ``ocr_differences`` as ``reconciled`` with uncertain
    correspondence, preserving all contributing lines. Image-only changes and
    omitted OCR evidence are outside this comparator's guarantees. Schema
    ``Uncertainties`` and service warnings retain their raw evidence in dedicated
    output arrays and set the report-level ``review_required`` flag.
    """
    if not _valid_number(confidence_threshold) or not 0 <= confidence_threshold <= 1:
        raise ValueError("confidence_threshold must be a finite number between 0 and 1")
    old_items, old_lines, old_warnings, old_counts = _extract(old, "old", confidence_threshold)
    new_items, new_lines, new_warnings, new_counts = _extract(new, "new", confidence_threshold)
    old_uncertainties, old_service_warnings, old_diagnostics = _diagnostics(old, "old")
    new_uncertainties, new_service_warnings, new_diagnostics = _diagnostics(new, "new")
    uncertainties = old_uncertainties + new_uncertainties
    service_warnings = old_service_warnings + new_service_warnings
    item_records, pairing_warnings = _compare_items(old_items, new_items)
    ocr_records = _compare_lines(old_lines, new_lines)
    warnings = old_warnings + new_warnings + pairing_warnings + old_diagnostics + new_diagnostics
    review_required = (bool(warnings) or bool(uncertainties) or bool(service_warnings)
                       or any(record["review_required"] for record in item_records + ocr_records))
    if review_required:
        warnings.append("Evidence requires review; uncertain or unpaired entries are not confirmed additions/deletions")
    warnings.append("Comparison covers extracted schema items and OCR lines only; it cannot guarantee all drawing changes")
    return {
        "differences": [record for record in item_records if record["change"] != "unchanged"],
        "unchanged": [record for record in item_records if record["change"] == "unchanged"],
        "ocr_differences": [record for record in ocr_records if record["change"] != "unchanged"],
        "ocr_unchanged": [record for record in ocr_records if record["change"] == "unchanged"],
        "coverage": {
            "schema": _coverage(item_records, len(old_items), len(new_items)),
            "ocr": _coverage(ocr_records, len(old_lines), len(new_lines)),
            "old_extraction": old_counts, "new_extraction": new_counts,
            "uncertainty_count": len(uncertainties),
            "service_warning_count": len(service_warnings),
            "scope": "custom_schema_items_and_full_ocr_lines",
            "completeness": "not_guaranteed",
        },
        "uncertainties": uncertainties,
        "service_warnings": service_warnings,
        "review_required": review_required,
        "warnings": warnings,
    }


# Short alias for callers that do not need to distinguish documents from pages.
compare = compare_documents
compare_responses = compare_documents
