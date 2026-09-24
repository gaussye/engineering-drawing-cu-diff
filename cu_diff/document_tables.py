"""Conservative table comparisons grounded in PDF rulings and cached OCR.

``compare_document_tables(old_pdf, new_pdf, old_operation, new_operation)``
returns browser-ready ``items``, ``coverage``, and ``warnings``. Items use IDs
T001..., channel ``tables``, normalized ``locations``, and separate
``table_context`` on both sides. Missing columns have a genuinely null side.
``table_cell_modified`` compares observed nonempty cell text.
``table_grid_changed`` describes measured ruled bands, not deleted records.

Scope is deliberately bounded: unrotated PDFs, complete rectilinear vector
grids, unique recognized top/bottom header rows, and mutually unique table
matches. OCR literals are not corrected. Empty OCR is not evidence of a blank
cell: rendered cell ink is checked. No network, OCR service, or CU table counts
are used. Raster/merged/partial grids and ambiguous records remain diagnostics.
Callers must supply the cached operation belonging to each displayed PDF;
dimensions alone cannot authenticate document identity.

Keyword-only ``include_bom=True`` enables BOM evidence without ``fields.Items``.
The default excludes BOMs for callers whose schema channel already handles them.
CU HTML/cell records may be present but never replace source ruling/ink checks.
"""

from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
import math
import re

from .evidence import parse_source


_HEADERS = {
    "customerpn": "customer_part_number", "customerpartnumber": "customer_part_number",
    "longwellpn": "supplier_part_number", "supplierpn": "supplier_part_number",
    "manufacturerpn": "supplier_part_number", "partnumber": "part_number",
    "l": "length", "l1": "length_1", "l2": "length_2",
    "length": "length", "barcodetag": "barcode_tag", "netweight": "net_weight",
    "rev": "revision", "revision": "revision", "ecnno": "ecn",
    "ecn": "ecn", "description": "description", "appd": "approved",
    "approved": "approved", "date": "date",
    "no": "number", "item": "number", "bomitem": "description",
    "qty": "quantity", "quantity": "quantity", "quantty": "quantity", "quty": "quantity",
    "unit": "unit", "material": "material", "pn": "part_number",
}
_EPS = 0.75
_BOM_HEADERS = {
    "itemno": "number", "number": "number", "no": "number",
    "partno": "part_number", "partnumber": "part_number",
    "units": "unit", "um": "unit", "qty": "quantity",
    "manufacturer": "manufacturer", "supplier": "manufacturer",
    "flameclass": "flame_class", "fireproofingrank": "flame_class",
}


def _key(text):
    return re.sub(r"[\W_]", "", text.casefold())


def _bounds(points):
    return (min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points))


def _location(box, page, width, height):
    x0, y0, x1, y1 = box
    return {"page": page, "x": x0 / width, "y": y0 / height,
            "width": (x1 - x0) / width, "height": (y1 - y0) / height,
            "polygon": [[x0 / width, y0 / height], [x1 / width, y0 / height],
                        [x1 / width, y1 / height], [x0 / width, y1 / height]]}


def _merge_lines(lines):
    """Merge collinear source pieces, never bridge a missing ruled boundary."""
    buckets = []
    for coordinate, start, end in sorted(lines):
        target = next((group for group in buckets
                       if abs(group[0] - coordinate) <= _EPS), None)
        if target is None:
            target = [coordinate, []]
            buckets.append(target)
        target[1].append((start, end, coordinate))
    merged = []
    for coordinate, intervals in buckets:
        start, end, coordinate = sorted(intervals)[0]
        representative_length = end - start
        for left, right, candidate_coordinate in sorted(intervals)[1:]:
            if left <= end + _EPS:
                if right - left > representative_length:
                    coordinate = candidate_coordinate
                    representative_length = right - left
                end = max(end, right)
            else:
                merged.append((coordinate, start, end))
                start, end, coordinate = left, right, candidate_coordinate
                representative_length = right - left
        merged.append((coordinate, start, end))
    return merged


