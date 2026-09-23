import copy
import hashlib
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

import pymupdf as fitz

from cu_diff.pdf_export import PdfExportError, render_comparison_pdf


def marker(identifier="M001", kind="change", x=100, y=200, width=300, height=100):
    return {"id": identifier, "x": x, "y": y, "width": width, "height": height, "kind": kind,
            "stroke": {"color": [1, 0, 0], "opacity": .7, "width": 2, "dash": []},
            "fill": {"color": [1, 0, 0], "opacity": .12}}


class PdfExportTests(unittest.TestCase):
    def setUp(self):
        # Synthetic files stay inside the worktree, never the system temp folder.
        self.root = Path.cwd() / (".pdf-export-test-" + uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.documents = {}
        for side, size in (("old", (600, 300)), ("new", (300, 600))):
            path = self.root / f"{side}.pdf"
            with fitz.open() as doc:
                page = doc.new_page(width=size[0], height=size[1])
                page.insert_text((20, 30), side.upper() + " ORIGINAL")
                doc.save(path)
            self.documents[side] = {"id": side + "-id", "name": side + "中文图纸.pdf",
                                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                    "path": path, "page_count": 1}
        self.result = {"items": [{"id": "M001"}, {"id": "M002"}]}
        self.snapshot = {
            "revision": 7, "job_id": "job-123",
            "filters": {"channel": "全部通道", "review": "待复核",
                        "interpretation": True, "translation": False, "scaling": True},
            "panes": {side: {"document_id": side + "-id", "page": 1, "width": 800,
                             "height": 400 if side == "old" else 1600, "rects": [], "labels": []}
                      for side in ("old", "new")},
            "items": [{"id": "M001", "title": "尺寸发生变化", "meta": "旧图 → 新图",
                       "blocks": [{"kind": "heading", "text": "中文说明"},
                                  {"kind": "paragraph", "text": "旧值：十毫米；新值：二十毫米。"},
                                  {"kind": "table", "rows": [["字段", "旧图", "新图"],
                                                           ["长度", "10", "20"]]},
                                  {"kind": "code", "text": "原始证据 M001"}]},
                      {"id": "M002", "title": "材料复核", "meta": "待复核",
                       "blocks": [{"kind": "paragraph", "text": "末项完整详情"}]}]}
        self.snapshot["panes"]["old"]["rects"] = [marker()]
        self.snapshot["panes"]["old"]["labels"] = [
            {"id": "M001", "x": 100, "y": 180, "font_size": 30, "color": [1, 0, 0]}]

    def render(self):
        return render_comparison_pdf(self.snapshot, self.documents, self.result)

    def test_searchable_chinese_complete_details_and_unchanged_sources(self):
        data = self.render()
        self.assertTrue(data.startswith(b"%PDF-"))
        with fitz.open(stream=data, filetype="pdf") as pdf:
            text = "".join(page.get_text() for page in pdf)
            for expected in ("工程图纸", "中文说明", "旧值：十毫米", "二十毫米", "长度", "材料复核",
                             "末项完整详情", "M001", "M002", "旧图", "新图", "全部通道", "待复核",
                             "job-123", "非实测变化", "不保证覆盖全部变化", "OLD ORIGINAL", "NEW ORIGINAL"):
                self.assertIn(expected, text)
            for doc in self.documents.values():
                self.assertIn(doc["sha256"], text.replace("\n", ""))
                self.assertIn(doc["name"], text)
            self.assertAlmostEqual(pdf[0].rect.width, 1190.551, places=2)
            self.assertEqual([row[1] for row in pdf.get_toc()],
                             ["对比总览", "快照范围与来源", "M001", "M002"])
            self.assertFalse(pdf[0].get_images())  # Source pages remain vectors.
        for doc in self.documents.values():
            self.assertEqual(hashlib.sha256(doc["path"].read_bytes()).hexdigest(), doc["sha256"])

    def test_exact_geometry_styles_and_empty_counterpart(self):
        old = self.snapshot["panes"]["old"]
        tiny = marker(x=990, y=990, width=.01, height=.02)
        review = marker("M002", "review", x=20, y=40, width=50, height=60)
        review["stroke"].update(color=[1, .8, 0], dash=[4, 2])
        review["fill"].update(color=[1, .8, 0], opacity=.2)
        old["rects"] += [tiny, review]
        with fitz.open(stream=self.render(), filetype="pdf") as pdf:
            rects = [d for d in pdf[0].get_drawings() if abs((d.get("fill_opacity") or 0) - .12) < .001]
            self.assertEqual(len(rects), 2)
            # Old page 600x300 fitted to panel (32,145)-(585,736):
            # image (32,302.25)-(585,578.75), not the entire tall panel.
            expected = fitz.Rect(87.3, 357.55, 253.2, 385.2)
            for actual, target in zip(rects[0]["rect"], expected):
                self.assertAlmostEqual(actual, target, places=3)
            self.assertAlmostEqual(rects[1]["rect"].width, .00553, places=4)
            self.assertAlmostEqual(rects[1]["rect"].height, .00553, places=4)
            self.assertAlmostEqual(rects[0]["width"], 2 * 553 / 800, places=4)
            self.assertAlmostEqual(rects[0]["stroke_opacity"], .7, places=3)
            label = next(span for block in pdf[0].get_text("dict")["blocks"] if "lines" in block
                         for line in block["lines"] for span in line["spans"] if span["text"] == "M001")
            self.assertAlmostEqual(label["size"], 30 * 276.5 / 1000, places=3)
            self.assertAlmostEqual(label["origin"][0], 87.3, places=3)
            self.assertAlmostEqual(label["origin"][1], 302.25 + 180 * 276.5 / 1000, places=3)
            yellow = [d for d in pdf[0].get_drawings() if d.get("fill") and d["fill"][1] > .79
                      and d["fill"][0] == 1 and d["fill"][2] == 0]
            self.assertEqual(len(yellow), 1)
            self.assertIn("2.765", yellow[0]["dashes"])
            self.assertIn("1.3825", yellow[0]["dashes"])
            # No selected rectangles at all in the new panel.
            self.assertFalse([d for d in rects + yellow if d["rect"].x0 > 600])

    def test_portrait_counterpart_mapping_and_dash(self):
        rect = marker(kind="counterpart", x=100, y=200, width=300, height=100)
        rect["stroke"]["dash"] = [3, 5]
        self.snapshot["panes"]["new"]["rects"] = [rect]
        with fitz.open(stream=self.render(), filetype="pdf") as pdf:
            drawing = next(d for d in pdf[0].get_drawings()
                           if d["rect"].x0 > 600 and abs((d.get("fill_opacity") or 0) - .12) < .001)
            # 300x600 -> 295.5x591, centered horizontally inside new panel.
            image_left = 605 + (553.551 - 295.5) / 2
            expected = (image_left + 29.55, 263.2, image_left + 118.2, 322.3)
            for actual, target in zip(drawing["rect"], expected):
                self.assertAlmostEqual(actual, target, places=3)
            self.assertEqual(drawing["color"], (1, 0, 0))
            self.assertNotEqual(drawing["dashes"], "[] 0")
            self.assertAlmostEqual(drawing["width"], 2 * 295.5 / 800, places=4)

    def test_long_text_unbroken_identifiers_tables_paginate_without_missing_tail(self):
        token = "UNBROKEN" * 800 + "IDENTIFIER_TAIL"
        self.snapshot["items"][0]["blocks"] = [
            {"kind": "paragraph", "text": "连续中文内容。" * 1200 + "段落终点"},
            {"kind": "code", "text": token},
            {"kind": "table", "rows": [["字段", "内容"], ["长证据", "表格文字" * 800 + "表格终点"]]},
        ]
        with fitz.open(stream=self.render(), filetype="pdf") as pdf:
            self.assertGreater(len(pdf), 10)
            all_text = "".join(p.get_text() for p in pdf)
            body_text = "".join(p.get_text(clip=fitz.Rect(36, 74, 559, 800))
                                for p in list(pdf)[2:]).replace("\n", "")
            self.assertTrue(token in body_text, "Unbroken identifier content must survive pagination")
            self.assertIn("段落终点", body_text)
            self.assertIn("表格终点", body_text)
            self.assertIn("末项完整详情", all_text)
            for p in list(pdf)[2:-1]:
                self.assertIn("候选编号：M001", p.get_text())
            for p in list(pdf)[1:]:
                for word in p.get_text("words"):
                    self.assertGreaterEqual(word[0], 30)
                    self.assertLessEqual(word[2], p.rect.width - 30)
                    self.assertGreaterEqual(word[1], 0)
                    self.assertLessEqual(word[3], p.rect.height)

    def test_markup_is_literal_without_resources_or_links(self):
        attack = '<script>alert("中文")</script><img src="https://example.invalid/x"><a href="file:///secret">链接</a>'
        self.snapshot["items"][0]["blocks"] = [{"kind": "paragraph", "text": attack}]
        with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            data = self.render()
        with fitz.open(stream=data, filetype="pdf") as pdf:
            self.assertIn(attack, "".join(p.get_text() for p in pdf).replace("\n", ""))
            self.assertFalse(any(p.get_links() or p.get_images() for p in pdf))

    def test_empty_filter_is_valid_not_no_difference_claim(self):
        self.snapshot["items"] = []
        for pane in self.snapshot["panes"].values():
            pane["rects"], pane["labels"] = [], []
        with fitz.open(stream=self.render(), filetype="pdf") as pdf:
            self.assertEqual(len(pdf), 2)
            self.assertIn("当前筛选无候选项；这不表示图纸没有差异", pdf[0].get_text())

    def test_annotations_widgets_and_rotation_normalized_source(self):
        path = self.documents["old"]["path"]
        with fitz.open(path) as source:
            source[0].set_rotation(90)
            source[0].remove_rotation()
            annot = source[0].add_rect_annot(fitz.Rect(40, 60, 90, 100))
            annot.set_colors(stroke=(0, 1, 0))
            annot.update()
            widget = fitz.Widget()
            widget.field_name = "synthetic"
            widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
            widget.field_value = "WIDGET VALUE"
            widget.rect = fitz.Rect(40, 130, 150, 165)
            source[0].add_widget(widget)
            source.saveIncr()
        before = path.read_bytes()
        self.documents["old"]["sha256"] = hashlib.sha256(before).hexdigest()
        self.snapshot["panes"]["old"]["rects"] = []
        with fitz.open(stream=self.render(), filetype="pdf") as pdf:
            self.assertTrue(pdf[0].get_images())
            with fitz.open(path) as source:
                expected = source[0].get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False, annots=True)
            embedded = fitz.Pixmap(pdf, pdf[0].get_images()[0][0])
            self.assertEqual(embedded.samples, expected.samples)
            pixmap = pdf[0].get_pixmap()
            self.assertTrue(any(g > 150 and r < 100 and b < 100
                                for r, g, b in zip(pixmap.samples[::3], pixmap.samples[1::3],
                                                   pixmap.samples[2::3])))
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_inputs_raise_explicit_errors(self):
        cases = [
            lambda s: s["panes"]["old"].update(document_id="wrong"),
            lambda s: s["panes"]["old"].update(page=0),
            lambda s: s["panes"]["old"].update(page=2),
            lambda s: s["panes"]["old"].update(width=0),
            lambda s: s["panes"]["old"].update(path="file:///secret"),
            lambda s: s["items"][0].update(id="foreign"),
            lambda s: s["items"].append(copy.deepcopy(s["items"][0])),
            lambda s: s["panes"]["old"]["rects"][0].update(id="foreign"),
            lambda s: s["panes"]["old"]["labels"][0].update(id="foreign"),
            lambda s: s["panes"]["old"]["rects"][0].update(x=float("nan")),
            lambda s: s["panes"]["old"]["rects"][0].update(x=1e30),
            lambda s: s["panes"]["old"]["rects"][0].update(x=10**1000),
            lambda s: s["panes"]["old"]["rects"][0].update(x=-1),
            lambda s: s["panes"]["old"]["rects"][0].update(width=0),
            lambda s: s["panes"]["old"]["rects"][0].update(width=1000),
            lambda s: s["panes"]["old"]["rects"][0]["stroke"].update(dash=[0, 0]),
            lambda s: s["panes"]["old"]["rects"][0]["stroke"].update(color=[1, 0, 4]),
            lambda s: s["panes"]["old"]["rects"][0]["fill"].update(opacity=-.1),
            lambda s: s["panes"]["old"]["labels"][0].update(font_size=float("inf")),
            lambda s: s["filters"].update(translation="yes"),
            lambda s: s["items"][0]["blocks"].append({"kind": "image", "text": "http://invalid"}),
            lambda s: s["items"][0]["blocks"].append({"kind": "table", "rows": [[]]}),
        ]
        original = copy.deepcopy(self.snapshot)
        for index, mutate in enumerate(cases):
            with self.subTest(index=index):
                self.snapshot = copy.deepcopy(original)
                mutate(self.snapshot)
                with self.assertRaisesRegex(PdfExportError, "PDF 导出失败"):
                    self.render()

    def test_caps_are_errors_not_truncated_success(self):
        for name, value in (("MAX_ITEMS", 1), ("MAX_PAYLOAD_BYTES", 100),
                            ("MAX_PAGES", 2), ("MAX_PRIMITIVES", 1), ("MAX_TEXT_CHARS", 20)):
            with self.subTest(cap=name), patch("cu_diff.pdf_export." + name, value):
                with self.assertRaises(PdfExportError):
                    self.render()


if __name__ == "__main__":
    unittest.main()
