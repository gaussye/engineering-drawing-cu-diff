"""Offline, bounded rendering of an already captured comparison snapshot.

Only ``documents`` supplies filesystem paths. Snapshot strings are drawn as text,
never interpreted as HTML, SVG, links, or resource locations. Coordinates describe
the entire displayed page, independently of the browser's zoom / scroll viewport.
"""

from contextlib import ExitStack
import json
import math
from pathlib import Path

import pymupdf as fitz


MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_ITEMS = 300
MAX_PAGES = 300
MAX_SOURCE_PAGES = 10000
MAX_PRIMITIVES = 10000
MAX_TEXT_CHARS = 1_000_000
MAX_RASTER_PIXELS = 20_000_000
INK = (0.12, 0.18, 0.23)
MUTED = (0.38, 0.44, 0.49)
RULE = (0.80, 0.84, 0.86)
ACCENT = (0.12, 0.37, 0.40)


class PdfExportError(ValueError):
    """A snapshot cannot safely be represented as a complete PDF."""


def _fail(message):
    raise PdfExportError(f"PDF 导出失败：{message}")


def _object(value, keys, where):
    if not isinstance(value, dict) or set(value) != set(keys.split()):
        _fail(f"{where} 字段不完整或含不支持的字段。")


def _string(value, where, limit=MAX_TEXT_CHARS, nonempty=False):
    if not isinstance(value, str) or len(value) > limit or (nonempty and not value):
        _fail(f"{where} 必须是有效文本，且不能超过 {limit} 字符。")
    if any((ord(c) < 32 and c not in "\n\r\t") or 0xD800 <= ord(c) <= 0xDFFF
           for c in value):
        _fail(f"{where} 含不支持的控制字符。")
    return value


def _number(value, where, low=0, high=1000, positive=False):
    if (type(value) not in (int, float) or not low <= value <= high
            or not math.isfinite(value) or (positive and value == 0)):
        _fail(f"{where} 必须是 {low} 至 {high} 范围内的有限数值。")
    return value


def _integer(value, where, low=0, high=MAX_SOURCE_PAGES):
    if type(value) is not int or not low <= value <= high:
        _fail(f"{where} 必须是 {low} 至 {high} 范围内的整数。")
    return value


def _array(value, where, limit):
    if not isinstance(value, list) or len(value) > limit:
        _fail(f"{where} 必须是列表，且不能超过 {limit} 项。")
    return value


def _color(value, where):
    if not isinstance(value, list) or len(value) != 3:
        _fail(f"{where} 必须是三个 RGB 数值。")
    for component in value:
        _number(component, where, high=1)


