"""Local graphical evidence with conservative uniform-scale registration. No cloud calls."""

from contextlib import nullcontext
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
import re

import pymupdf

from .evidence import parse_source


VERSION = "local-graphics-v5"
MAX_PIXELS = 8_000_000
MAX_BOXES = 24
LIMITATIONS = [
    "设计内容与可选视图平移/等比缩放分开；变换框表示整个视图，不是设计变化像素。",
    "本地图形结果是渲染外观差异候选，不是材料、尺寸数值或功能改变的结论。",
    "仅在可靠对应后消除平移与受限等比绘图缩放；不做旋转、非等比缩放、弹性形变或局部拉伸。",
    "绘图缩放不等于实物尺寸变化；原始尺寸、零件与BOM字段仍独立比较。",
    "文字掩膜仅用于区分含文字区域；OCR遗漏可能让文字变化进入图形通道。",
    "小于渲染分辨率或噪声阈值的细线/符号可能漏检；框内并非每个像素都改变。",
    "单侧残差可按已接受的配准映射到另一侧；映射定位框不是本侧实测残差，也不增加变化像素数。",
]


class GraphicsError(ValueError):
    pass


@dataclass
class Region:
    page: int
    box: tuple[float, float, float, float]
    kind: str
    anchors: str = ""


def _libraries():
    try:
        import cv2
        import numpy as np
    except ImportError as error:
        raise GraphicsError("缺少本地图形依赖，请安装项目的[graphics]或[web]依赖；未生成图形结果。") from error
    return cv2, np


def _pages(path, lock):
    with lock or nullcontext(), pymupdf.open(path) as pdf:
        if pdf.needs_pass or not 1 <= len(pdf) <= 20:
            raise GraphicsError("图形对比仅支持未加密、1至20页的PDF。")
        return [(page.rect.width, page.rect.height) for page in pdf]


def _content_pages(response):
    contents = response.get("result", {}).get("contents", []) if isinstance(response, dict) else []
    for content in contents:
        for page in content.get("pages", []):
            yield content, page


def _source_boxes(source, content, pages, sizes):
    by_number = {page.get("pageNumber"): page for page in pages}
    boxes = []
    for polygon in parse_source(source):
        number = polygon["page_number"]
        page = by_number.get(number, {})
        width, height = page.get("width"), page.get("height")
        if (not 1 <= number <= len(sizes) or not isinstance(width, (int, float))
                or not isinstance(height, (int, float)) or width <= 0 or height <= 0):
            continue
        pw, ph = sizes[number - 1]
        unit = page.get("unit", content.get("unit"))
        if (unit == "inch" and (abs(width * 72 - pw) > 1 or abs(height * 72 - ph) > 1)
                or unit == "pixel" and abs(width / height - pw / ph) > .005
                or unit not in ("inch", "pixel")):
            continue
        points = [(x / width * pw, y / height * ph) for x, y in polygon["points"]]
        x0, y0 = min(x for x, _ in points), min(y for _, y in points)
        x1, y1 = max(x for x, _ in points), max(y for _, y in points)
        if 0 <= x0 < x1 <= pw + .1 and 0 <= y0 < y1 <= ph + .1:
            boxes.append((number, (x0, y0, min(pw, x1), min(ph, y1))))
    return boxes


def _regions(response, sizes):
    found = {number: [] for number in range(1, len(sizes) + 1)}
    for content in response.get("result", {}).get("contents", []) if isinstance(response, dict) else []:
        paragraphs = content.get("paragraphs", [])
        for figure in content.get("figures", []):
            anchors = []
            for element in figure.get("elements", []):
                match = re.fullmatch(r"/paragraphs/(\d+)", element)
                if match and int(match[1]) < len(paragraphs):
                    anchors.append(paragraphs[int(match[1])].get("content", ""))
            for page, box in _source_boxes(figure.get("source"), content, content.get("pages", []), sizes):
                found[page].append(Region(page, box, "cu_figure", " ".join(anchors)))
    return found


def _iou(a, b):
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / union if union > 0 else 0


def _pair(old, new):
    scores = {}
    for i, left in enumerate(old):
        for j, right in enumerate(new):
            overlap = _iou(left.box, right.box)
            a = set(re.findall(r"[A-Za-z0-9]{3,}|[\u4e00-\u9fff]{2,}", left.anchors))
            b = set(re.findall(r"[A-Za-z0-9]{3,}|[\u4e00-\u9fff]{2,}", right.anchors))
            text = len(a & b) / len(a | b) if a | b else 0
            if overlap >= .2 or (text >= .7 and len(a & b) >= 3):
                scores[i, j] = .65 * overlap + .35 * text
    pairs = []
    for (i, j), score in scores.items():
        left = sorted((v, k) for (n, k), v in scores.items() if n == i)
        right = sorted((v, k) for (k, n), v in scores.items() if n == j)
        if (left[-1][1] == j and right[-1][1] == i
                and (len(left) == 1 or score - left[-2][0] >= .12)
                and (len(right) == 1 or score - right[-2][0] >= .12)):
            pairs.append((i, j, score))
    return pairs


def _render(path, number, scale, lock):
    _, np = _libraries()
    with lock or nullcontext(), pymupdf.open(path) as pdf:
        pixmap = pdf[number - 1].get_pixmap(
            matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csGRAY, alpha=False)
        return np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width).copy()


def _crop(image, region, scale):
    pad = 6 * scale if region.kind == "cu_figure" else 0
    x0 = max(0, int(region.box[0] * scale - pad))
    y0 = max(0, int(region.box[1] * scale - pad))
    x1 = min(image.shape[1], int(region.box[2] * scale + pad + 1))
    y1 = min(image.shape[0], int(region.box[3] * scale + pad + 1))
    return image[y0:y1, x0:x1], (x0, y0)


def _residual(a, b, dx, dy, radius):
    return _transform_residual(a, b, dx, dy, 1.0, radius)