def _grids(page, *, extend_shared_borders=False):
    horizontal, vertical = [], []

    def segment(a, b):
        if abs(a.y - b.y) <= 0.15 and abs(a.x - b.x) >= 4:
            horizontal.append(((a.y + b.y) / 2, min(a.x, b.x), max(a.x, b.x)))
        elif abs(a.x - b.x) <= 0.15 and abs(a.y - b.y) >= 4:
            vertical.append(((a.x + b.x) / 2, min(a.y, b.y), max(a.y, b.y)))

    for drawing in page.get_drawings():
        for item in drawing["items"]:
            if item[0] == "l" and drawing.get("type") in ("s", "fs"):
                segment(item[1], item[2])
            elif item[0] == "re":
                rect = item[1]
                if drawing.get("type") in ("s", "fs"):
                    for a, b in ((rect.tl, rect.tr), (rect.tr, rect.br),
                                 (rect.br, rect.bl), (rect.bl, rect.tl)):
                        segment(a, b)
                elif drawing.get("type") == "f":
                    # CAD exporters may encode a ruling as a thin filled bar.
                    if 0 < rect.height <= .8 and rect.width >= 4:
                        horizontal.append(((rect.y0 + rect.y1) / 2, rect.x0, rect.x1))
                    elif 0 < rect.width <= .8 and rect.height >= 4:
                        vertical.append(((rect.x0 + rect.x1) / 2, rect.y0, rect.y1))
    # A panel bottom may coincide with a longer page border. Keep its explicit
    # source segment as well as merged spans, rather than losing the panel edge.
    horizontal, vertical = horizontal + _merge_lines(horizontal), _merge_lines(vertical)
    groups = []
    for y, left, right in horizontal:
        group = next((g for g in groups if abs(g["left"] - left) <= _EPS
                      and abs(g["right"] - right) <= _EPS), None)
        if group is None:
            group = {"left": left, "right": right, "ys": []}
            groups.append(group)
        if not any(abs(y - existing) <= _EPS for existing in group["ys"]):
            group["ys"].append(y)
    bounded_groups = []
    for group in groups:
        band = []
        ys = sorted(group["ys"])
        for top, bottom in zip(ys, ys[1:]):
            bounded = all(any(abs(x - edge) <= _EPS and start <= top + _EPS
                              and end >= bottom - _EPS for x, start, end in vertical)
                          for edge in (group["left"], group["right"]))
            if bounded:
                if not band:
                    band.append(top)
                band.append(bottom)
            elif band:
                bounded_groups.append(dict(group, ys=band))
                band = []
        if band:
            bounded_groups.append(dict(group, ys=band))
    found = []
    for group in bounded_groups:
        ys = group["ys"]
        left, right = group["left"], group["right"]
        if extend_shared_borders:
            supports = [(x, top, bottom) for x, top, bottom in vertical
                        if left - _EPS <= x <= right + _EPS
                        and top <= ys[0] + _EPS and bottom >= ys[-1] - _EPS]
            if len(supports) >= 3:
                top, bottom = max(s[1] for s in supports), min(s[2] for s in supports)
                # A BOM header often shares the longer page-border ruling.
                # Extend only through actual full-width lines AND every
                # existing column boundary; never infer a missing top edge.
                for y, start, end in horizontal:
                    if (top - _EPS <= y <= bottom + _EPS
                            and start <= left + _EPS and end >= right - _EPS
                            and not any(abs(y - previous) <= _EPS for previous in ys)):
                        ys = sorted([*ys, y])
        if not 3 <= len(ys) <= 101:
            continue
        if (left < 0 or ys[0] < 0 or right > page.rect.width
                or ys[-1] > page.rect.height):
            continue
        xs = sorted(x for x, top, bottom in vertical
                    if left - _EPS <= x <= right + _EPS
                    and top <= ys[0] + _EPS and bottom >= ys[-1] - _EPS)
        if (not 2 <= len(xs) <= 33 or abs(xs[0] - left) > _EPS
                or abs(xs[-1] - right) > _EPS
                or min(b - a for a, b in zip(xs, xs[1:])) < 4
                or min(b - a for a, b in zip(ys, ys[1:])) < 4):
            continue
        # Partial internal rulings imply a merged/irregular grid, not a blank cell.
        partial = any(left + _EPS < x < right - _EPS
                      and top < ys[-1] - _EPS and bottom > ys[0] + _EPS
                      and sum(top - _EPS <= y <= bottom + _EPS for y in ys) >= 2
                      and not any(abs(x - full) <= _EPS for full in xs)
                      for x, top, bottom in vertical)
        if not partial:
            found.append({"xs": xs, "ys": ys, "box": (left, ys[0], right, ys[-1])})
    return found


def _ocr_pages(operation, document, side, warnings):
    if not isinstance(operation, dict) or operation.get("status") != "Succeeded":
        warnings.append(f"{side}: OCR operation is not Succeeded; no table claims.")
        return {}
    result = {}
    payload = operation.get("result")
    contents = payload.get("contents") if isinstance(payload, dict) else None
    if not isinstance(contents, list):
        warnings.append(f"{side}: invalid OCR contents; no table claims.")
        return {}
    for content in contents:
        if not isinstance(content, dict) or not isinstance(content.get("pages"), list):
            warnings.append(f"{side}: invalid OCR page collection.")
            continue
        for evidence in content["pages"]:
            if not isinstance(evidence, dict):
                warnings.append(f"{side}: invalid OCR page.")
                continue
            number = evidence.get("pageNumber")
            if not isinstance(number, int) or not 1 <= number <= len(document):
                warnings.append(f"{side}: invalid OCR page number.")
                continue
            page = document[number - 1]
            width, height = evidence.get("width"), evidence.get("height")
            if (page.rotation != 0 or evidence.get("unit", content.get("unit")) != "inch"
                    or not all(isinstance(v, (int, float)) and math.isfinite(v)
                               and v > 0 for v in (width, height))
                    or abs(width * 72 - page.rect.width) > 1
                    or abs(height * 72 - page.rect.height) > 1):
                warnings.append(f"{side} page {number}: PDF/OCR geometry mismatch.")
                continue
            if number in result:
                warnings.append(f"{side} page {number}: duplicate OCR page; ambiguous evidence.")
                result[number] = None
                continue
            words, invalid = [], False
            raw_words = evidence.get("words", [])
            if not isinstance(raw_words, list):
                warnings.append(f"{side} page {number}: invalid OCR words.")
                result[number] = None
                continue
            for word in raw_words:
                if not isinstance(word, dict):
                    invalid = True
                    continue
                polygons = parse_source(word.get("source"))
                text = word.get("content")
                if (not isinstance(text, str) or not text.strip() or len(polygons) != 1
                        or polygons[0]["page_number"] != number):
                    invalid = True
                    continue
                points = [[x * 72, y * 72] for x, y in polygons[0]["points"]]
                box = _bounds(points)
                if (box[0] < -_EPS or box[1] < -_EPS
                        or box[2] > page.rect.width + _EPS
                        or box[3] > page.rect.height + _EPS):
                    invalid = True
                    continue
                words.append({"text": text, "box": box, "source": word["source"],
                              "confidence": word.get("confidence")})
            if invalid:
                warnings.append(f"{side} page {number}: some OCR words have invalid source; "
                                "ink checks may mark affected cells unresolved.")
            result[number] = words
    return result


