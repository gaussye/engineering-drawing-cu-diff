"""Position-independent correspondence of bounded drawing views, not part semantics."""

from dataclasses import dataclass

import cv2 as cv
import numpy as np


MAX_SUBVIEWS = 24
MIN_SCORE = .80
MIN_MARGIN = .06


@dataclass
class Subview:
    box: tuple[int, int, int, int]
    ink: np.ndarray
    silhouette: np.ndarray
    interior: np.ndarray


def _canonical(image):
    height, width = image.shape
    scale = 112 / max(height, width)
    w, h = max(1, round(width * scale)), max(1, round(height * scale))
    resized = cv.resize(image.astype(np.float32), (w, h), interpolation=cv.INTER_AREA)
    canvas = np.zeros((128, 128), dtype=np.uint8)
    x, y = (128 - w) // 2, (128 - h) // 2
    canvas[y:y+h, x:x+w] = resized >= .15
    return canvas


def extract_subviews(ink, scale):
    """Close subpixel gaps for segmentation only; descriptors use original ink."""
    radius = max(1, round(scale * .25))
    closed = cv.morphologyEx(ink, cv.MORPH_CLOSE,
                            np.ones((2 * radius + 1,) * 2, np.uint8))
    contours, hierarchy = cv.findContours(closed, cv.RETR_TREE, cv.CHAIN_APPROX_SIMPLE)
    eligible = {}
    for index, contour in enumerate(contours):
        x, y, w, h = cv.boundingRect(contour)
        if (min(w, h) < 14 * scale or w * h > ink.size * .6
                or cv.contourArea(contour) / (w * h) < .25):
            continue
        # A clipped outline does not establish a complete independent view.
        if x == 0 or y == 0 or x + w == ink.shape[1] or y + h == ink.shape[0]:
            continue
        eligible[index] = (w * h, (x, y, x+w, y+h), contour)
    candidates = []
    for index, candidate in eligible.items():
        ancestor = hierarchy[0, index, 3]
        while ancestor >= 0 and ancestor not in eligible:
            ancestor = hierarchy[0, ancestor, 3]
        if ancestor < 0:
            candidates.append(candidate)
    candidates.sort(key=lambda item: (-item[0], item[1]))
    omitted = max(0, len(candidates) - MAX_SUBVIEWS)
    views = []
    for _, box, contour in sorted(candidates[:MAX_SUBVIEWS], key=lambda item: (item[1][1], item[1][0])):
        x0, y0, x1, y1 = box
        silhouette = np.zeros((y1-y0, x1-x0), dtype=np.uint8)
        cv.drawContours(silhouette, [contour - np.array([[[x0, y0]]])], -1, 1, cv.FILLED)
        silhouette = _canonical(silhouette)
        pixels = _canonical(ink[y0:y1, x0:x1])
        interior = pixels & cv.erode(silhouette, np.ones((7, 7), np.uint8))
        views.append(Subview(box, pixels, silhouette, interior))
    return views, omitted


def _ink_similarity(a, b):
    if not a.any() or not b.any():
        return float(not a.any() and not b.any())
    da = cv.distanceTransform(1 - a, cv.DIST_L2, 3)
    db = cv.distanceTransform(1 - b, cv.DIST_L2, 3)
    return 1 - float((np.minimum(da[b != 0] / 3, 1).mean()
                      + np.minimum(db[a != 0] / 3, 1).mean()) / 2)


def similarity(a, b):
    aw, ah = a.box[2] - a.box[0], a.box[3] - a.box[1]
    bw, bh = b.box[2] - b.box[0], b.box[3] - b.box[1]
    if (not .65 <= bw / aw <= 1.5 or not .65 <= bh / ah <= 1.5
            or abs((bw / bh) / (aw / ah) - 1) > .25):
        return 0.0
    overlap = float((a.silhouette & b.silhouette).sum()) / max(
        1, int((a.silhouette | b.silhouette).sum()))
    if min(int(a.interior.sum()), int(b.interior.sum())) >= 20:
        return .25 * overlap + .75 * _ink_similarity(a.interior, b.interior)
    return .5 * overlap + .5 * _ink_similarity(a.ink, b.ink)


def pair_subviews(old, new):
    """Require mutual unique best appearance matches; never break ties by position."""
    scores = {(i, j): similarity(left, right)
              for i, left in enumerate(old) for j, right in enumerate(new)}
    pairs = []
    for (i, j), score in scores.items():
        old_alternatives = [v for (oi, nj), v in scores.items() if oi == i and nj != j]
        new_alternatives = [v for (oi, nj), v in scores.items() if nj == j and oi != i]
        old_margin = score - max(old_alternatives, default=0)
        new_margin = score - max(new_alternatives, default=0)
        if score >= MIN_SCORE and min(old_margin, new_margin) >= MIN_MARGIN:
            pairs.append((i, j, {"score": round(score, 6),
                                 "old_margin": round(old_margin, 6),
                                 "new_margin": round(new_margin, 6)}))
    return pairs


def order_reversals(old, new, pairs):
    """Report only separated views reversing order while sharing a row/column."""
    result = {i: [] for i, _, _ in pairs}
    for pos, (i, j, _) in enumerate(pairs):
        for oi, nj, _ in pairs[pos+1:]:
            for axis, name in ((0, "horizontal"), (1, "vertical")):
                other = 1 - axis
                a, b, c, d = old[i].box, old[oi].box, new[j].box, new[nj].box
                forward = a[axis+2] <= b[axis] and d[axis+2] <= c[axis]
                backward = b[axis+2] <= a[axis] and c[axis+2] <= d[axis]
                overlaps = all(
                    min(left[other+2], right[other+2]) - max(left[other], right[other])
                    >= .5 * min(left[other+2]-left[other], right[other+2]-right[other])
                    for left, right in ((a, b), (c, d)))
                if (forward or backward) and overlaps:
                    result[i].append({"axis": name, "old_index": oi+1, "new_index": nj+1})
                    result[oi].append({"axis": name, "old_index": i+1, "new_index": j+1})
    return result