def _validate(snapshot, documents, result):
    try:
        encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        _fail("快照必须是有限、有效的 JSON 数据。")
    if len(encoded) > MAX_PAYLOAD_BYTES:
        _fail(f"快照超过 {MAX_PAYLOAD_BYTES // 1024 // 1024} MB 限制，请缩小筛选范围。")
    keys = "revision job_id filters panes items"
    if isinstance(snapshot, dict) and "detail_mode" in snapshot:
        keys += " detail_mode"
    _object(snapshot, keys, "快照")
    if snapshot.get("detail_mode", "full") not in ("full", "concise"):
        _fail("不支持的详情模式。")
    _integer(snapshot["revision"], "版本", high=2**53 - 1)
    _string(snapshot["job_id"], "任务编号", 256, True)
    filters = snapshot["filters"]
    _object(filters, "channel review interpretation translation scaling", "筛选条件")
    for name in ("channel", "review"):
        _string(filters[name], "筛选标签", 500, True)
    for name in ("interpretation", "translation", "scaling"):
        if type(filters[name]) is not bool:
            _fail("筛选开关必须是布尔值。")
    _object(snapshot["panes"], "old new", "图纸面板")
    items = _array(snapshot["items"], "候选项", MAX_ITEMS)
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        _fail("存储的分析结果无有效候选项列表。")
    known = {item.get("id") for item in result["items"]
             if isinstance(item, dict) and isinstance(item.get("id"), str)}
    selected = set()
    for item in items:
        _object(item, "id title meta blocks", "候选项")
        identifier = _string(item["id"], "候选编号", 256, True)
        if any(c in identifier for c in "\r\n\t"):
            _fail("候选编号不能包含换行或制表符。")
        if identifier in selected or identifier not in known:
            _fail("候选编号重复或不属于当前分析结果。")
        selected.add(identifier)
        _string(item["title"], "候选标题", 10000)
        _string(item["meta"], "候选元数据", 10000)
        for block in _array(item["blocks"], "详情块", 2000):
            if not isinstance(block, dict):
                _fail("详情块格式无效。")
            if block.get("kind") in ("heading", "paragraph", "code"):
                _object(block, "kind text", "文本详情块")
                _string(block["text"], "详情文本")
            elif block.get("kind") == "table":
                _object(block, "kind rows", "表格详情块")
                for row in _array(block["rows"], "表格行", 10000):
                    if not _array(row, "表格列", 20):
                        _fail("表格行不能没有单元格。")
                    for cell in row:
                        _string(cell, "表格单元格")
            else:
                _fail("不支持的详情块类型。")
    primitive_count = 0
    for side in ("old", "new"):
        pane = snapshot["panes"][side]
        _object(pane, "document_id page width height rects labels", "图纸面板")
        doc = documents.get(side) if isinstance(documents, dict) else None
        if not isinstance(doc, dict) or not isinstance(doc.get("path"), Path):
            _fail("服务器图纸资料无效。")
        if pane["document_id"] != doc.get("id") or not isinstance(pane["document_id"], str):
            _fail("图纸编号不匹配，请刷新后重新导出。")
        _string(doc.get("name"), "文件名", 10000, True)
        _string(doc.get("sha256"), "文件哈希", 128, True)
        count = _integer(doc.get("page_count"), "图纸页数", 1)
        _integer(pane["page"], "选定页码", 1, count)
        for key in ("width", "height"):
            _number(pane[key], "显示尺寸", high=100000, positive=True)
        rects = _array(pane["rects"], "标记框", MAX_PRIMITIVES)
        labels = _array(pane["labels"], "标记标签", MAX_PRIMITIVES)
        primitive_count += len(rects) + len(labels)
        for primitive in rects + labels:
            if not isinstance(primitive, dict) or not isinstance(primitive.get("id"), str):
                _fail("标记编号无效。")
            if primitive["id"] not in selected:
                _fail("标记编号不属于当前筛选候选项。")
        for rect in rects:
            _object(rect, "id x y width height kind stroke fill", "标记框")
            if rect["kind"] not in ("change", "review", "counterpart"):
                _fail("标记框类型无效。")
            for key in ("x", "y", "width", "height"):
                _number(rect[key], "标记框坐标", positive=key in ("width", "height"))
            if rect["x"] + rect["width"] > 1000 or rect["y"] + rect["height"] > 1000:
                _fail("标记框超出完整图纸坐标范围。")
            _object(rect["stroke"], "color opacity width dash", "边线样式")
            _object(rect["fill"], "color opacity", "填充样式")
            for style in (rect["stroke"], rect["fill"]):
                _color(style["color"], "标记颜色")
                _number(style["opacity"], "标记透明度", high=1)
            _number(rect["stroke"]["width"], "边线宽度", high=100)
            dash = _array(rect["stroke"]["dash"], "虚线样式", 16)
            for length in dash:
                _number(length, "虚线长度", high=1000)
            if dash and not any(dash):
                _fail("虚线长度不能全部为零。")
        for label in labels:
            _object(label, "id x y font_size color" + (" text" if "text" in label else ""), "标记标签")
            if "text" in label:
                _string(label["text"], "标记文字", 300, True)
                if label["text"] not in [label["id"] + suffix for suffix in ("", " 待核", " 对应", " 视图范围")]:
                    _fail("标记文字与候选编号或显示状态不一致。")
            _number(label["x"], "标签横坐标")
            _number(label["y"], "标签纵坐标")
            _number(label["font_size"], "标签字号", high=200, positive=True)
            _color(label["color"], "标签颜色")
    if primitive_count > MAX_PRIMITIVES:
        _fail(f"标记总数超过 {MAX_PRIMITIVES}，请缩小导出范围。")
    if sum(len(s) for s in _strings(snapshot)) > MAX_TEXT_CHARS:
        _fail(f"文本超过 {MAX_TEXT_CHARS} 字符，请缩小导出范围。")


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