def _cell(page, box, words, rendered=None):
    import numpy as np
    import pymupdf

    x0, y0, x1, y1 = box
    selected = [word for word in words
                if x0 < (word["box"][0] + word["box"][2]) / 2 < x1
                and y0 < (word["box"][1] + word["box"][3]) / 2 < y1]
    selected.sort(key=lambda word: (round(word["box"][1] / 3), word["box"][0]))
    # Preserve every OCR token verbatim. Whitespace between tokens is synthesized.
    text = " ".join(word["text"] for word in selected)
    interior = pymupdf.Rect(x0 + 1.1, y0 + 1.1, x1 - 1.1, y1 - 1.1)
    if rendered is None:
        pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=interior,
                              colorspace=pymupdf.csGRAY, alpha=False)
        image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
        origin_x, origin_y = pix.x, pix.y
    else:
        whole, offset_x, offset_y = rendered
        left, top = max(0, math.floor(interior.x0 * 2 - offset_x)), max(0, math.floor(interior.y0 * 2 - offset_y))
        right, bottom = min(whole.shape[1], math.ceil(interior.x1 * 2 - offset_x)), min(whole.shape[0], math.ceil(interior.y1 * 2 - offset_y))
        image = whole[top:bottom, left:right]
        origin_x, origin_y = offset_x + left, offset_y + top
    ink = image < 180
    covered = np.zeros_like(ink)
    crossing = False
    for word in selected:
        a, b, c, d = word["box"]
        crossing |= a < x0 - 1 or b < y0 - 1 or c > x1 + 1 or d > y1 + 1
        left, top = max(0, math.floor((a - 1) * 2 - origin_x)), max(0, math.floor((b - 1) * 2 - origin_y))
        right, bottom = min(image.shape[1], math.ceil((c + 1) * 2 - origin_x)), min(image.shape[0], math.ceil((d + 1) * 2 - origin_y))
        covered[top:bottom, left:right] = True
    unexplained = int(np.count_nonzero(ink & ~covered))
    residual_y, residual_x = np.nonzero(ink & ~covered)
    total_ink = int(np.count_nonzero(ink))
    overlapping = False
    for index, word in enumerate(selected):
        a, b, c, d = word["box"]
        for other in selected[index + 1:]:
            if word["text"] != other["text"]:
                continue
            e, f, g, h = other["box"]
            overlap = max(0, min(c, g) - max(a, e)) * max(0, min(d, h) - max(b, f))
            smaller = min((c - a) * (d - b), (g - e) * (h - f))
            overlapping |= smaller > 0 and overlap / smaller > .5
    complete = not crossing and not overlapping and unexplained <= max(4, total_ink * 0.02)
    return {"text": text, "words": selected, "box": box, "complete": complete,
            "blank": not selected and total_ink <= 4, "unexplained_ink": unexplained,
            "_residual_points": np.column_stack(((residual_x + origin_x) / 2,
                                                 (residual_y + origin_y) / 2))}


def _is_bom_header(cells, keys):
    return (any(_key(cell["text"]).startswith("bom") for cell in cells)
            or {"description", "quantity"}.issubset(keys))


def _header(cells, include_bom=False):
    if any(not cell["complete"] or not cell["text"] for cell in cells):
        return None
    if not include_bom and any(_key(cell["text"]).startswith("bom") for cell in cells):
        return None
    aliases = _HEADERS | (_BOM_HEADERS if include_bom else {})
    keys = [aliases.get(_key(cell["text"]), "header:" + _key(cell["text"])) for cell in cells]
    if len(set(keys)) != len(keys):
        return None
    recognized = {key for key in keys if not key.startswith("header:")}
    bom = _is_bom_header(cells, keys)
    if bom and not include_bom:
        return None
    if len(recognized) < (2 if bom else 3):
        return None
    for cell in cells:
        if any(not isinstance(w["confidence"], (int, float)) or w["confidence"] < .8
               for w in cell["words"]):
            return None
    return keys