def _transform_residual(a, b, dx, dy, factor, radius):
    """Return residuals in each ORIGINAL raster coordinate system, never resized evidence."""
    cv, np = _libraries()
    kernel = cv.getStructuringElement(cv.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
    moved_a = cv.warpAffine(a, np.float32([[factor, 0, dx], [0, factor, dy]]),
                           (a.shape[1], a.shape[0]), flags=cv.INTER_NEAREST)
    moved_b = cv.warpAffine(b, np.float32([[1/factor, 0, -dx/factor], [0, 1/factor, -dy/factor]]),
                           (a.shape[1], a.shape[0]), flags=cv.INTER_NEAREST)
    removed, added = a & (1 - cv.dilate(moved_b, kernel)), b & (1 - cv.dilate(moved_a, kernel))
    fraction = (int(removed.sum()) + int(added.sum())) / max(1, int(a.sum()) + int(b.sum()))
    return removed, added, fraction


def _outline(ink):
    cv, np = _libraries()
    contours, _ = cv.findContours(ink * 255, cv.RETR_LIST, cv.CHAIN_APPROX_SIMPLE)
    boxes = []
    for contour in contours:
        area = cv.contourArea(contour)
        if area < ink.size * .1:
            continue
        perimeter = cv.arcLength(contour, True)
        polygon = cv.approxPolyDP(contour, .012 * perimeter, True)
        x, y, w, h = cv.boundingRect(polygon)
        if (len(polygon) == 4 and cv.isContourConvex(polygon) and area / (w * h) >= .9
                and w >= ink.shape[1] * .35 and h >= ink.shape[0] * .15 and 1.2 <= w / h <= 6):
            boxes.append((x, y, x + w, y + h))
    # Leaders can touch an otherwise rectangular outline, making its contour
    # non-quadrilateral. Verify the four actual straight borders instead.
    horizontal = cv.morphologyEx(
        ink, cv.MORPH_OPEN, np.ones((1, max(8, ink.shape[1] // 4)), np.uint8))
    _, _, stats, _ = cv.connectedComponentsWithStats(horizontal, connectivity=8)
    lines = [tuple(map(int, stat[:4])) for stat in stats[1:] if stat[2] >= ink.shape[1] * .35]
    for n, (ax, ay, aw, ah) in enumerate(lines):
        for bx, by, bw, bh in lines[n + 1:]:
            x0, x1 = max(ax, bx), min(ax + aw, bx + bw)
            y0, y1 = sorted((ay + ah // 2, by + bh // 2))
            w, h = x1-x0, y1-y0
            if (h < ink.shape[0] * .15 or w * h < ink.size * .1
                    or not 1.2 <= w / h <= 6 or abs(ax-bx) > 5 or abs(aw-bw) > 5):
                continue
            supported = all(
                ink[y0:y1+1, max(0, x-2):min(ink.shape[1], x+3)].max(axis=1).mean() >= .995
                for x in (x0, x1-1))
            if supported:
                boxes.append((x0, y0, x1, y1+1))
    boxes.sort(key=lambda box: (box[2]-box[0]) * (box[3]-box[1]), reverse=True)
    distinct = []
    for box in boxes:
        if not any(_iou(box, existing) >= .7 for existing in distinct):
            distinct.append(box)
    if not distinct:
        return None
    if len(distinct) > 1:
        first, second = distinct[:2]
        if ((first[2]-first[0]) * (first[3]-first[1])
                < 1.5 * (second[2]-second[0]) * (second[3]-second[1])):
            return None
    return distinct[0]


def _left_lines(ink, box):
    """Long, near-solid interior vertical runs; excludes short barcode bars."""
    _, np = _libraries()
    x0, y0, x1, y1 = box
    y0, y1 = y0 + 3, y1 - 3
    start, end = x0 + 3, x0 + max(4, round((x1-x0) * .15))
    if y1 <= y0 or end <= start:
        return []
    columns = np.flatnonzero(ink[y0:y1, start:end].mean(axis=0) >= .85) + start
    groups = np.split(columns, np.flatnonzero(np.diff(columns) > 1) + 1)
    return [(int(group[0]), y0, int(group[-1])+1, y1) for group in groups if len(group)]


def _uniform_alignment(a, b, radius, bounds):
    """Fit one scale from complete bounds, then validate interior ink; never fit an affine."""
    from .subviews import _canonical, _ink_similarity

    old, new = bounds
    ratios = [(new[k+2]-new[k]) / (old[k+2]-old[k]) for k in (0, 1)]
    factor = sum(ratios) / 2
    quantization = min(.004, 2 / max(old[2]-old[0], old[3]-old[1]))
    if (not .65-quantization <= factor <= 1.5+quantization or abs(factor-1) <= .02
            or abs(ratios[0]/ratios[1]-1) > .015):
        return None
    canonical = []
    for image, box in ((a, old), (b, new)):
        x0, y0, x1, y1 = box
        # Excluding the border prevents a shared rectangle alone from validating
        # unrelated internal designs. Uniform resizing preserves aspect ratio.
        inset = max(2, round(min(x1-x0, y1-y0) * .04))
        canonical.append(_canonical(image[y0+inset:y1-inset, x0+inset:x1-inset]))
    similarity = _ink_similarity(*canonical)
    if similarity < .80:
        return None
    # Raster outline edges include stroke thickness and integer rounding. Refine
    # the single scalar within that quantization error, not with independent axes.
    center_old = [(old[k]+old[k+2])/2 for k in (0, 1)]
    center_new = [(new[k]+new[k+2])/2 for k in (0, 1)]
    best = None
    error = quantization
    for offset in (-error, -error/2, 0, error/2, error):
        candidate = max(.65, min(1.5, factor+offset))
        dx, dy = [center_new[k]-candidate*center_old[k] for k in (0, 1)]
        residual = _transform_residual(a, b, dx, dy, candidate, radius)
        if best is None or residual[2] < best[0][2]:
            best = residual, candidate, dx, dy
    residual, factor, dx, dy = best
    for ox in (-.5, 0, .5):
        for oy in (-.5, 0, .5):
            candidate = _transform_residual(a, b, dx+ox, dy+oy, factor, radius)
            if candidate[2] < best[0][2]:
                best = candidate, factor, dx+ox, dy+oy
    residual, factor, dx, dy = best
    if residual[2] > .30:
        return None
    return residual[:2], {
        "method": "verified_uniform_scale", "accepted": True,
        "response": None, "inlier_count": None, "dx_pixels": float(dx), "dy_pixels": float(dy),
        "scale_ratio": float(factor), "outline_width_ratio": ratios[0], "outline_height_ratio": ratios[1],
        "outline_old_pixels": list(old), "outline_new_pixels": list(new),
        "normalized_interior_similarity": round(similarity, 6),
        "unmatched_ink_fraction": residual[2],
        "note": "唯一对应后仅等比绘图缩放及平移；残差保留原页坐标。不是实物尺寸变化结论。",
    }


def _align(a, b, radius, *, verified_bounds=None):
    cv, np = _libraries()
    baseline = _residual(a, b, 0, 0, radius)
    if baseline[2] <= .001:
        return baseline[:2], {
            "method": "identity", "accepted": True, "response": 1 - baseline[2],
            "inlier_count": 0, "dx_pixels": 0.0, "dy_pixels": 0.0,
            "unmatched_ink_fraction": baseline[2], "note": "相同物理尺度，无形变或缩放对齐。",
        }
    candidates = [(0.0, 0.0, "identity", 1 - baseline[2], 0)]
    outline_a, outline_b = _outline(a), _outline(b)
    bounds = verified_bounds or ((outline_a, outline_b) if outline_a and outline_b else None)
    if bounds:
        uniform = _uniform_alignment(a, b, radius, bounds)
        if uniform:
            residual, metadata = uniform
            if outline_a and outline_b:
                metadata["left_lines_old_pixels"] = _left_lines(a, outline_a)
                metadata["left_lines_new_pixels"] = _left_lines(b, outline_b)
            return residual, metadata
    if outline_a and outline_b:
        wa, ha = outline_a[2] - outline_a[0], outline_a[3] - outline_a[1]
        wb, hb = outline_b[2] - outline_b[0], outline_b[3] - outline_b[1]
        if .6 <= wb / wa <= 1.6 and .6 <= hb / ha <= 1.6 and abs((wb / hb) / (wa / ha) - 1) <= .2:
            dx = (outline_b[0] + outline_b[2] - outline_a[0] - outline_a[2]) / 2
            dy = (outline_b[1] + outline_b[3] - outline_a[1] - outline_a[3]) / 2
            removed, added, fraction = _residual(a, b, dx, dy, radius)
            return (removed, added), {
                "method": "dominant_rectangular_outline_centers", "accepted": True,
                "response": None, "inlier_count": None, "dx_pixels": dx, "dy_pixels": dy,
                "unmatched_ink_fraction": fraction,
                "outline_old_pixels": list(outline_a), "outline_new_pixels": list(outline_b),
                "outline_width_ratio": wb / wa, "outline_height_ratio": hb / ha,
                "left_lines_old_pixels": _left_lines(a, outline_a),
                "left_lines_new_pixels": _left_lines(b, outline_b),
                "note": "仅以唯一大矩形轮廓中心对齐，未缩放；外观比例变化保留。绘图尺寸不等于实物尺寸。",
            }
    window = cv.createHanningWindow((a.shape[1], a.shape[0]), cv.CV_32F)
    shift, response = cv.phaseCorrelate(
        cv.GaussianBlur(a.astype(np.float32), (3, 3), .7),
        cv.GaussianBlur(b.astype(np.float32), (3, 3), .7), window)
    if np.isfinite(shift).all() and response >= .12:
        candidates.append((*shift, "phase_translation", float(response), 0))
    orb = cv.ORB_create(nfeatures=1600, edgeThreshold=10, fastThreshold=12)
    ka, da = orb.detectAndCompute((255 - a * 255).astype(np.uint8), None)
    kb, db = orb.detectAndCompute((255 - b * 255).astype(np.uint8), None)
    if da is not None and db is not None and len(db) >= 2:
        matches = [pair[0] for pair in cv.BFMatcher(cv.NORM_HAMMING).knnMatch(da, db, k=2)
                   if len(pair) == 2 and pair[0].distance < .7 * pair[1].distance]
        if len(matches) >= 5:
            displacements = np.array([
                np.subtract(kb[m.trainIdx].pt, ka[m.queryIdx].pt) for m in matches])
            bins, counts = np.unique(np.round(displacements / 3).astype(int), axis=0, return_counts=True)
            center = bins[int(counts.argmax())] * 3
            mask = np.linalg.norm(displacements - center, axis=1) <= 4
            if int(mask.sum()) >= 5:
                dx, dy = np.median(displacements[mask], axis=0)
                candidates.append((float(dx), float(dy), "orb_translation_consensus",
                                   float(mask.mean()), int(mask.sum())))
    best = (baseline[2], candidates[0], baseline)
    for candidate in candidates[1:]:
        dx, dy, _, _, _ = candidate
        if abs(dx) > a.shape[1] * .35 or abs(dy) > a.shape[0] * .35:
            continue
        residual = _residual(a, b, dx, dy, radius)
        if residual[2] < best[0] - .025:
            best = residual[2], candidate, residual
    fraction, candidate, residual = best
    dx, dy, method, response, inliers = candidate
    accepted = fraction <= .45 and (method != "identity" or baseline[2] <= .12)
    return residual[:2], {
        "method": method, "accepted": accepted, "response": response, "inlier_count": inliers,
        "dx_pixels": dx, "dy_pixels": dy, "unmatched_ink_fraction": fraction,
        "note": "平移分数是启发式，不是识别准确率；未估计缩放或形变。",
    }


def _location(box, page, size):
    pw, ph = size
    x0, y0, x1, y1 = box
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(pw, x1), min(ph, y1)
    return {"page": page, "x": x0 / pw, "y": y0 / ph,
            "width": (x1 - x0) / pw, "height": (y1 - y0) / ph,
            "polygon": [[x0 / pw, y0 / ph], [x1 / pw, y0 / ph],
                        [x1 / pw, y1 / ph], [x0 / pw, y1 / ph]]}


def _components(mask, origin, scale, region, size):
    cv, np = _libraries()
    minimum = max(8, int(scale * scale))
    count, labels, stats, _ = cv.connectedComponentsWithStats(mask, connectivity=8)
    keep = np.zeros(count, dtype=np.uint8)
    keep[1:] = stats[1:, cv.CC_STAT_AREA] >= minimum
    clean = keep[labels]
    radius = max(1, round(scale))
    merged = cv.morphologyEx(clean, cv.MORPH_CLOSE, np.ones((radius * 2 + 1,) * 2, np.uint8))
    count, labels, stats, _ = cv.connectedComponentsWithStats(merged, connectivity=8)
    boxes = []
    for label in range(1, count):
        x, y, w, h, _ = stats[label]
        selected = (labels[y:y+h, x:x+w] == label) & (clean[y:y+h, x:x+w] != 0)
        support = int(selected.sum())
        if support < minimum:
            continue
        ys, xs = np.nonzero(selected)
        box = ((origin[0] + x + xs.min()) / scale, (origin[1] + y + ys.min()) / scale,
               (origin[0] + x + xs.max() + 1) / scale, (origin[1] + y + ys.max() + 1) / scale)
        boxes.append((support, _location(box, region.page, size)))
    boxes.sort(key=lambda item: item[0], reverse=True)
    return [box for _, box in boxes[:MAX_BOXES]], int(clean.sum()), max(0, len(boxes) - MAX_BOXES)


def _text_mask(response, sizes, region, origin, shape, scale):
    _, np = _libraries()
    mask = np.zeros(shape, dtype=np.uint8)
    for content, page in _content_pages(response):
        if page.get("pageNumber") != region.page:
            continue
        for word in page.get("words", []):
            for number, box in _source_boxes(word.get("source"), content, [page], sizes):
                if number != region.page:
                    continue
                x0 = max(0, int(box[0] * scale - origin[0]) - 1)
                y0 = max(0, int(box[1] * scale - origin[1]) - 1)
                x1 = min(shape[1], int(box[2] * scale - origin[0]) + 2)
                y1 = min(shape[0], int(box[3] * scale - origin[1]) + 2)
                if x1 > x0 and y1 > y0:
                    mask[y0:y1, x0:x1] = 1
    return mask


def _side(region, size, locations, scale, origin=None):
    return {
        "raw_text": "本地PDF渲染证据（不是OCR原文）", "detail": "框为变化像素包围区域，非框内全部内容改变。",
        "confidence": None, "source": {"kind": "local_pdf_render", "page": region.page,
            "dpi": round(scale * 72, 3), "region_pt": list(region.box),
            "render_scale": scale, "page_size_pt": list(size),
            "crop_origin_pixels": list(origin) if origin is not None else None, "version": VERSION},
        "locations": locations, "context_locations": [_location(region.box, region.page, size)],
        "location_error": None if locations else "本侧没有达到阈值的变化像素框；对应定位与实测残差分开记录",
    }


def _link_counterparts(left, right, metadata):
    """Project one-sided residual boxes without changing observed evidence."""
    alignment = metadata.get("alignment", {})
    if not left or not right or alignment.get("accepted") is not True:
        return
    factor = alignment.get("scale_ratio", 1.0)
    offsets = (alignment.get("dx_pixels"), alignment.get("dy_pixels"))
    if not all(isinstance(v, (int, float)) and isfinite(v) for v in (factor, *offsets)) or factor <= 0:
        raise GraphicsError("已接受的图形配准缺少有效平移/缩放参数，无法生成对应定位框。")
    for source_role, source, target in (("old", left, right), ("new", right, left)):
        if target["locations"] or not source["locations"]:
            continue
        a, b = source["source"], target["source"]
        if a.get("crop_origin_pixels") is None or b.get("crop_origin_pixels") is None:
            target["counterpart_location_error"] = "缺少渲染裁切原点，不能映射对侧残差。"
            continue
        projected = []
        for index, location in enumerate(source["locations"]):
            points = ((location["x"], location["y"]),
                      (location["x"] + location["width"], location["y"] + location["height"]))
            mapped = []
            for point in points:
                for axis in (0, 1):
                    pixel = point[axis] * a["page_size_pt"][axis] * a["render_scale"] - a["crop_origin_pixels"][axis]
                    pixel = factor * pixel + offsets[axis] if source_role == "old" else (pixel - offsets[axis]) / factor
                    mapped.append((pixel + b["crop_origin_pixels"][axis]) / b["render_scale"])
            pw, ph = b["page_size_pt"]
            if min(pw, mapped[2]) <= max(0, mapped[0]) or min(ph, mapped[3]) <= max(0, mapped[1]):
                target["counterpart_location_error"] = "部分对侧残差映射到页面外，未绘制越界定位框。"
                continue
            projected.append({
                **_location(mapped, b["page"], b["page_size_pt"]),
                "evidence_role": "projected_counterpart", "label": "对应位置，非本侧残差",
                "from_side": source_role, "source_location_index": index,
            })
        target["counterpart_locations"] = projected
        target["counterpart_source"] = {
            "kind": "registered_residual_projection", "from_side": source_role,
            "alignment_method": alignment["method"], "scale_ratio": factor,
            "dx_pixels": offsets[0], "dy_pixels": offsets[1],
            "source_crop_origin_pixels": a["crop_origin_pixels"],
            "target_crop_origin_pixels": b["crop_origin_pixels"],
        }


def _transformations(regions, sizes, origins, scale, alignment, metadata, emit):
    """Optional full-view frames; these must never be mistaken for residual boxes."""
    if not alignment["accepted"]:
        return
    boxes = {role: region.box for role, region in regions.items()}
    dimensions = {role: [box[2]-box[0], box[3]-box[1]] for role, box in boxes.items()}
    factor = alignment.get("scale_ratio", 1.0)
    delta = {
        name: round((origins["new"][axis]-origins["old"][axis]
                     + alignment[f"{name}_pixels"]
                     + (factor-1)*((boxes["old"][axis]+boxes["old"][axis+2])*scale/2
                                   - origins["old"][axis]))/scale, 4)
        for axis, name in enumerate(("dx", "dy"))}
    ratios = {name: dimensions["new"][axis]/dimensions["old"][axis]
              for axis, name in enumerate(("width", "height"))}
    # With scaling, center displacement is the translation in a center-based
    # decomposition. An origin-anchored scale can therefore also move the view.
    changes = []
    if max(abs(value) for value in delta.values()) > 1:
        changes.append(("visual_moved", "视图平移（可选；非设计内容变化）"))
    if abs(factor-1) > .02:
        changes.append(("visual_scaled", "视图等比绘图缩放（可选；非实物尺寸变化）"))
    for change, label in changes:
        evidence = {}
        for role, region in regions.items():
            evidence[role] = _side(region, sizes[role],
                                   [_location(region.box, region.page, sizes[role])],
                                   scale, origins[role])
            evidence[role]["detail"] = "整个对应视图的变换定位框；不是变化像素框，也不是实物尺寸证据。"
            evidence[role]["evidence_role"] = "transformation_frame"
        emit(change, evidence["old"], evidence["new"], {
            **metadata, "classification": label, "alignment": alignment,
            "optional_transformation": True, "evidence_role": "transformation_frame",
            "translation_pt": delta, "translation_basis": "view_center_displacement",
            "scale_ratio": {"x": ratios["width"], "y": ratios["height"]},
            "scale_ratios": ratios,
            "old_region_pt": list(boxes["old"]), "new_region_pt": list(boxes["new"]),
            "limitations": LIMITATIONS,
        })


def _refine_subviews(ink, origins, scale, regions, sizes, masks, alignment, residuals, emit,
                     emit_transform=None):
    from .subviews import extract_subviews, order_reversals, pair_subviews

    _, np = _libraries()
    stats = dict.fromkeys(("old_candidates", "new_candidates", "matched", "unresolved",
                          "omitted", "resolved_residual_pairs"), 0)
    candidates, omitted = {}, {}
    for role in ink:
        candidates[role], omitted[role] = extract_subviews(ink[role], scale)
    if not any(candidates.values()):
        return stats, 0, 0
    for role in ink:
        stats[f"{role}_candidates"] = len(candidates[role]) + omitted[role]
    stats["omitted"] = sum(omitted.values())
    # Truncation must not hide an indistinguishable competitor and create a false match.
    pairs = [] if stats["omitted"] else pair_subviews(candidates["old"], candidates["new"])
    stats["matched"] = len(pairs)
    stats["unresolved"] = stats["old_candidates"] + stats["new_candidates"] - 2 * len(pairs)
    reversals = order_reversals(candidates["old"], candidates["new"], pairs)
    parent_factor = alignment.get("scale_ratio", 1.0)
    emitted, unshown = 0, 0
    for i, j, identity in pairs:
        views = {"old": candidates["old"][i], "new": candidates["new"][j]}
        boxes = {role: view.box for role, view in views.items()}
        absolute = {role: tuple((v + origins[role][k % 2]) / scale for k, v in enumerate(box))
                    for role, box in boxes.items()}
        dimensions = {role: [box[2]-box[0], box[3]-box[1]] for role, box in absolute.items()}
        delta = [(absolute["new"][axis] + absolute["new"][axis+2]
                  - absolute["old"][axis] - absolute["old"][axis+2]) / 2 for axis in (0, 1)]
        parent_motion = [
            (origins["new"][axis]-origins["old"][axis]
             + alignment[("dx_pixels", "dy_pixels")[axis]]
             + (parent_factor-1)*(boxes["old"][axis]+boxes["old"][axis+2])/2)/scale
            for axis in (0, 1)]
        moved = any(abs(delta[axis]) > 1 + abs(
            dimensions["new"][axis] - dimensions["old"][axis]) / 2 for axis in (0, 1))
        independent = moved and any(abs(delta[axis] - parent_motion[axis]) > 2 for axis in (0, 1))
        resized = any(abs(dimensions["new"][axis]/dimensions["old"][axis]-parent_factor) > .02
                      for axis in (0, 1))
        if alignment["accepted"] and not independent and not reversals[i] and not resized:
            continue
        shape = tuple(max(boxes[role][axis+2]-boxes[role][axis] for role in ink) for axis in (1, 0))
        child_ink, child_masks, child_origins, child_regions = {}, {}, {}, {}
        for role, box in boxes.items():
            x0, y0, x1, y1 = box
            child_ink[role] = np.zeros(shape, dtype=np.uint8)
            child_masks[role] = np.zeros(shape, dtype=np.uint8)
            child_ink[role][:y1-y0, :x1-x0] = ink[role][y0:y1, x0:x1]
            child_masks[role][:y1-y0, :x1-x0] = masks[role][y0:y1, x0:x1]
            child_origins[role] = (origins[role][0] + x0, origins[role][1] + y0)
            child_regions[role] = Region(regions[role].page, absolute[role], "local_subview")
        child_residuals, child_alignment = _align(
            child_ink["old"], child_ink["new"], max(1, int(np.ceil(scale * .3))),
            verified_bounds=tuple((0, 0, boxes[role][2]-boxes[role][0],
                                   boxes[role][3]-boxes[role][1]) for role in ink))
        subview = {"old_index": i+1, "new_index": j+1, "identity": identity,
                   "old_size_pt": dimensions["old"], "new_size_pt": dimensions["new"]}
        subview["drawing_size_changed"] = any(
            abs(dimensions["new"][axis] - dimensions["old"][axis]) > 1
            and abs(dimensions["new"][axis] / dimensions["old"][axis] - 1) > .02
            for axis in (0, 1))
        metadata = {
            "method": VERSION, "pairing_method": "mutual_subview_shape_and_ink",
            "region_source": "local_subview", "dpi": scale * 72,
            "old_region_pt": list(absolute["old"]), "new_region_pt": list(absolute["new"]),
            "alignment": child_alignment, "subview": subview,
            "limitations": LIMITATIONS + [
                "子图仅按封闭轮廓与内部墨迹建立候选对应，不识别零件种类或实物身份。",
                "指纹用于唯一配对；仅通过内墨迹验证的受限等比缩放可用于内容对齐。",
                "未可靠对齐的外观仍留在父区域待核，不因忽略位置而认定内容一致。",
                "非等比尺寸变化与不满足验证条件的缩放保留残差；不能据此认定实物尺寸改变。",
            ],
        }
        if not child_alignment["accepted"]:
            continue
        stats["resolved_residual_pairs"] += 1
        if emit_transform:
            _transformations(child_regions, sizes, child_origins, scale, child_alignment,
                             metadata, emit_transform)
        # Nearby labels can travel with a view without being inside its contour.
        # Suppress only agreeing pixels under the same accepted uniform transform.
        factor = child_alignment.get("scale_ratio", 1.0)
        neighborhood_ink = {role: image.copy() for role, image in ink.items()}
        for role in ink:
            for other in candidates[role]:
                if other is not views[role]:
                    x0, y0, x1, y1 = other.box
                    neighborhood_ink[role][y0:y1, x0:x1] = 0
        neighborhood = _transform_residual(
            neighborhood_ink["old"], neighborhood_ink["new"],
            boxes["new"][0] - factor*boxes["old"][0] + child_alignment["dx_pixels"],
            boxes["new"][1] - factor*boxes["old"][1] + child_alignment["dy_pixels"], factor,
            max(1, int(np.ceil(scale * .3))))
        for role, residual, local_residual in zip(ink, residuals, neighborhood[:2]):
            x0, y0, x1, y1 = boxes[role]
            pad = round(14 * scale * (factor if role == "new" else 1))
            left, top = max(0, x0-pad), max(0, y0-pad)
            right, bottom = min(residual.shape[1], x1+pad), min(residual.shape[0], y1+pad)
            allowed = np.ones((bottom-top, right-left), dtype=np.uint8)
            for other in candidates[role]:
                if other is views[role]:
                    continue
                ox0, oy0, ox1, oy1 = other.box
                ax, ay = max(left, ox0), max(top, oy0)
                bx, by = min(right, ox1), min(bottom, oy1)
                if ax < bx and ay < by:
                    allowed[ay-top:by-top, ax-left:bx-left] = 0
            residual[top:bottom, left:right] &= (
                local_residual[top:bottom, left:right] | (1 - allowed))
            residual[y0:y1, x0:x1] = 0
        for text, change, label in ((False, "visual_modified", "外观"),
                                   (True, "visual_annotation", "文字/标注")):
            evidence, pixels, fractions = {}, {}, {}
            for role, residual in zip(ink, child_residuals):
                mask = residual & (child_masks[role] if text else 1 - child_masks[role])
                locations, count, omitted_count = _components(
                    mask, child_origins[role], scale, child_regions[role], sizes[role])
                unshown += omitted_count
                evidence[role] = _side(child_regions[role], sizes[role], locations, scale, child_origins[role])
                pixels[role] = count
                fractions[role] = round(count / max(1, int(child_ink[role].sum())), 6)
            if max(pixels.values()) >= max(20, round(scale * scale * 2)):
                emit(change, evidence["old"], evidence["new"],
                     {**metadata, "classification": f"子图{i+1} → 子图{j+1}：独立对齐后{label}候选",
                      "changed_pixels": pixels, "residual_fraction": fractions}, identity["score"])
                emitted += 1
    return stats, emitted, unshown


def compare_graphics(old_path: Path, new_path: Path, old_response=None, new_response=None,
                     *, dpi=200, pdf_lock=None, progress=None, include_transformations=False):
    """Compare content; optionally append full-view transforms without changing residuals."""
    if not 96 <= dpi <= 300:
        raise GraphicsError("本地图形DPI须在96至300之间。")
    _, np = _libraries()
    sizes = {"old": _pages(old_path, pdf_lock), "new": _pages(new_path, pdf_lock)}
    responses = {"old": old_response or {}, "new": new_response or {}}
    regions = {role: _regions(responses[role], sizes[role]) for role in sizes}
    paths = {"old": old_path, "new": new_path}
    items, transformations = [], []
    coverage = {"status": "completed", "version": VERSION, "dpi_requested": dpi,
                "comparison_policy": "design_content_only",
                "ignored_changes": ["view_translation", "view_order", "uniform_drawing_scale"],
                "transformations_included": bool(include_transformations),
                "old_pages": len(sizes["old"]), "new_pages": len(sizes["new"]),
                "paired_regions": 0, "unchanged_regions": 0, "unpaired_regions": 0, "uncertain_regions": 0,
                "fallback_pages": 0, "unshown_components": 0, "warnings": [],
                "subview_matching": dict.fromkeys(
                    ("old_candidates", "new_candidates", "matched", "unresolved",
                     "omitted", "resolved_residual_pairs"), 0),
                "page_geometry_changes": [],
                "parameters": {"ink_threshold": 180, "pixel_tolerance_pt": .3,
                               "min_component_area_pt2": 1, "max_render_pixels": MAX_PIXELS,
                               "uniform_scale": {"min_ratio": .65, "max_ratio": 1.5,
                                                 "min_change": .02, "max_axis_ratio_error": .015,
                                                 "min_interior_similarity": .80,
                                                 "max_unmatched_ink_fraction": .30,
                                                 "max_raster_scale_refinement": .004,
                                                 "max_raster_offset_refinement_pixels": .5},
                               "subviews": {"min_edge_pt": 14, "max_candidates_per_region": 24,
                                            "min_identity_score": .80, "min_identity_margin": .06,
                                            "min_center_displacement_pt": 1}},
                "limits": LIMITATIONS + [
                    f"每侧每图形类别最多显示{MAX_BOXES}个像素区域，超出数量明确记录。",
                    "按物理页序比较；页重排需复核。CU未标出的图形可能未被覆盖。",
                    "子图仅在已配对父区域内跨位置匹配；开口/相连轮廓、重复视图可能无法唯一拆分或对应。",
                    "子图旧侧外14pt上下文及新侧等比映射范围仅消除按同一变换可对应的墨迹；其他候选不受影响。",
                    "缩放仅支持0.65至1.5倍且横纵比例偏差不超过1.5%；超过范围保留残差/待核。",
                    "变换诊断为启发式，不是概率准确率；重复或未可靠对应的子图不输出变换。",
                ]}

    def emit_transform(*args):
        transformations.append(args)

    def emit(change, left, right, metadata, score=0):
        if change in ("visual_modified", "visual_annotation"):
            _link_counterparts(left, right, metadata)
        items.append({
            "id": f"G{len(items)+1:03d}", "channel": "graphics", "change": change,
            "region": "本地图形", "key": metadata["classification"], "review_required": True,
            "review_reasons": metadata.get("limitations", []) + ["图形外观候选需按原图复核。"],
            "match": {"method": metadata.get("pairing_method", "mutual_region_overlap_and_literal_anchors"),
                      "score": round(score, 4), "certainty": "uncertain"},
            "old": left, "new": right, "graphics": metadata,
        })

    for page_number in range(1, max(map(len, sizes.values())) + 1):
        if progress:
            progress(f"本地图形对比：第{page_number}页（不调用Azure）")
        active = [role for role in sizes if page_number <= len(sizes[role])]
        if len(active) == 2 and sizes["old"][page_number-1] != sizes["new"][page_number-1]:
            coverage["page_geometry_changes"].append({
                "page": page_number, "old_pt": sizes["old"][page_number-1],
                "new_pt": sizes["new"][page_number-1]})
            coverage["warnings"].append(f"第{page_number}页尺寸不同；保持相同物理渲染尺度，不拉伸对齐。")
        area = max(w * h for role in active for w, h in [sizes[role][page_number - 1]])
        scale = min(dpi / 72, (MAX_PIXELS / area) ** .5)
        page_regions = {role: regions[role].get(page_number, []) for role in sizes}
        if any(not page_regions[role] or len(page_regions[role]) > 32 for role in active):
            coverage["fallback_pages"] += 1
            for role in active:
                w, h = sizes[role][page_number - 1]
                page_regions[role] = [Region(page_number, (0, 0, w, h), "page_fallback")]
        pairs = _pair(page_regions["old"], page_regions["new"])
        for role, index in (("old", 0), ("new", 1)):
            for ri, region in enumerate(page_regions[role]):
                if any(pair[index] == ri for pair in pairs):
                    continue
                coverage["unpaired_regions"] += 1
                evidence = _side(region, sizes[role][page_number - 1],
                                 [_location(region.box, page_number, sizes[role][page_number - 1])], scale)
                emit(f"unpaired_{role}", evidence if role == "old" else None,
                     evidence if role == "new" else None,
                     {"classification": "图形区域未配对（非确认增删）", "method": VERSION,
                      "region_source": region.kind, "dpi": scale * 72, "limitations": LIMITATIONS})
        if not pairs:
            continue
        images = {role: _render(paths[role], page_number, scale, pdf_lock) for role in active}
        for i, j, score in pairs:
            coverage["paired_regions"] += 1
            pair = {"old": page_regions["old"][i], "new": page_regions["new"][j]}
            crops = {role: _crop(images[role], pair[role], scale) for role in sizes}
            shape = tuple(max(crops[role][0].shape[axis] for role in sizes) for axis in (0, 1))
            if min(shape) < 8 or shape[0] * shape[1] > MAX_PIXELS * 2:
                coverage["warnings"].append(f"第{page_number}页区域尺寸不适合像素比较；仅保留待复核区域。")
                emit("visual_uncertain",
                     _side(pair["old"], sizes["old"][page_number-1], [], scale),
                     _side(pair["new"], sizes["new"][page_number-1], [], scale),
                     {"classification": "图形区域无法可靠比较", "method": VERSION,
                      "dpi": scale*72, "limitations": coverage["warnings"][-1:]}, score)
                continue
            ink = {}
            for role in sizes:
                ink[role] = np.zeros(shape, dtype=np.uint8)
                crop = crops[role][0]
                ink[role][:crop.shape[0], :crop.shape[1]] = crop < 180
            if not any(ink[role].any() for role in sizes):
                coverage["unchanged_regions"] += 1
                continue
            residuals, alignment = _align(ink["old"], ink["new"], max(1, int(np.ceil(scale * .3))))
            origins = {role: crops[role][1] for role in sizes}
            translation = {
                "dx": round((origins["new"][0] - origins["old"][0] + alignment["dx_pixels"]) / scale, 4),
                "dy": round((origins["new"][1] - origins["old"][1] + alignment["dy_pixels"]) / scale, 4),
            }
            masks = {role: _text_mask(responses[role], sizes[role], pair[role],
                                     origins[role], shape, scale) for role in sizes}
            subview_stats, child_emitted, child_omitted = _refine_subviews(
                ink, origins, scale, pair, {role: sizes[role][page_number-1] for role in sizes},
                masks, alignment, residuals, emit, emit_transform)
            parent_identity = (not subview_stats["unresolved"]
                               or alignment["method"] == "verified_uniform_scale")
            if (alignment["accepted"] and parent_identity
                    and not subview_stats["resolved_residual_pairs"]):
                frames = {}
                for role in sizes:
                    box = alignment.get(f"outline_{role}_pixels")
                    if box is None:
                        ys, xs = np.nonzero(ink[role])
                        if not len(xs):
                            break
                        box = (int(xs.min()), int(ys.min()), int(xs.max())+1, int(ys.max())+1)
                    bounds = tuple((value+origins[role][axis % 2])/scale
                                   for axis, value in enumerate(box))
                    frames[role] = Region(page_number, bounds, pair[role].kind)
                if len(frames) == 2:
                    _transformations(
                        frames, {role: sizes[role][page_number-1] for role in sizes},
                        origins, scale, alignment,
                        {"method": VERSION, "region_source": pair["old"].kind, "dpi": scale*72},
                        emit_transform)
            for key, value in subview_stats.items():
                coverage["subview_matching"][key] += value
            coverage["unshown_components"] += child_omitted
            emitted = child_emitted > 0
            parent_emitted = False
            line_runs = {role: alignment.get(f"left_lines_{role}_pixels", []) for role in sizes}
            if alignment.get("outline_old_pixels") and len(line_runs["old"]) != len(line_runs["new"]):
                outlines = {role: alignment[f"outline_{role}_pixels"] for role in sizes}
                positions = {role: [((box[0]+box[2])/2 - outlines[role][0]) /
                                   (outlines[role][2]-outlines[role][0]) for box in line_runs[role]]
                             for role in sizes}
                matched = set()
                for oi, old_x in enumerate(positions["old"]):
                    choices = [(abs(old_x-new_x), ni) for ni, new_x in enumerate(positions["new"])]
                    if choices and min(choices)[0] <= .025:
                        ni = min(choices)[1]
                        reverse = [(abs(positions["new"][ni]-x), k) for k, x in enumerate(positions["old"])]
                        if min(reverse)[1] == oi:
                            matched.add((oi, ni))
                evidence = {}
                for role, index in (("old", 0), ("new", 1)):
                    locations = []
                    for ri, box in enumerate(line_runs[role]):
                        if any(match[index] == ri for match in matched):
                            continue
                        points = tuple((box[k] + origins[role][k % 2]) / scale for k in range(4))
                        locations.append(_location(points, page_number, sizes[role][page_number-1]))
                    evidence[role] = _side(pair[role], sizes[role][page_number-1], locations, scale, origins[role])
                emit("visual_modified", evidence["old"], evidence["new"], {
                    "classification": f"矩形左缘长竖线数量候选：{len(line_runs['old'])} → {len(line_runs['new'])}",
                    "method": "rectangular_interior_vertical_runs", "dpi": scale*72,
                    "alignment": alignment, "translation_pt": translation,
                    "region_source": pair["old"].kind, "old_region_pt": list(pair["old"].box),
                    "new_region_pt": list(pair["new"].box),
                    "limitations": LIMITATIONS + ["线条数量是渲染特征，不自动解释为封口工艺或实物结构。"],
                }, score)
                emitted = True
                parent_emitted = True
            for channel, change, label in (
                (False, "visual_modified", "图形/线条外观变化候选"),
                (True, "visual_annotation", "文字/标注外观残差候选"),
            ):
                side_values, pixels, fractions = {}, {}, {}
                for role, residual in zip(sizes, residuals):
                    mask = residual & (masks[role] if channel else 1 - masks[role])
                    boxes, count, omitted = _components(mask, origins[role], scale,
                                                      pair[role], sizes[role][page_number-1])
                    coverage["unshown_components"] += omitted
                    side_values[role] = _side(pair[role], sizes[role][page_number-1], boxes, scale, origins[role])
                    pixels[role], fractions[role] = count, round(count / max(1, int(ink[role].sum())), 6)
                if max(pixels.values()) < max(20, round(scale * scale * 2)):
                    continue
                emitted = True
                parent_emitted = True
                actual_change = change if alignment["accepted"] else "visual_uncertain"
                emit(actual_change, side_values["old"], side_values["new"], {
                    "classification": label if alignment["accepted"] else "图形像素差异待核（未可靠对齐）",
                    "method": VERSION, "dpi": round(scale * 72, 3), "alignment": alignment,
                    "translation_pt": translation, "changed_pixels": pixels, "residual_fraction": fractions,
                    "region_source": pair["old"].kind, "old_region_pt": list(pair["old"].box),
                    "new_region_pt": list(pair["new"].box), "limitations": LIMITATIONS,
                }, score)
            if not emitted:
                coverage["unchanged_regions"] += 1
            if parent_emitted and not alignment["accepted"]:
                coverage["uncertain_regions"] += 1
    if coverage["subview_matching"]["unresolved"]:
        coverage["warnings"].append(
            f"{coverage['subview_matching']['unresolved']}个子图候选未建立唯一对应；"
            "保留父区域像素复核，不将未配对解释为增删。")
    if coverage["subview_matching"]["omitted"]:
        coverage["warnings"].append("某父区封闭子图超过24个，跳过该区子图配对，避免截断造成假唯一匹配。")
    if coverage["unshown_components"]:
        coverage["warnings"].append(f"{coverage['unshown_components']}个小区域未绘制，不能据此认定无变化。")
    if coverage["uncertain_regions"]:
        coverage["warnings"].append(
            f"{coverage['uncertain_regions']}个图形区域未可靠对齐；相关像素差异仅待核，不确认部件改变。")
    coverage["design_content_items"] = len(items)
    coverage["transformation_candidates"] = {
        change: sum(args[0] == change for args in transformations)
        for change in ("visual_moved", "visual_scaled")}
    if include_transformations:
        for args in transformations:
            emit(*args)
    return {"items": items, "coverage": coverage}