class _Typesetter:
    def __init__(self, pdf):
        self.pdf = pdf
        self.font = fitz.Font("cjk")  # MuPDF's bundled, offline Droid Sans Fallback.
        self.widths = {}

    def text(self, page, x, y, text, size=10, color=INK):
        if not text:
            return
        page.insert_font(fontname="ExportCJK", fontbuffer=self.font.buffer)
        page.insert_text((x, y), text, fontsize=size, fontname="ExportCJK", color=color)

    def wrap(self, text, width, size):
        # Character wrapping also handles Chinese and arbitrarily long identifiers.
        lines = []
        for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4).split("\n"):
            line, used = [], 0
            for char in paragraph:
                if char not in self.widths:
                    if not self.font.has_glyph(ord(char)):
                        _fail(f"离线字体不支持字符 U+{ord(char):04X}，无法完整导出。")
                    self.widths[char] = self.font.text_length(char, fontsize=1)
                advance = self.widths[char] * size
                if advance > width:
                    _fail("文本列过窄，无法完整排版。")
                if line and used + advance > width:
                    lines.append("".join(line))
                    line, used = [], 0
                line.append(char)
                used += advance
            lines.append("".join(line))
        return lines

    def new_page(self, width=595.276, height=841.89):
        if len(self.pdf) >= MAX_PAGES:
            _fail(f"报告超过 {MAX_PAGES} 页，请缩小筛选范围；未生成不完整报告。")
        return self.pdf.new_page(width=width, height=height)


def _image_rect(page_rect, panel):
    scale = min(panel.width / page_rect.width, panel.height / page_rect.height)
    width, height = page_rect.width * scale, page_rect.height * scale
    x = panel.x0 + (panel.width - width) / 2
    y = panel.y0 + (panel.height - height) / 2
    return fitz.Rect(x, y, x + width, y + height)


def _source_image(page, source, index, image):
    original = source[index]
    if original.first_annot is not None or original.first_widget is not None or original.rotation:
        # show_pdf_page excludes annotations / widgets. Render their appearances.
        scale = 2.5
        if original.rect.width * original.rect.height * scale**2 > MAX_RASTER_PIXELS:
            _fail("含批注或表单的图纸过大，超出安全渲染像素限制。")
        pixmap = original.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, annots=True)
        page.insert_image(image, pixmap=pixmap)
    elif original.get_contents():
        page.show_pdf_page(image, source, index, keep_proportion=True)
    # A genuinely blank page has no content stream; leave its panel white.


def _primitives(page, pane, image, typesetter):
    sx, sy = image.width / 1000, image.height / 1000
    css_scale = image.width / pane["width"]
    for rect in pane["rects"]:
        box = fitz.Rect(image.x0 + rect["x"] * sx, image.y0 + rect["y"] * sy,
                        image.x0 + (rect["x"] + rect["width"]) * sx,
                        image.y0 + (rect["y"] + rect["height"]) * sy)
        stroke, fill = rect["stroke"], rect["fill"]
        # PDF width zero means hairline, whereas SVG width zero means no stroke.
        color = stroke["color"] if stroke["width"] > 0 else None
        dash = "[" + " ".join(f"{n * css_scale:.8f}" for n in stroke["dash"]) + "] 0"
        page.draw_rect(box, color=color, fill=fill["color"],
                       width=stroke["width"] * css_scale,
                       dashes=dash if stroke["dash"] else None,
                       stroke_opacity=stroke["opacity"], fill_opacity=fill["opacity"])
    for label in pane["labels"]:
        typesetter.text(page, image.x0 + label["x"] * sx, image.y0 + label["y"] * sy,
                       label.get("text", label["id"]), label["font_size"] * sy, label["color"])