def _printed_panel_header(cell):
    text = cell["text"]
    words = re.findall(r"[A-Za-z]+", text)
    return (cell["complete"] and 2 <= len(words) <= 12 and len(text) <= 100
            and not re.search(r"\d", text) and all(word.isupper() for word in words)
            and _key(text) not in _HEADERS and not _key(text).startswith("bom")
            and all(isinstance(word["confidence"], (int, float))
                    and word["confidence"] >= .8 for word in cell["words"]))


def _extract(path, operation, side, warnings, diagnostics=None, *, include_bom=False):
    import numpy as np
    import pymupdf

    tables = []
    try:
        document = pymupdf.open(Path(path))
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        warnings.append(f"{side}: PDF source unavailable ({type(error).__name__}); no table claims.")
        return tables
    with document:
        pages = _ocr_pages(operation, document, side, warnings)
        for page in document:
            number = page.number + 1
            words = pages.get(number)
            if not words:
                warnings.append(f"{side} page {number}: source-grounded OCR unavailable.")
                continue
            pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), colorspace=pymupdf.csGRAY, alpha=False)
            rendered = (np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width),
                        pix.x, pix.y)
            accepted, rejected = 0, 0
            for grid in _grids(page, extend_shared_borders=include_bom):
                xs, ys = grid["xs"], grid["ys"]
                rows = [[_cell(page, (x0, y0, x1, y1), words, rendered)
                         for x0, x1 in zip(xs, xs[1:])] for y0, y1 in zip(ys, ys[1:])]
                grid.update({"page": number, "width": page.rect.width,
                             "height": page.rect.height, "rows": rows, "side": side})
                bom_header_seen = any(
                    keys and _is_bom_header(rows[index], keys)
                    for index in (0, len(rows) - 1)
                    for keys in [_header(rows[index], include_bom=True)])
                headers = [(index, _header(rows[index], include_bom=include_bom))
                           for index in (0, len(rows) - 1)]
                headers = [(index, keys) for index, keys in headers if keys]
                kind = ("bom" if len(headers) == 1 and _is_bom_header(rows[headers[0][0]], headers[0][1])
                        else "column_table")
                if (not headers and len(xs) == 2 and _printed_panel_header(rows[0][0])
                        and not _printed_panel_header(rows[-1][0])):
                    headers = [(0, ["panel:" + " ".join(rows[0][0]["text"].casefold().split())])]
                    kind = "printed_header_panel"
                if diagnostics is not None:
                    diagnostics.append({
                        "page": number, "ruled_bands": len(ys) - 1, "columns": len(xs) - 1,
                        "header_assignment": headers[0][0] if len(headers) == 1 else None,
                        "status": "eligible" if len(headers) == 1 else "unmatched_or_excluded",
                        "table_kind": "bom" if bom_header_seen else kind,
                        "exclusion_reason": "bom_disabled" if bom_header_seen and not include_bom else None,
                        "first_band_ocr": " | ".join(cell["text"] for cell in rows[0]),
                        "locations": [_location(grid["box"], number, page.rect.width, page.rect.height)],
                        "horizontal_boundaries_points": ys, "vertical_boundaries_points": xs,
                        "meaning": "Ruled bands are source geometry, not populated records or OCR text lines.",
                    })
                if len(headers) != 1:
                    rejected += 1
                    continue
                index, keys = headers[0]
                grid.update({"header": index, "keys": keys, "kind": kind})
                tables.append(grid)
                accepted += 1
            if not accepted:
                scope = "table" if include_bom else "non-BOM table"
                warnings.append(f"{side} page {number}: no eligible {scope}: "
                                "requires complete vector grid and unique readable header.")
            if rejected:
                exclusions = "unrecognized/incomplete, or ambiguous headers" if include_bom else "BOM, unrecognized/incomplete, or ambiguous headers"
                warnings.append(f"{side} page {number}: {rejected} source grids skipped "
                                f"({exclusions}).")
    return tables


def _entry(table, cells=None, structural=False):
    cells = cells if cells is not None else [c for row in table["rows"] for c in row]
    words = [word for cell in cells for word in cell["words"]]
    boxes = [table["box"]] if structural else [word["box"] for word in words]
    confidence = [w["confidence"] for w in words if isinstance(w["confidence"], (int, float))]
    return {"raw_text": (f"{len(table['ys']) - 1} ruled rows (including header) "
                         f"x {len(table['xs']) - 1} columns" if structural else
                         " | ".join(cell["text"] for cell in cells if cell["text"])),
            "confidence": min(confidence) if confidence and not structural else None,
            "source": ("PDF vector rulings (points)" if structural else
                       ";".join(word["source"] for word in words)),
            "locations": [_location(box, table["page"], table["width"], table["height"])
                          for box in boxes],
            "location_error": None if boxes else "No observed text; not inferred evidence.",
            "detail": {"coordinate_basis": "unrotated_pdf_points",
                       "evidence_kind": "vector_grid" if structural else "cached_ocr_words",
                       "cell_text_coverage_complete": all(cell["complete"] for cell in cells)}}


