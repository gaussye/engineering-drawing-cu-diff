"""Lossless source parsing and narrowly scoped text normalization."""

from datetime import date
import math
import re
import unicodedata


_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_SOURCE = re.compile(r"D\(\s*(\d+)\s*((?:,\s*" + _NUMBER + r"\s*)+)\)")
_DATE = re.compile(r"(?<![\w./-])(\d{4})\s*([./-])\s*(\d{1,2})\s*\2\s*(\d{1,2})(?![\w./-])")


def normalize_text(value):
    """Normalize whitespace and NFC, never case, punctuation, or identifiers."""
    return " ".join(unicodedata.normalize("NFC", value).split())


def normalize_date_spacing(value):
    """Remove separator-adjacent spaces only in valid, unambiguous year-first dates."""
    def replace(match):
        year, separator, month, day = match.groups()
        try:
            date(int(year), int(month), int(day))
        except ValueError:
            return match.group()
        return separator.join((year, month, day))

    return _DATE.sub(replace, normalize_text(value))


def parse_source(source):
    """Parse CU D(page,x,y,width,height) or four-vertex sources without remapping."""
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