class _Report:
    def __init__(self, typesetter):
        self.t = typesetter
        self.page = None
        self.y = 0
        self.identifier = None
        self.toc = [[1, "对比总览", 1]]

    def start(self, identifier=None):
        self.identifier = identifier
        self._next()
        if identifier is not None:
            self.toc.append([1, identifier, len(self.t.pdf)])

    def compact_item(self, identifier, title):
        self.identifier = None
        heading = f"候选编号：{identifier}  {title}"
        required = len(self.t.wrap(heading, 523, 12)) * 18.6 + 38
        if self.page is None or self.y + required > 792:
            self._next()
        self.identifier = identifier
        self.toc.append([1, identifier, len(self.t.pdf),
                         {"kind": fitz.LINK_GOTO, "page": len(self.t.pdf) - 1,
                          "to": fitz.Point(36, self.y - 14)}])
        self.paragraph(heading, 12, ACCENT, gap=6)

    def _next(self):
        self.page = self.t.new_page()
        self.t.text(self.page, 36, 31, "图纸对比 / 候选证据详情", 10, ACCENT)
        self.page.draw_line((36, 42), (559, 42), color=RULE, width=0.6)
        self.y = 62
        if self.identifier:
            for line in self.t.wrap("候选编号：" + self.identifier, 523, 11):
                self.t.text(self.page, 36, self.y, line, 11, ACCENT)
                self.y += 16
            self.y += 8

    def _room(self, height):
        if self.y + height > 792:
            self._next()

    def paragraph(self, text, size=10, color=INK, gap=8, indent=0, shaded=False):
        for line in self.t.wrap(text, 523 - 2 * indent, size):
            self._room(size * 1.55)
            if shaded:
                self.page.draw_rect((36, self.y - size - 2, 559, self.y + size * .55),
                                    color=None, fill=(.95, .97, .97))
            self.t.text(self.page, 36 + indent, self.y, line, size, color)
            self.y += size * 1.55
        self.y += gap

    def table(self, rows):
        if not rows:
            self.paragraph("（空表格）", color=MUTED)
            return
        columns = max(map(len, rows))
        width = 523 / columns
        for row_index, row in enumerate(rows):
            cells = [self.t.wrap(cell, width - 10, 9) for cell in row]
            count = max(map(len, cells))
            # Split even a single enormous cell across pages, without clipping.
            for line_index in range(count):
                self._room(17)
                top, bottom = self.y - 11, self.y + 6
                for column in range(columns):
                    x = 36 + column * width
                    self.page.draw_rect((x, top, x + width, bottom), color=RULE,
                                        width=.4, fill=(.94, .97, .97) if row_index == 0 else None)
                    if column < len(cells) and line_index < len(cells[column]):
                        self.t.text(self.page, x + 5, self.y, cells[column][line_index], 9)
                self.y += 17
            self.y += 2
        self.y += 8