def _measurement(table):
    def line(a, b):
        return [[a[0] / table["width"], a[1] / table["height"]],
                [b[0] / table["width"], b[1] / table["height"]]]

    xs, ys = table["xs"], table["ys"]
    data = [row for index, row in enumerate(table["rows"]) if index != table["header"]]
    return {"rows_including_header": len(ys) - 1, "columns": len(xs) - 1,
            "blank_rows_verified": sum(all(cell["blank"] for cell in row) for row in data),
            "populated_rows_observed": sum(any(cell["text"] for cell in row) for row in data),
            "page": table["page"],
            "horizontal_lines": [line((xs[0], y), (xs[-1], y)) for y in ys],
            "vertical_lines": [line((x, ys[0]), (x, ys[-1])) for x in xs]}


def _record(old, new, change, key, old_cells=None, new_cells=None, structural=False):
    scope = ("structural_grid_only" if structural else
             "column_presence_only" if change.startswith("table_column_") else
             "observed_row_presence" if change in ("table_row_added", "table_row_removed") else
             "observed_cell_presence" if change in ("table_cell_added", "table_cell_removed") else
             "observed_cell_text_pair")
    return {"channel": "tables", "region": "document_table", "key": key, "change": change,
            "review_required": True,
            "review_reasons": ["Source-grounded candidate; OCR and engineering meaning need review."],
            "match": {"method": "unique_header_and_validated_pdf_grid"},
            "old": _entry(old, old_cells, structural) if old_cells is not None or structural else None,
            "new": _entry(new, new_cells, structural) if new_cells is not None or structural else None,
            "table_context": {"old": _entry(old, structural=True), "new": _entry(new, structural=True)},
            "table_comparison": {"status": "complete", "scope": scope,
                                 "table_kind": old["kind"],
                                 "meaning": "Complete only for the stated evidence scope, not OCR accuracy or whole-table coverage.",
                                 "old_grid": _measurement(old),
                                 "new_grid": _measurement(new)}}


def _ocr_lines(cells):
    words = [word for cell in cells for word in cell["words"]]
    groups = []
    for word in sorted(words, key=lambda w: ((w["box"][1] + w["box"][3]) / 2, w["box"][0])):
        center = (word["box"][1] + word["box"][3]) / 2
        height = word["box"][3] - word["box"][1]
        group = next((g for g in groups if abs(g["center"] - center) <=
                      .5 * min(g["height"], height)), None)
        if group is None:
            group = {"center": center, "height": height, "words": []}
            groups.append(group)
        group["words"].append(word)
    return [{"raw_text": " ".join(word["text"] for word in sorted(g["words"], key=lambda w: w["box"][0])),
             "sources": [word["source"] for word in sorted(g["words"], key=lambda w: w["box"][0])]}
            for g in sorted(groups, key=lambda g: g["center"])]


def _bounded_panel_span(cells, words):
    if (not words or any(not isinstance(word["confidence"], (int, float))
                        or word["confidence"] < .8 or "\ufffd" in word["text"] for word in words)
            or len(_ocr_lines([{"words": words}])) != 1):
        return None
    selected = {word["source"] for word in words}
    containers = [cell for cell in cells
                  if selected.issubset({word["source"] for word in cell["words"]})]
    if len(containers) != 1:
        return None
    cell = containers[0]
    box = _bounds([point for word in words
                   for point in ((word["box"][0], word["box"][1]),
                                 (word["box"][2], word["box"][3]))])
    x0, y0, x1, y1 = box
    a, b, c, d = cell["box"]
    if x0 < a or y0 < b or x1 > c or y1 > d:
        return None
    if any(word["source"] not in selected and
           x0 <= (word["box"][0] + word["box"][2]) / 2 <= x1 and
           y0 <= (word["box"][1] + word["box"][3]) / 2 <= y1 for word in cell["words"]):
        return None
    residual = cell["_residual_points"]
    inside = ((residual[:, 0] >= x0 - .5) & (residual[:, 0] <= x1 + .5)
              & (residual[:, 1] >= y0 - .5) & (residual[:, 1] <= y1 + .5))
    if int(inside.sum()) > 4:
        return None
    return {"text": " ".join(word["text"] for word in words), "words": words,
            "box": box, "complete": True, "blank": False}


def _verified_panel_spans(cells, lines):
    ordered = {}
    for side in ("old", "new"):
        words = [word for cell in cells[side] for word in cell["words"]]
        index = {word["source"]: word for word in words}
        if len(index) != len(words):
            return []
        ordered[side] = [index[source] for line in lines[side] for source in line["sources"]]
    texts = {side: [word["text"] for word in words] for side, words in ordered.items()}
    old, new = texts["old"], texts["new"]
    verified = []
    for tag, i0, i1, j0, j1 in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == "equal" or min(i0, j0) < 2 or i1 + 2 > len(old) or j1 + 2 > len(new):
            continue
        before, after = old[i0 - 2:i0], old[i1:i1 + 2]
        if before != new[j0 - 2:j0] or after != new[j1:j1 + 2]:
            continue
        # Both two-token anchors must be unique within each panel. Generic
        # repeated labels or numerical coincidences cannot establish this span.
        if any(sum(tokens[k:k + 2] == anchor for k in range(len(tokens) - 1)) != 1
               for tokens in (old, new) for anchor in (before, after)):
            continue
        left = _bounded_panel_span(cells["old"], ordered["old"][i0 - 2:i1 + 2])
        right = _bounded_panel_span(cells["new"], ordered["new"][j0 - 2:j1 + 2])
        if left is not None and right is not None:
            verified.append((left, right))
    return verified


