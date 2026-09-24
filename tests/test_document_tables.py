"""Synthetic source PDFs and OCR only; no customer fixtures or service calls."""

from copy import deepcopy
from pathlib import Path
import shutil
import unittest
import uuid

import pymupdf

from cu_diff.document_tables import _merge_lines, compare_document_tables


class DocumentTableTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path("local") / ("document-table-tests-" + uuid.uuid4().hex)
        self.directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)

    def drawing(self, name, headers=None, rows=None, origin=(30, 40),
                bottom=False, duplicate=False, incomplete=False, partial=False,
                filled=False, shared_border=False, row_height=24, shared_top=False):
        headers = headers or ["Customer P/N", "Supplier P/N", "L", "Barcode Tag"]
        rows = rows if rows is not None else [["TEST-A", "PART-X", "100", "Yes"]]
        doc = pymupdf.open()
        page = doc.new_page(width=720, height=520)
        x0, y0 = origin

        def table(top):
            matrix = rows + [headers] if bottom else [headers] + rows
            xs = [x0 + i * 115 for i in range(len(headers) + 1)]
            ys = [top + i * row_height for i in range(len(matrix) + 1)]
            for index, x in enumerate(xs):
                bottom_y = ys[1] if partial and index == 2 else ys[-1]
                if filled:
                    page.draw_rect((x - .2, ys[0], x + .2, bottom_y),
                                   color=None, fill=(0, 0, 0))
                else:
                    page.draw_line((x, ys[0]), (x, bottom_y), width=.5)
            for y in ys:
                if filled:
                    page.draw_rect((xs[0], y - .2, xs[-1], y + .2),
                                   color=None, fill=(0, 0, 0))
                else:
                    page.draw_line((xs[0] - 20 if shared_top and y == ys[0] else xs[0], y),
                                   (xs[-1], y), width=.5)
            if shared_border:
                page.draw_line((xs[0], ys[-1]), (xs[-1] + 80, ys[-1]), width=.5)
                page.draw_line((xs[-1] - .54, ys[0] + 5),
                               (xs[-1] - .54, ys[0] + 13), width=.5)
                page.draw_line((xs[0] + 80, ys[0] + 5),
                               (xs[0] + 80, ys[0] + 13), width=.5)
            for r, row in enumerate(matrix):
                for c, text in enumerate(row):
                    if text:
                        page.insert_text((xs[c] + 3, ys[r] + 15), text, fontsize=8)

        table(y0)
        if duplicate:
            table(y0 + 200)
        path = self.directory / f"{name}.pdf"
        doc.save(path)
        words = []
        for x0, y0, x1, y1, text, *_ in page.get_text("words"):
            coordinates = [x0, y0, x1, y0, x1, y1, x0, y1]
            words.append({"content": text, "confidence": .99,
                          "source": "D(1," + ",".join(str(v / 72) for v in coordinates) + ")"})
        doc.close()
        if incomplete:
            words = [word for word in words if word["content"] != "PART-X"]
        return path, {"status": "Succeeded", "result": {"contents": [{
            "unit": "inch", "pages": [{"pageNumber": 1, "width": 10,
                                      "height": 520 / 72, "words": words}],
            # Contradictory CU grids must never govern source row counts.
            "tables": [{"rowCount": 99, "columnCount": 99, "cells": []}],
        }]}}

    def compare(self, old, new):
        return compare_document_tables(old[0], new[0], old[1], new[1])

    def test_changed_identity_and_movement_without_cu_table(self):
        old = self.drawing("old")
        new = self.drawing("new", rows=[["TEST-B", "PART-Y", "100", "Yes"]], origin=(75, 110))
        before = deepcopy((old[1], new[1]))
        result = self.compare(old, new)
        self.assertEqual(result["coverage"]["paired_tables"], 1, result)
        self.assertEqual([r["key"] for r in result["items"]], ["Customer P/N", "Supplier P/N"])
        self.assertEqual(result["items"][0]["old"]["raw_text"], "TEST-A")
        self.assertEqual(result["items"][0]["new"]["raw_text"], "TEST-B")
        self.assertEqual([r["id"] for r in result["items"]], ["T001", "T002"])
        self.assertEqual((old[1], new[1]), before)
        for item in result["items"]:
            self.assertEqual(item["channel"], "tables")
            self.assertEqual(item["table_comparison"]["status"], "complete")
            self.assertEqual(item["table_comparison"]["scope"], "observed_cell_text_pair")
            for side in ("old", "new"):
                self.assertTrue(item[side]["source"].startswith("D("))
                for location in item[side]["locations"]:
                    self.assertTrue(0 <= location["x"] < 1)
                    self.assertTrue(0 < location["width"] < 1)

    def test_reordered_columns_do_not_create_changes(self):
        old = self.drawing("old")
        new = self.drawing("new", headers=["L", "Customer P/N", "Barcode Tag", "Supplier P/N"],
                           rows=[["100", "TEST-A", "Yes", "PART-X"]])
        result = self.compare(old, new)
        self.assertEqual(result["coverage"]["paired_tables"], 1, result)
        self.assertEqual(result["items"], [])

    def test_columns_have_null_missing_side_and_separate_context(self):
        old = self.drawing("old", headers=["Customer P/N", "Supplier P/N", "L", "Net Weight"],
                           rows=[["TEST-A", "PART-X", "100", "50g"]])
        new = self.drawing("new")
        records = self.compare(old, new)["items"]
        self.assertEqual({r["change"] for r in records}, {"table_column_added", "table_column_removed"})
        for record in records:
            missing = "old" if record["change"] == "table_column_added" else "new"
            self.assertIsNone(record[missing])
            self.assertTrue(record["table_context"][missing]["locations"])
            self.assertEqual(record["table_comparison"]["old_grid"]["columns"], 4)
            self.assertEqual(record["table_comparison"]["status"], "complete")
            self.assertEqual(record["table_comparison"]["scope"], "column_presence_only")

    def test_blank_drawn_row_difference_not_record_deletion(self):
        headers = ["REV", "ECN NO", "DESCRIPTION", "APPD.", "DATE"]
        old = self.drawing("old", headers=headers,
                           rows=[["1", "", "Initial", "", "01/02/20"]], bottom=True)
        new = self.drawing("new", headers=headers,
                           rows=[["", "", "", "", ""],
                                 ["1", "", "Initial", "", "02/03/21"]], bottom=True)
        result = self.compare(old, new)
        grid, date = result["items"]
        self.assertEqual(grid["change"], "table_grid_changed")
        self.assertEqual(grid["old"]["detail"]["evidence_kind"], "vector_grid")
        self.assertIn("2 ruled rows", grid["old"]["raw_text"])
        self.assertIn("3 ruled rows", grid["new"]["raw_text"])
        measurements = grid["table_comparison"]
        self.assertEqual(measurements["old_grid"]["rows_including_header"], 2)
        self.assertEqual(measurements["new_grid"]["rows_including_header"], 3)
        self.assertEqual(measurements["new_grid"]["blank_rows_verified"], 1)
        self.assertEqual(measurements["old_grid"]["populated_rows_observed"], 1)
        self.assertEqual(len(measurements["new_grid"]["horizontal_lines"]), 4)
        self.assertEqual(date["key"], "DATE")
        self.assertEqual(date["change"], "table_cell_modified")
        self.assertEqual(grid["table_comparison"]["status"], "complete")
        self.assertEqual(grid["table_comparison"]["scope"], "structural_grid_only")

    def test_incomplete_ocr_is_not_empty_cell_or_removed_record(self):
        old = self.drawing("old", incomplete=True)
        new = self.drawing("new", rows=[["TEST-A", "PART-Y", "100", "Yes"]])
        result = self.compare(old, new)
        self.assertEqual(result["items"], [])
        self.assertTrue(any("incomplete OCR" in warning for warning in result["warnings"]), result)

    def test_true_no_text_blank_cells_are_not_incomplete(self):
        old = self.drawing("old", rows=[["TEST-A", "", "100", ""]])
        new = self.drawing("new", rows=[["TEST-B", "", "100", ""]])
        result = self.compare(old, new)
        self.assertEqual(len(result["items"]), 1, result)
        self.assertFalse(any("incomplete OCR" in warning for warning in result["warnings"]))

    def test_ambiguous_tables_fail_without_positional_guess(self):
        old = self.drawing("old", duplicate=True)
        new = self.drawing("new", rows=[["TEST-B", "PART-Y", "100", "Yes"]])
        result = self.compare(old, new)
        self.assertEqual(result["items"], [])
        self.assertTrue(any("ambiguous header" in warning for warning in result["warnings"]))

    def test_missing_source_fails_explicitly(self):
        old, new = self.drawing("old"), self.drawing("new")
        old[0].unlink()
        result = self.compare(old, new)
        self.assertEqual(result["items"], [])
        self.assertTrue(any("PDF source unavailable" in warning for warning in result["warnings"]))

    def test_wrong_page_geometry_fails_explicitly(self):
        old, new = self.drawing("old"), self.drawing("new")
        new[1]["result"]["contents"][0]["pages"][0]["width"] = 12
        result = self.compare(old, new)
        self.assertEqual(result["items"], [])
        self.assertTrue(any("geometry mismatch" in warning for warning in result["warnings"]))

    def test_bom_excluded(self):
        headers = ["No", "Description", "Quantity", "Material"]
        old = self.drawing("old", headers=headers, rows=[["1", "CONNECTOR", "1", "BRASS"]])
        new = self.drawing("new", headers=headers, rows=[["1", "CONNECTOR", "2", "BRASS"]])
        self.assertEqual(self.compare(old, new)["items"], [])

    def test_ambiguous_multiple_changed_records_not_paired_by_order(self):
        old = self.drawing("old", rows=[["TEST-A", "PART-X", "100", "Yes"],
                                        ["TEST-B", "PART-Y", "100", "Yes"]])
        new = self.drawing("new", rows=[["TEST-C", "PART-Z", "100", "Yes"],
                                        ["TEST-D", "PART-Q", "100", "Yes"]])
        result = self.compare(old, new)
        self.assertEqual(result["items"], [])
        self.assertTrue(any("ambiguous/unmatched populated" in w for w in result["warnings"]))

    def test_incomplete_header_ocr_does_not_prove_removed_column(self):
        old, new = self.drawing("old"), self.drawing("new")
        words = new[1]["result"]["contents"][0]["pages"][0]["words"]
        words[:] = [w for w in words if w["content"] != "Supplier"]
        result = self.compare(old, new)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["coverage"]["new_eligible_tables"], 0)

    def test_low_confidence_header_is_not_used_for_correspondence(self):
        old, new = self.drawing("old"), self.drawing("new")
        new[1]["result"]["contents"][0]["pages"][0]["words"][0]["confidence"] = .2
        result = self.compare(old, new)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["coverage"]["new_eligible_tables"], 0)

    def test_partial_grid_is_not_a_column_deletion(self):
        old, new = self.drawing("old"), self.drawing("new", partial=True)
        result = self.compare(old, new)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["coverage"]["new_eligible_tables"], 0)

    def test_filled_vector_bars_are_measurable_rulings(self):
        old = self.drawing("old", filled=True)
        new = self.drawing("new", rows=[["TEST-B", "PART-X", "100", "Yes"]], filled=True)
        result = self.compare(old, new)
        self.assertEqual(len(result["items"]), 1, result)
        self.assertEqual(result["items"][0]["key"], "Customer P/N")

    def test_duplicate_page_evidence_is_ambiguous(self):
        old, new = self.drawing("old"), self.drawing("new")
        contents = new[1]["result"]["contents"]
        contents.append(deepcopy(contents[0]))
        result = self.compare(old, new)
        self.assertEqual(result["items"], [])
        self.assertTrue(any("duplicate OCR page" in w for w in result["warnings"]))

    def test_malformed_ocr_fails_into_diagnostics(self):
        old, new = self.drawing("old"), self.drawing("new")
        new[1]["result"] = None
        result = self.compare(old, new)
        self.assertEqual(result["items"], [])
        self.assertTrue(any("invalid OCR contents" in w for w in result["warnings"]))

    def test_identical_moved_table_stays_unchanged(self):
        result = self.compare(self.drawing("old"), self.drawing("new", origin=(70, 200)))
        self.assertEqual(result["items"], [])
        self.assertEqual(result["coverage"]["paired_tables"], 1)

    def test_unique_revision_anchors_multiple_records(self):
        headers = ["REV", "ECN NO", "DESCRIPTION", "APPD.", "DATE"]
        old = self.drawing("old", headers=headers, rows=[
            ["1", "", "First", "", "01/02/20"], ["2", "", "Next", "", "02/03/21"]], bottom=True)
        new = self.drawing("new", headers=headers, rows=[
            ["2", "", "Next", "", "03/04/22"], ["1", "", "First", "", "01/02/20"]], bottom=True)
        result = self.compare(old, new)
        self.assertEqual(len(result["items"]), 1, result)
        self.assertEqual(result["items"][0]["old"]["raw_text"], "02/03/21")
        self.assertEqual(result["items"][0]["new"]["raw_text"], "03/04/22")

    def test_single_column_panel_reports_geometry_without_inventing_header_or_rows(self):
        old = self.drawing("old", headers=["INSCRIPTION"], rows=[["SYNTHETIC A"]])
        new = self.drawing("new", headers=["INSCRIPTION"], rows=[["SYNTHETIC B"]])
        result = self.compare(old, new)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["coverage"]["comparison_status"], "no_match")
        for side in ("old", "new"):
            grid, = result["coverage"][side + "_source_grids"]
            self.assertEqual(grid["columns"], 1)
            self.assertEqual(grid["ruled_bands"], 2)
            self.assertIsNone(grid["header_assignment"])
            self.assertEqual(grid["status"], "unmatched_or_excluded")
            self.assertEqual(grid["first_band_ocr"], "INSCRIPTION")

    def test_panel_shared_page_border_and_vector_glyph_stems_keep_source_bounds(self):
        old = self.drawing("old", headers=["INSCRIPTION"], rows=[["SYNTHETIC A"]],
                           shared_border=True)
        result = self.compare(old, old)
        for side in ("old", "new"):
            grid, = result["coverage"][side + "_source_grids"]
            self.assertEqual(grid["ruled_bands"], 2)
            self.assertEqual(grid["vertical_boundaries_points"], [30, 145])
        self.assertEqual(result["items"], [])

    def panel(self, name, text, **kwargs):
        return self.drawing(name, headers=["PANEL NOTES"], rows=[[text]],
                            row_height=48, **kwargs)

    def test_panel_identical_joined_text_with_two_to_three_lines_is_only_reflow(self):
        result = self.compare(self.panel("old", "Alpha beta\nGamma delta"),
                              self.panel("new", "Alpha\nbeta Gamma\ndelta"))
        self.assertEqual(result["items"], [])
        diagnostic, = result["coverage"]["panel_comparisons"]
        self.assertEqual(diagnostic["status"], "reflow_only", diagnostic)
        self.assertEqual((diagnostic["old_ocr_line_count"], diagnostic["new_ocr_line_count"]), (2, 3))
        self.assertTrue(diagnostic["normalized_joined_text_equal"])

    def test_panel_changed_text_two_to_three_lines_has_both_sources_not_added_grid_row(self):
        old = self.panel("old", "Alpha beta\nGamma delta")
        new = self.panel("new", "Alpha beta\nGamma changed\nAdditional text")
        for before, after, counts in ((old, new, (2, 3)), (new, old, (3, 2))):
            result = self.compare(before, after)
            record, = result["items"]
            self.assertEqual(record["change"], "table_cell_modified")
            metadata = record["table_comparison"]
            self.assertEqual(metadata["scope"], "observed_panel_text")
            self.assertEqual(metadata["status"], "complete", result)
            self.assertEqual((metadata["old_ocr_line_count"], metadata["new_ocr_line_count"]), counts)
            for side in ("old", "new"):
                self.assertEqual(metadata[side + "_grid"]["rows_including_header"], 2)
                self.assertTrue(record[side]["source"])
                self.assertTrue(record[side]["locations"])
                self.assertEqual(record[side]["detail"]["evidence_kind"], "cached_ocr_words")

    def test_duplicate_panel_headers_reject_positional_pairing(self):
        result = self.compare(self.panel("old", "Alpha beta", duplicate=True),
                              self.panel("new", "Alpha changed"))
        self.assertEqual(result["items"], [])
        self.assertEqual(result["coverage"]["paired_tables"], 0)
        self.assertTrue(any("ambiguous header" in w for w in result["warnings"]))

    def test_relocated_panel_changed_body_keeps_source_coordinates(self):
        result = self.compare(self.panel("old", "Alpha beta"),
                              self.panel("new", "Alpha changed", origin=(150, 170)))
        record, = result["items"]
        self.assertEqual(record["table_comparison"]["status"], "complete")
        self.assertGreater(record["new"]["locations"][0]["x"], record["old"]["locations"][0]["x"])
        self.assertGreater(record["new"]["locations"][0]["y"], record["old"]["locations"][0]["y"])

    def test_panel_missing_ocr_with_visible_ink_is_review_only(self):
        old, new = self.panel("old", "Alpha beta\nGamma delta"), self.panel("new", "Alpha changed")
        words = old[1]["result"]["contents"][0]["pages"][0]["words"]
        words[:] = [w for w in words if w["content"] != "Gamma"]
        result = self.compare(old, new)
        record, = result["items"]
        self.assertEqual(record["table_comparison"]["status"], "review_only")
        self.assertTrue(any("source ink" in issue for issue in record["table_comparison"]["issues"]))
        self.assertFalse(record["old"]["detail"]["cell_text_coverage_complete"])

    def test_panel_low_confidence_ocr_is_never_default_complete(self):
        old, new = self.panel("old", "Alpha beta"), self.panel("new", "Alpha changed")
        for word in old[1]["result"]["contents"][0]["pages"][0]["words"]:
            if word["content"] == "beta":
                word["confidence"] = .3
        record, = self.compare(old, new)["items"]
        self.assertEqual(record["table_comparison"]["status"], "review_only")
        self.assertTrue(any("low-confidence" in issue for issue in record["table_comparison"]["issues"]))

    def test_two_header_like_panel_bands_are_ambiguous(self):
        result = self.compare(self.panel("old", "OTHER HEADER"), self.panel("new", "OTHER TITLE"))
        self.assertEqual(result["items"], [])
        self.assertEqual(result["coverage"]["paired_tables"], 0)

    def test_panel_uncertain_prefix_does_not_hide_independently_verified_changed_span(self):
        old = self.panel("old", "Noise token\nOne two red blue")
        new = self.panel("new", "Noise token\nOne two add red blue")
        for document in (old, new):
            for word in document[1]["result"]["contents"][0]["pages"][0]["words"]:
                if word["content"] == "Noise":
                    word["confidence"] = .3
        record, = self.compare(old, new)["items"]
        metadata = record["table_comparison"]
        self.assertEqual(metadata["status"], "complete")
        self.assertEqual(metadata["scope"], "observed_panel_text")
        self.assertEqual(metadata["evidence_extent"], "source_ink_validated_changed_span")
        self.assertFalse(metadata["panel_text_complete"])
        self.assertTrue(metadata["panel_issues"])
        self.assertEqual(record["old"]["raw_text"], "One two red blue")
        self.assertEqual(record["new"]["raw_text"], "One two add red blue")
        self.assertNotIn("Noise", record["old"]["raw_text"])

    def test_panel_changed_span_cannot_hide_unread_ink_between_anchors(self):
        old = self.panel("old", "One two MISS red blue")
        new = self.panel("new", "One two add red blue")
        words = old[1]["result"]["contents"][0]["pages"][0]["words"]
        words[:] = [word for word in words if word["content"] != "MISS"]
        record, = self.compare(old, new)["items"]
        self.assertEqual(record["table_comparison"]["status"], "review_only")

    def test_disjoint_nearby_rulings_retain_their_own_source_coordinate(self):
        lines = [(100.0, 10, 50), (100.7, 100, 125)]
        self.assertEqual(_merge_lines(lines), lines)

    def test_duplicate_overlapping_ocr_is_unresolved_not_changed_cell_text(self):
        old, new = self.drawing("old"), self.drawing("new")
        words = new[1]["result"]["contents"][0]["pages"][0]["words"]
        words.append(deepcopy(next(word for word in words if word["content"] == "100")))
        result = self.compare(old, new)
        self.assertEqual(result["items"], [])
        self.assertTrue(any("duplicate overlapping words" in warning for warning in result["warnings"]))

    def bom(self, name, rows=None, headers=None, **kwargs):
        headers = headers or ["No", "BOM ITEM", "Q' TY", "P/N", "MATERIAL"]
        rows = rows if rows is not None else [["1", "CONNECTOR", "1", "PART-A", "BRASS"]]
        path, operation = self.drawing(name, headers=headers, rows=rows, **kwargs)
        content = operation["result"]["contents"][0]
        matrix = rows + [headers] if kwargs.get("bottom") else [headers] + rows
        x, y = kwargs.get("origin", (30, 40))
        height = kwargs.get("row_height", 24)
        content["tables"] = [{
            "rowCount": len(matrix), "columnCount": len(headers),
            "cells": [{"rowIndex": ri, "columnIndex": ci,
                       "kind": "columnHeader" if row is headers else "content",
                       "content": text,
                       "source": f"D(1,{(x + ci * 115) / 72},{(y + ri * height) / 72},{115 / 72},{height / 72})"}
                      for ri, row in enumerate(matrix) for ci, text in enumerate(row)],
        }]
        content["markdown"] = "<table>" + "".join(
            "<tr>" + "".join(f"<td>{text}</td>" for text in row) + "</tr>" for row in matrix) + "</table>"
        self.assertNotIn("fields", content)
        return path, operation

    def compare_bom(self, old, new):
        return compare_document_tables(old[0], new[0], old[1], new[1], include_bom=True)

    def test_layout_bom_is_opt_in_and_legacy_exclusion_is_reported(self):
        old = self.bom("old")
        new = self.bom("new", rows=[["1", "CONNECTOR", "2", "PART-A", "BRASS"]])
        legacy = self.compare(old, new)
        self.assertEqual(legacy["items"], [])
        self.assertFalse(legacy["coverage"]["bom"]["enabled"])
        self.assertEqual(legacy["coverage"]["bom"]["old_excluded"], 1)
        self.assertEqual(legacy["coverage"]["scope"], "non_bom_complete_vector_grids")
        layout = self.compare_bom(old, new)
        self.assertEqual(layout["coverage"]["bom"]["old_included"], 1)
        self.assertEqual(layout["coverage"]["bom"]["new_included"], 1)
        self.assertEqual(layout["coverage"]["bom"]["old_excluded"], 0)
        self.assertEqual(layout["coverage"]["scope"], "all_complete_vector_grids")
        record, = layout["items"]
        self.assertEqual(record["key"], "Q' TY")

    def test_layout_bom_quantity_change_only_highlights_real_changed_cell(self):
        old = self.bom("old")
        new = self.bom("new", rows=[["1", "CONNECTOR", "2", "PART-A", "BRASS"]])
        snapshot = deepcopy((old[1], new[1]))
        record, = self.compare_bom(old, new)["items"]
        self.assertEqual(record["change"], "table_cell_modified")
        self.assertEqual(record["table_comparison"]["table_kind"], "bom")
        self.assertEqual(record["old"]["raw_text"], "1")
        self.assertEqual(record["new"]["raw_text"], "2")
        for side in ("old", "new"):
            self.assertEqual(len(record[side]["locations"]), 1)
            self.assertGreater(record[side]["locations"][0]["x"], 260 / 720)
            self.assertLess(record[side]["locations"][0]["x"] + record[side]["locations"][0]["width"], 375 / 720)
        self.assertEqual((old[1], new[1]), snapshot)

    def test_layout_bom_part_number_change_uses_unchanged_description_identity(self):
        record, = self.compare_bom(self.bom("old"), self.bom(
            "new", rows=[["1", "CONNECTOR", "1", "PART-B", "BRASS"]]))["items"]
        self.assertEqual(record["key"], "P/N")
        self.assertEqual((record["old"]["raw_text"], record["new"]["raw_text"]), ("PART-A", "PART-B"))

    def test_layout_bom_same_ordinal_does_not_pair_unrelated_replacement(self):
        result = self.compare_bom(self.bom("old"), self.bom(
            "new", rows=[["1", "HOUSING", "1", "PART-B", "BRASS"]]))
        self.assertEqual(result["items"], [])
        self.assertTrue(any("ambiguous/unmatched BOM" in w for w in result["warnings"]))

    def test_layout_bom_added_and_removed_rows_have_null_missing_side(self):
        old = self.bom("old")
        new = self.bom("new", rows=[["1", "CONNECTOR", "1", "PART-A", "BRASS"],
                                    ["2", "HOUSING", "3", "PART-B", "PLASTIC"]])
        for before, after, change, missing in ((old, new, "table_row_added", "old"),
                                               (new, old, "table_row_removed", "new")):
            result = self.compare_bom(before, after)
            record, = [item for item in result["items"] if item["change"] == change]
            self.assertIsNone(record[missing])
            self.assertTrue(record["table_context"][missing]["locations"])
            self.assertEqual(record["table_comparison"]["scope"], "observed_row_presence")
            self.assertIn("PART-B", record["new" if missing == "old" else "old"]["raw_text"])
            self.assertEqual([i["change"] for i in result["items"]], ["table_grid_changed", change])

    def test_layout_bom_missing_ocr_does_not_prove_row_added(self):
        old = self.bom("old")
        new = self.bom("new", rows=[["1", "CONNECTOR", "1", "PART-A", "BRASS"],
                                    ["2", "HOUSING", "3", "PART-B", "PLASTIC"]])
        words = new[1]["result"]["contents"][0]["pages"][0]["words"]
        words[:] = [word for word in words if word["content"] != "PART-B"]
        result = self.compare_bom(old, new)
        self.assertFalse(any(i["change"] in ("table_row_added", "table_row_removed") for i in result["items"]))
        self.assertTrue(any("incomplete OCR" in warning for warning in result["warnings"]))

    def test_layout_bom_blank_cell_addition_and_removal_have_source_blank_context(self):
        old = self.bom("old", rows=[["1", "CONNECTOR", "1", "", "BRASS"]])
        new = self.bom("new")
        for before, after, change, missing in ((old, new, "table_cell_added", "old"),
                                               (new, old, "table_cell_removed", "new")):
            record, = self.compare_bom(before, after)["items"]
            self.assertEqual(record["change"], change)
            self.assertIsNone(record[missing])
            self.assertTrue(record["table_context"][missing]["locations"])
            context = record["table_comparison"]["blank_counterpart_context"]
            self.assertEqual(context["side"], missing)
            self.assertTrue(context["locations"])

    def test_layout_bom_missing_word_is_not_a_blank_cell(self):
        old, new = self.bom("old"), self.bom("new")
        words = old[1]["result"]["contents"][0]["pages"][0]["words"]
        words[:] = [word for word in words if word["content"] != "PART-A"]
        result = self.compare_bom(old, new)
        self.assertEqual(result["items"], [])
        self.assertTrue(any("cell unresolved" in warning for warning in result["warnings"]))

    def test_layout_bom_duplicate_identities_remain_ambiguous(self):
        rows = [["1", "CONNECTOR", "1", "PART-A", "BRASS"],
                ["2", "CONNECTOR", "1", "PART-A", "BRASS"]]
        new_rows = deepcopy(rows)
        new_rows[1][2] = "2"
        result = self.compare_bom(self.bom("old", rows=rows), self.bom("new", rows=new_rows))
        self.assertEqual(result["items"], [])
        self.assertTrue(any("ambiguous/unmatched BOM" in w for w in result["warnings"]))

    def test_layout_bom_reordered_columns_and_bottom_headers(self):
        old = self.bom("old", bottom=True)
        new = self.bom("new", bottom=True, headers=["P/N", "MATERIAL", "No", "Q' TY", "BOM ITEM"],
                       rows=[["PART-A", "BRASS", "1", "2", "CONNECTOR"]], origin=(70, 150))
        record, = self.compare_bom(old, new)["items"]
        self.assertEqual(record["key"], "Q' TY")
        self.assertEqual(record["old"]["raw_text"], "1")
        self.assertEqual(record["new"]["raw_text"], "2")

    def test_layout_bom_two_column_quantity_table_without_generated_fields(self):
        old = self.bom("old", headers=["Description", "Quantity"], rows=[["CONNECTOR", "1"]])
        new = self.bom("new", headers=["Description", "Quantity"], rows=[["CONNECTOR", "2"]])
        record, = self.compare_bom(old, new)["items"]
        self.assertEqual(record["key"], "Quantity")

    def test_layout_bom_duplicate_tables_never_pair_by_location(self):
        result = self.compare_bom(self.bom("old", duplicate=True), self.bom(
            "new", rows=[["1", "CONNECTOR", "2", "PART-A", "BRASS"]]))
        self.assertEqual(result["items"], [])
        self.assertEqual(result["coverage"]["paired_tables"], 0)

    def test_layout_bom_low_confidence_quantity_is_unresolved(self):
        old, new = self.bom("old"), self.bom("new", rows=[["1", "CONNECTOR", "2", "PART-A", "BRASS"]])
        for word in new[1]["result"]["contents"][0]["pages"][0]["words"]:
            if word["content"] == "2":
                word["confidence"] = .3
        result = self.compare_bom(old, new)
        self.assertEqual(result["items"], [])
        self.assertTrue(any("low-confidence/illegible BOM" in warning for warning in result["warnings"]))

    def test_layout_opt_in_preserves_non_bom_results(self):
        old = self.drawing("old")
        new = self.drawing("new", rows=[["TEST-B", "PART-X", "100", "Yes"]])
        self.assertEqual(self.compare(old, new)["items"], self.compare_bom(old, new)["items"])

    def test_layout_bom_header_on_shared_page_border_requires_actual_column_support(self):
        old = self.bom("old", shared_top=True)
        new = self.bom("new", shared_top=True, rows=[["1", "CONNECTOR", "2", "PART-A", "BRASS"]])
        record, = self.compare_bom(old, new)["items"]
        self.assertEqual(record["key"], "Q' TY")
        self.assertEqual(record["table_comparison"]["old_grid"]["rows_including_header"], 2)

    def test_layout_bom_blank_drawn_row_filling_is_not_a_grid_addition(self):
        old = self.bom("old", rows=[["1", "CONNECTOR", "1", "PART-A", "BRASS"], ["", "", "", "", ""]])
        new = self.bom("new", rows=[["1", "CONNECTOR", "1", "PART-A", "BRASS"],
                                    ["2", "HOUSING", "2", "PART-B", "PLASTIC"]])
        record, = self.compare_bom(old, new)["items"]
        self.assertEqual(record["change"], "table_row_added")
        self.assertEqual(record["table_comparison"]["old_grid"]["blank_rows_verified"], 1)
        self.assertEqual(record["table_comparison"]["old_grid"]["rows_including_header"],
                         record["table_comparison"]["new_grid"]["rows_including_header"])

    def test_layout_bom_conflicting_part_and_description_anchors_are_not_guessed(self):
        old = self.bom("old", rows=[["1", "CONNECTOR", "1", "PART-A", "BRASS"],
                                    ["2", "HOUSING", "1", "PART-B", "PLASTIC"]])
        new = self.bom("new", rows=[["1", "CONNECTOR", "2", "PART-B", "BRASS"],
                                    ["2", "HOUSING", "1", "PART-A", "PLASTIC"]])
        result = self.compare_bom(old, new)
        self.assertEqual(result["items"], [])
        self.assertTrue(any("ambiguous/unmatched BOM" in w for w in result["warnings"]))

    def test_layout_bom_reordered_rows_match_identity_not_position(self):
        old = self.bom("old", rows=[["1", "CONNECTOR", "1", "PART-A", "BRASS"],
                                    ["2", "HOUSING", "1", "PART-B", "PLASTIC"]])
        new = self.bom("new", rows=[["2", "HOUSING", "2", "PART-B", "PLASTIC"],
                                    ["1", "CONNECTOR", "1", "PART-A", "BRASS"]])
        record, = self.compare_bom(old, new)["items"]
        self.assertEqual(record["key"], "Q' TY")
        self.assertLess(record["new"]["locations"][0]["y"], record["old"]["locations"][0]["y"])

    def test_layout_bom_column_presence_is_distinct_from_missing_cell_text(self):
        old = self.bom("old")
        new = self.bom("new", headers=["No", "BOM ITEM", "P/N", "MATERIAL"],
                       rows=[["1", "CONNECTOR", "PART-A", "BRASS"]])
        for before, after, change, missing in ((old, new, "table_column_removed", "new"),
                                               (new, old, "table_column_added", "old")):
            record, = self.compare_bom(before, after)["items"]
            self.assertEqual(record["change"], change)
            self.assertEqual(record["key"], "Q' TY")
            self.assertIsNone(record[missing])
            self.assertEqual(record["table_comparison"]["scope"], "column_presence_only")

    def test_layout_bom_cu_html_counts_cannot_fabricate_source_rows(self):
        old, new = self.bom("old"), self.bom("new")
        content = new[1]["result"]["contents"][0]
        content["tables"][0]["rowCount"] = 20
        content["tables"][0]["columnCount"] = 10
        content["markdown"] += "<table><tr><td>UNSUPPORTED ROW</td></tr></table>"
        result = self.compare_bom(old, new)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["coverage"]["new_tables"][0]["grid"]["rows_including_header"], 2)


if __name__ == "__main__":
    unittest.main()