def render_comparison_pdf(snapshot: dict, documents: dict, result: dict) -> bytes:
    """Return a complete PDF of the current evidence snapshot, or raise PdfExportError.

    Pane pages are one-based. Source paths must be server-owned ``Path`` objects;
    job/session/revision locking and authorization belong to the calling route.
    Output starts with A3 landscape followed by A4 portrait detail pages.
    """
    _validate(snapshot, documents, result)
    try:
        with ExitStack() as stack:
            sources = {}
            for side in ("old", "new"):
                source = stack.enter_context(fitz.open(documents[side]["path"]))
                if (not source.is_pdf or source.needs_pass
                        or len(source) != documents[side]["page_count"]):
                    _fail("服务器图纸页数或格式已变化，请刷新后重新导出。")
                sources[side] = source
            pdf = stack.enter_context(fitz.open())
            t = _Typesetter(pdf)
            # Fail explicitly for unavailable glyphs, including captured labels.
            for text in _strings(snapshot):
                t.wrap(text, 100000, 1)
            overview = t.new_page(1190.551, 841.89)
            t.text(overview, 32, 43, "工程图纸 · 对比证据快照", 23, INK)
            t.text(overview, 33, 67, "旧图在左 / 新图在右 · 完整选定页面 · 不受浏览器缩放或滚动裁切影响", 10, MUTED)
            t.text(overview, 33, 88, f"当前筛选候选 {len(snapshot['items'])} 项；全部编号及详情见后续报告。", 10)
            if not snapshot["items"]:
                t.text(overview, 33, 107, "当前筛选无候选项；这不表示图纸没有差异。", 10, ACCENT)
            for side, left, right, name in (("old", 32, 585, "旧图"),
                                           ("new", 605, 1158.551, "新图")):
                pane = snapshot["panes"][side]
                t.text(overview, left, 132, f"{name} / 第 {pane['page']} 页", 12, ACCENT)
                panel = fitz.Rect(left, 145, right, 736)
                overview.draw_rect(panel, color=RULE, width=.6, fill=(1, 1, 1))
                image = _image_rect(sources[side][pane["page"] - 1].rect, panel)
                _source_image(overview, sources[side], pane["page"] - 1, image)
                _primitives(overview, pane, image, t)
            legend = (
                (32, (1, 0, 0), None, "红色：已观察差异"),
                (280, (1, 0, 0), "[4 3] 0", "红色虚线：投影对侧位置（非实测变化）"),
                (765, (1, .72, 0), "[4 3] 0", "黄色：待复核区域"),
            )
            for x, color, dash, text in legend:
                overview.draw_line((x, 760), (x + 26, 760), color=color, width=1.4, dashes=dash)
                t.text(overview, x + 34, 764, text, 10)
            t.text(overview, 32, 789, "本快照仅保留已有候选证据，不执行新的分析，也不保证覆盖全部变化。标记以捕获的颜色、虚线与透明度为准。", 10, MUTED)
            report = _Report(t)
            report.start()
            concise = snapshot.get("detail_mode") == "concise"
            section_title = "差异简述" if concise else "快照范围与来源"
            report.toc.append([1, section_title, 2])
            report.paragraph(section_title, 19, ACCENT, gap=15)
            filters = snapshot["filters"]
            if concise:
                for side, name in (("old", "旧图"), ("new", "新图")):
                    report.paragraph(f"{name}：{documents[side]['name']}（第 {snapshot['panes'][side]['page']} 页）",
                                     9, MUTED, gap=4)
                report.paragraph(f"筛选：{filters['channel']} / {filters['review']}；共 {len(snapshot['items'])} 项。",
                                 9, MUTED, gap=6)
                report.paragraph("以下均为待复核候选；模型理由非原文证据，图形变化不确认实体部件增删。"
                                 "总览仅显示当前所选页；完整来源与定位细节见网页。", 9, MUTED, gap=16)
            else:
                report.paragraph("这是当前筛选结果的已有候选证据快照，不执行新的分析，不保证发现或覆盖全部变化。"
                                 "总览展示当前选定的完整旧图和新图页；不导出浏览器缩放、滚动裁切或其他未选定图纸页。")
                report.paragraph(f"任务：{snapshot['job_id']}    版本：{snapshot['revision']}")
                report.paragraph(f"筛选 / 通道：{filters['channel']}；复核：{filters['review']}")
                report.paragraph("显示开关 / " + "；".join(
                    f"{name}：{'开' if filters[key] else '关'}"
                    for key, name in (("interpretation", "解释"), ("translation", "平移"), ("scaling", "缩放差异"))))
                for side, name in (("old", "旧图（左）"), ("new", "新图（右）")):
                    doc, pane = documents[side], snapshot["panes"][side]
                    report.paragraph(name, 13, ACCENT)
                    report.paragraph(f"文件名：{doc['name']}\nSHA-256：{doc['sha256']}\n"
                                     f"选定页：{pane['page']} / 总页数：{doc['page_count']}")
                report.paragraph("图例与证据边界", 13, ACCENT)
                report.paragraph("红色为已观察差异；红色虚线为投影对侧位置，不是实测变化；黄色为待复核区域。"
                                 "具体线宽、颜色、透明度及虚线采用当前页面捕获的样式。空白对侧没有标记时，不补画推测区域。")
                report.paragraph(f"当前筛选候选数量：{len(snapshot['items'])}", 12)
            if not snapshot["items"]:
                report.paragraph("当前筛选无候选项；这不表示图纸没有差异。")
            for item in snapshot["items"]:
                if concise:
                    report.compact_item(item["id"], item["title"])
                else:
                    report.start(item["id"])
                    report.paragraph(item["title"], 16, ACCENT, gap=12)
                if item["meta"]:
                    report.paragraph(item["meta"], 9, MUTED, gap=14)
                for block in item["blocks"]:
                    if block["kind"] == "table":
                        report.table(block["rows"])
                    else:
                        heading, code = block["kind"] == "heading", block["kind"] == "code"
                        report.paragraph(block["text"], 12 if heading else 10,
                                         ACCENT if heading else INK, indent=7 if code else 0, shaded=code)
                if concise:
                    report.y += 8
            if concise:
                report.identifier = None
                report.paragraph("来源标识 / " + "；".join(
                    f"{name} SHA-256：{documents[side]['sha256']}" for side, name in (("old", "旧图"), ("new", "新图"))),
                    7, MUTED, gap=4)
            for index, page in enumerate(pdf):
                page.draw_line((32, 810), (page.rect.width - 32, 810), color=RULE, width=.5)
                t.text(page, 32, 827, f"证据快照 · 第 {index + 1} / {len(pdf)} 页", 8, MUTED)
            pdf.set_toc(report.toc)
            pdf.set_metadata({"title": "工程图纸对比证据快照", "creator": "cu_diff local PDF export",
                              "subject": "已有候选证据；非新增分析；不保证覆盖全部变化"})
            pdf.subset_fonts()
            return pdf.tobytes(garbage=4, deflate=True)
    except PdfExportError:
        raise
    except Exception as exc:
        raise PdfExportError("PDF 导出失败：本地图纸读取或完整排版失败，请刷新并缩小筛选范围后重试。") from exc