def _compare_panel(old, new, warnings, diagnostics):
    cells = {side: [cell for index, row in enumerate(table["rows"]) if index != table["header"]
                    for cell in row] for side, table in (("old", old), ("new", new))}
    lines = {side: _ocr_lines(body) for side, body in cells.items()}
    text = {side: " ".join(line["raw_text"] for line in value) for side, value in lines.items()}
    label = old["rows"][old["header"]][0]["text"]
    issues = []
    for side in ("old", "new"):
        if not text[side]:
            issues.append(f"{side}: no observed body text.")
        if not all(cell["complete"] for cell in cells[side]):
            issues.append(f"{side}: body OCR does not account for all source ink or crosses a cell boundary.")
        if any(not isinstance(word["confidence"], (int, float)) or word["confidence"] < .8
               or "\ufffd" in word["text"] for cell in cells[side] for word in cell["words"]):
            issues.append(f"{side}: low-confidence or illegible body OCR literals; no corrected readings inferred.")
    equal = text["old"] == text["new"]
    metadata = {"scope": "observed_panel_text", "header": label,
                "old_ocr_lines": lines["old"], "new_ocr_lines": lines["new"],
                "old_ocr_line_count": len(lines["old"]), "new_ocr_line_count": len(lines["new"]),
                "normalized_joined_text_equal": equal,
                "line_count_meaning": "Observed OCR text layout, not ruled rows or populated records.",
                "issues": issues}
    if equal:
        status = ("unresolved" if issues else
                  "reflow_only" if len(lines["old"]) != len(lines["new"]) else "unchanged")
        diagnostics.append(dict(metadata, status=status))
        if issues:
            warnings.append(f"panel {label}: identical extracted text but source coverage is unresolved.")
        return []
    verified = _verified_panel_spans(cells, lines) if issues else []
    if verified:
        records = []
        for index, (left, right) in enumerate(verified, 1):
            record = _record(old, new, "table_cell_modified",
                             f"{label} / observed span {index}", [left], [right])
            record["table_comparison"].update(
                metadata, status="complete", evidence_extent="source_ink_validated_changed_span",
                panel_text_complete=False, panel_issues=issues, issues=[],
                meaning="Complete only for this uniquely anchored, ink-accounted text span; "
                        "other panel text remains unresolved.")
            record["match"]["method"] = "unique_printed_header_and_unique_ocr_span_anchors"
            record["review_reasons"].append("Other panel text has unresolved OCR; this is not whole-panel completeness.")
            records.append(record)
        diagnostics.append(dict(metadata, status="partially_resolved", verified_span_count=len(records)))
        warnings.append(f"panel {label}: {len(records)} source-validated changed text spans; "
                        "remaining incomplete/illegible body text is unresolved.")
        return records
    record = _record(old, new, "table_cell_modified", label, cells["old"], cells["new"])
    record["table_comparison"].update(metadata, status="review_only" if issues else "complete")
    record["match"]["method"] = "unique_printed_panel_header_and_validated_pdf_grid"
    for side in ("old", "new"):
        record[side]["raw_text"] = "\n".join(line["raw_text"] for line in lines[side])
    if issues:
        record["review_reasons"].extend(issues)
        warnings.append(f"panel {label}: changed extracted body text is review-only; incomplete/illegible OCR.")
    diagnostics.append(dict(metadata, status=record["table_comparison"]["status"]))
    return [record]


def _reliable_cell(cell):
    return cell["complete"] and all(
        isinstance(word["confidence"], (int, float)) and math.isfinite(word["confidence"])
        and word["confidence"] >= .8 and "\ufffd" not in word["text"]
        for word in cell["words"])


def _bom_pairs(old, new, old_rows, new_rows, warnings, label):
    identity_keys = set(old["keys"]) & set(new["keys"]) & {
        "description", "part_number", "supplier_part_number"}
    usable = [key for key in sorted(identity_keys) if all(
        _reliable_cell(row[table["keys"].index(key)])
        for table, rows in ((old, old_rows), (new, new_rows)) for row in rows)]
    edges, reverse = defaultdict(list), defaultdict(list)
    for i, left in enumerate(old_rows):
        for j, right in enumerate(new_rows):
            anchors = []
            for key in usable:
                oi, ni = old["keys"].index(key), new["keys"].index(key)
                value = left[oi]["text"]
                if (value and value == right[ni]["text"]
                        and sum(row[oi]["text"] == value for row in old_rows) == 1
                        and sum(row[ni]["text"] == value for row in new_rows) == 1):
                    anchors.append(key)
            if anchors:
                edges[i].append(j)
                reverse[j].append(i)
    indexes = [(i, js[0]) for i, js in edges.items()
               if len(js) == 1 and len(reverse[js[0]]) == 1]
    unmatched_old = [row for i, row in enumerate(old_rows) if i not in {i for i, _ in indexes}]
    unmatched_new = [row for j, row in enumerate(new_rows) if j not in {j for _, j in indexes}]
    presence = []
    if unmatched_old or unmatched_new:
        complete = all(_reliable_cell(cell) for table in (old, new)
                       for row in table["rows"] for cell in row)
        if complete and not (unmatched_old and unmatched_new):
            side, rows = ("old", unmatched_old) if unmatched_old else ("new", unmatched_new)
            for row in rows:
                record = _record(old, new, "table_row_removed" if side == "old" else "table_row_added",
                                 "BOM row presence", old_cells=row if side == "old" else None,
                                 new_cells=row if side == "new" else None)
                record["match"]["method"] = "complete_source_bom_and_exhaustive_identity_pairing"
                presence.append(record)
        else:
            warnings.append(f"{label}: ambiguous/unmatched BOM identities or incomplete OCR; "
                            "no row addition/removal claims.")
    return [(old_rows[i], new_rows[j]) for i, j in indexes], presence


def _bom_cell_presence(old, new, key, before, after):
    blank_side, blank, present = ("old", before, after) if before["blank"] else ("new", after, before)
    table = old if blank_side == "old" else new
    header = old["rows"][old["header"]][old["keys"].index(key)]["text"]
    record = _record(old, new, "table_cell_added" if blank_side == "old" else "table_cell_removed",
                     header, old_cells=None if blank_side == "old" else [present],
                     new_cells=None if blank_side == "new" else [present])
    record["table_comparison"]["blank_counterpart_context"] = {
        "side": blank_side, "source": "PDF cell rulings and rendered empty cell interior",
        "locations": [_location(blank["box"], table["page"], table["width"], table["height"])],
        "meaning": "Navigation context only; no missing-side OCR text or red evidence box.",
    }
    return record


def _compare_pair(old, new, warnings, panel_diagnostics):
    records = []
    label = f"table p{old['page']} ({', '.join(old['keys'])})"
    if len(old["ys"]) != len(new["ys"]):
        records.append(_record(old, new, "table_grid_changed", "Ruled row count",
                               structural=True))
    if old["kind"] == "printed_header_panel":
        return records + _compare_panel(old, new, warnings, panel_diagnostics)
    old_keys, new_keys = set(old["keys"]), set(new["keys"])
    for key in sorted(old_keys ^ new_keys):
        table = old if key in old_keys else new
        col = table["keys"].index(key)
        cells = [row[col] for row in table["rows"]]
        record = _record(old, new, "table_column_removed" if table is old else "table_column_added",
                         table["rows"][table["header"]][col]["text"],
                         old_cells=cells if table is old else None,
                         new_cells=cells if table is new else None)
        if any(not cell["complete"] for cell in cells):
            warnings.append(f"{label} {key}: column presence is source-grounded, "
                            "but its cell text has incomplete OCR; values remain unresolved.")
        records.append(record)
    old_rows = [row for i, row in enumerate(old["rows"])
                if i != old["header"] and not all(c["blank"] for c in row)]
    new_rows = [row for i, row in enumerate(new["rows"])
                if i != new["header"] and not all(c["blank"] for c in row)]
    # No identity heuristic using changed P/N, row positions, or similar text.
    # Single populated records pair by the unique table headers. Multi-record
    # tables require an unchanged ordinal/revision, or two independently unique
    # unchanged shared cells. A coincidentally equal length is not an identity.
    pairs = []
    shared = sorted(old_keys & new_keys)
    if old["kind"] == "bom":
        pairs, presence = _bom_pairs(old, new, old_rows, new_rows, warnings, label)
        records.extend(presence)
    elif len(old_rows) == len(new_rows) == 1:
        pairs = [(old_rows[0], new_rows[0])]
    elif old_rows or new_rows:
        edges = defaultdict(list)
        reverse = defaultdict(list)
        for i, left in enumerate(old_rows):
            for j, right in enumerate(new_rows):
                anchors = []
                for key in shared:
                    a, b = left[old["keys"].index(key)], right[new["keys"].index(key)]
                    if not a["complete"] or not b["complete"] or not a["text"] or a["text"] != b["text"]:
                        continue
                    unique_old = sum(r[old["keys"].index(key)]["text"] == a["text"] for r in old_rows) == 1
                    unique_new = sum(r[new["keys"].index(key)]["text"] == b["text"] for r in new_rows) == 1
                    if unique_old and unique_new:
                        anchors.append(key)
                if set(anchors) & {"number", "revision"} or len(anchors) >= 2:
                    edges[i].append(j)
                    reverse[j].append(i)
        pairs = [(old_rows[i], new_rows[js[0]]) for i, js in edges.items()
                 if len(js) == 1 and len(reverse[js[0]]) == 1]
        if len(pairs) != len(old_rows) or len(pairs) != len(new_rows):
            warnings.append(f"{label}: ambiguous/unmatched populated rows; no row addition/removal claims.")
    for left, right in pairs:
        for key in shared:
            a, b = left[old["keys"].index(key)], right[new["keys"].index(key)]
            if not a["complete"] or not b["complete"]:
                warnings.append(f"{label} {key}: incomplete OCR, duplicate overlapping words, "
                                "or crossing source box; cell unresolved.")
                continue
            if a["text"] == b["text"]:
                continue
            if old["kind"] == "bom" and (not _reliable_cell(a) or not _reliable_cell(b)):
                warnings.append(f"{label} {key}: low-confidence/illegible BOM cell text; unresolved.")
                continue
            if not a["text"] or not b["text"]:
                if old["kind"] == "bom" and (a["blank"] or b["blank"]):
                    records.append(_bom_cell_presence(old, new, key, a, b))
                    continue
                warnings.append(f"{label} {key}: observed blank/text transition; "
                                "not emitted as a text-to-text modification.")
                continue
            header = old["rows"][old["header"]][old["keys"].index(key)]["text"]
            records.append(_record(old, new, "table_cell_modified", header, [a], [b]))
    return records


def compare_document_tables(old_pdf, new_pdf, old_operation, new_operation, *, include_bom=False):
    """Compare bounded table evidence; return JSON-serializable web items.

    Header matching is location-independent and order-independent. At least
    three shared roles and >=60% overlap of the larger header set are required,
    with exactly one possible partner in both directions. No eligible source,
    ambiguous tables, incomplete cells, and unmatched rows yield warnings, not
    guesses. Unique single-column panels additionally require a complete, highly
    confident uppercase printed top-band header (2-12 words, no numeric values)
    and a non-header-like body. Their joined OCR text is compared independently
    of line segmentation; incomplete/low-confidence bodies are review-only,
    except uniquely anchored high-confidence text spans whose local source ink
    is independently accounted for. Such span records retain whole-panel issues.
    Source grid counts include header and independently verified blanks.
    No schema/ordinary OCR records are removed; callers must expose overlap.
    Opt-in BOM matching requires unique unchanged description/part-number
    evidence, never row ordinals alone. One-sided unmatched rows require complete
    source/ink/OCR coverage of both tables. Source-verified blank cells may yield
    table_cell_added/removed with a null missing side and separate blank context.
    No fields.Items, generated interpretations, or CU row counts are required.
    """
    warnings = []
    old_diagnostics, new_diagnostics = [], []
    old_tables = _extract(old_pdf, old_operation, "old", warnings, old_diagnostics, include_bom=include_bom)
    new_tables = _extract(new_pdf, new_operation, "new", warnings, new_diagnostics, include_bom=include_bom)
    edges, reverse = defaultdict(list), defaultdict(list)
    for i, old in enumerate(old_tables):
        for j, new in enumerate(new_tables):
            if old["kind"] != new["kind"]:
                continue
            common = set(old["keys"]) & set(new["keys"])
            panel_match = old["kind"] == "printed_header_panel" and old["keys"] == new["keys"]
            minimum = 2 if old["kind"] == "bom" else 3
            if panel_match or (len(common) >= minimum and len(common) / max(len(old["keys"]), len(new["keys"])) >= .6):
                edges[i].append(j)
                reverse[j].append(i)
    items, paired, panel_diagnostics = [], 0, []
    for i, old in enumerate(old_tables):
        targets = edges[i]
        if len(targets) != 1 or len(reverse[targets[0]]) != 1:
            warnings.append(f"old table page {old['page']}: missing or ambiguous header correspondence; no additions.")
            continue
        paired += 1
        items.extend(_compare_pair(old, new_tables[targets[0]], warnings, panel_diagnostics))
    if paired != len(new_tables):
        warnings.append("Some new tables lack unique old counterparts; no table addition claims.")
    for index, item in enumerate(items, 1):
        item["id"] = f"T{index:03d}"
    warnings.append("Bounded vector-grid/OCR coverage only; raster, partial/merged and unreadable "
                    "tables are not certified. Schema/OCR overlap is not coalesced.")
    return {"items": items, "coverage": {"old_eligible_tables": len(old_tables),
                                        "new_eligible_tables": len(new_tables),
                                        "paired_tables": paired,
                                        "unmatched_old_tables": len(old_tables) - paired,
                                        "unmatched_new_tables": len(new_tables) - paired,
                                        "comparison_status": "paired" if paired else "no_match",
                                        "panel_comparisons": panel_diagnostics,
                                        "old_source_grids": old_diagnostics,
                                        "new_source_grids": new_diagnostics,
                                        "candidate_count": len(items),
                                        "old_tables": [{"headers": table["keys"],
                                                        "grid": _measurement(table)}
                                                       for table in old_tables],
                                        "new_tables": [{"headers": table["keys"],
                                                        "grid": _measurement(table)}
                                                       for table in new_tables],
                                        "bom": {"enabled": include_bom,
                                                "old_included": sum(t["kind"] == "bom" for t in old_tables),
                                                "new_included": sum(t["kind"] == "bom" for t in new_tables),
                                                "old_excluded": sum(d["exclusion_reason"] == "bom_disabled"
                                                                    for d in old_diagnostics),
                                                "new_excluded": sum(d["exclusion_reason"] == "bom_disabled"
                                                                    for d in new_diagnostics),
                                                "source": "PDF vector grids and cached OCR; no generated Items"},
                                        "scope": ("all_complete_vector_grids" if include_bom
                                                  else "non_bom_complete_vector_grids"),
                                        "complete": False},
            "warnings": list(dict.fromkeys(warnings))}
