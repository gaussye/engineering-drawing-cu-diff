"""Synthetic evidence only: no customer files, network, or service dependency."""

from copy import deepcopy
import json
import unittest

from cu_diff.compare import compare_documents, compare_responses, normalize_text, parse_source


def source(x=10, y=10, width=100, height=10, page=1):
    return f"D({page},{x},{y},{x + width},{y},{x + width},{y + height},{x},{y + height})"


def item(text="PN-A/001", key="row 1", region="BOM", category="BOM",
         detail="", evidence=None, confidence=0.99):
    values = {
        name: {"type": "string", "valueString": value}
        for name, value in (
            ("Region", region), ("Category", category), ("Key", key),
            ("RawText", text), ("Detail", detail),
        )
    }
    values["RawText"].update(source=source() if evidence is None else evidence, confidence=confidence)
    return {"type": "object", "valueObject": values}


def line(text, y=10, x=10, height=10, evidence=None, confidence=0.99, page=1):
    return {
        "content": text,
        "source": source(x=x, y=y, height=height, page=page) if evidence is None else evidence,
        "confidence": confidence,
    }


def operation(items=None, lines=None, width=1000, height=1000):
    return {
        "status": "Succeeded",
        "result": {"contents": [{
            "fields": {"Items": {"type": "array", "valueArray": items or []}},
            "pages": [{"pageNumber": 1, "width": width, "height": height,
                       "unit": "pixel", "words": [], "lines": lines or []}],
            "markdown": "Synthetic fixture only",
        }]},
    }


class CompareTests(unittest.TestCase):
    def test_bom_row_swap_pairs_components_instead_of_row_numbers(self):
        old = operation([
            item("1 BRACKET COVER 1 pcs 2.5 g SYN-A1", key="row 1"),
            item("2 TERMINAL BLOCK 2 pcs 4.0 g SYN-B2", key="row 2"),
            item("3 INNER SPACER 1 pcs 1.0 g SYN-C3", key="row 3"),
        ])
        new = operation([
            item("1 INNER SPACER 1 pcs 1.0 g SYN-C3", key="row 1"),
            item("2 BRACKET COVER 1 pcs 2.5 g SYN-A1", key="row 2"),
            item("3 TERMINAL BLOCK 2 pcs 4.0 g SYN-B2", key="row 3"),
        ])
        report = compare_responses(old, new)
        self.assertEqual(len(report["differences"]), 3)
        self.assertEqual(report["coverage"]["schema"]["relocated_pairs"], 3)
        for record in report["differences"]:
            self.assertEqual(record["change"], "relocated")
            self.assertTrue(record["row_relocated"])
            self.assertEqual(record["match"]["method"], "unique_bom_component_role")
            self.assertIn(record["component_role"], record["old"]["raw_text"])
            self.assertIn(record["component_role"], record["new"]["raw_text"])

    def test_bom_relocation_and_material_change_remain_modified(self):
        report = compare_responses(
            operation([item("1 TERMINAL BLOCK 2 pcs 4 g SYN-A1", key="row 1")]),
            operation([item("5 TERMINAL BLOCK 2 pcs 4 g SYN-A2", key="row 5")]))
        record, = report["differences"]
        self.assertEqual(record["change"], "modified")
        self.assertEqual(record["component_role"], "TERMINAL BLOCK")
        self.assertTrue(record["row_relocated"])
        self.assertEqual(record["match"]["method"], "unique_bom_component_role")

    def test_bom_relocation_with_generated_detail_change_is_not_modified(self):
        report = compare_responses(
            operation([item("1 COVER 1 pcs SYN-A", key="row 1", detail="Synthetic location summary")]),
            operation([item("2 COVER 1 pcs SYN-A", key="row 2", detail="Reworded synthetic location summary")]))
        record, = report["differences"]
        self.assertEqual(record["change"], "relocated")
        self.assertTrue(record["row_relocated"])
        self.assertTrue(record["detail_changed"])
        self.assertTrue(record["review_required"])
        self.assertEqual(record["old"]["detail"], "Synthetic location summary")
        self.assertEqual(record["new"]["detail"], "Reworded synthetic location summary")
        self.assertEqual(report["coverage"]["schema"]["relocated_pairs"], 1)
        self.assertEqual(report["coverage"]["schema"]["modified_pairs"], 0)
        self.assertEqual(report["coverage"]["schema"]["interpretation_only_pairs"], 0)

    def test_bom_different_descriptions_cannot_match_on_same_row_or_part(self):
        report = compare_responses(
            operation([item("1 INNER COVER 1 pcs 2 g SYN-SAME", key="row 1")]),
            operation([item("1 OUTER COVER 1 pcs 2 g SYN-SAME", key="row 1")]))
        self.assertEqual(report["coverage"]["schema"]["matched_old"], 0)
        self.assertEqual({record["change"] for record in report["differences"]},
                         {"unpaired_old", "unpaired_new"})

    def test_bom_description_with_weight_delimiter_and_whitespace_only(self):
        report = compare_responses(
            operation([item("1 FRAME   COVER 2.50 g SYN-A", key="row 1")]),
            operation([item("4 FRAME COVER 2.50 g SYN-A", key="row 4")]))
        record, = report["differences"]
        self.assertEqual(record["component_role"], "FRAME COVER")
        self.assertEqual(record["change"], "relocated")
        report = compare_responses(
            operation([item("1 Frame Cover 1 pcs SYN-A", key="row 1")]),
            operation([item("1 FRAME COVER 1 pcs SYN-A", key="row 1")]))
        self.assertEqual(report["coverage"]["schema"]["matched_old"], 0)

    def test_bom_repeated_description_disambiguates_by_full_payload(self):
        report = compare_responses(
            operation([
                item("1 TERMINAL 1 pcs SYN-A", key="row 1"),
                item("2 TERMINAL 1 pcs SYN-B", key="row 2"),
            ]),
            operation([
                item("1 TERMINAL 1 pcs SYN-B", key="row 1"),
                item("2 TERMINAL 1 pcs SYN-A", key="row 2"),
            ]))
        self.assertEqual(len(report["differences"]), 2)
        self.assertTrue(all(record["change"] == "relocated" for record in report["differences"]))
        self.assertTrue(all(record["match"]["method"] == "unique_bom_row_payload"
                            for record in report["differences"]))

    def test_bom_ambiguous_repeated_description_does_not_get_high_certainty(self):
        report = compare_responses(
            operation([
                item("1 TERMINAL 1 pcs SYN-A", key="row 1"),
                item("2 TERMINAL 1 pcs SYN-B", key="row 2"),
            ]),
            operation([
                item("1 TERMINAL 1 pcs SYN-C", key="row 1"),
                item("2 TERMINAL 1 pcs SYN-D", key="row 2"),
            ]))
        self.assertTrue(all(record["match"]["certainty"] != "high" for record in report["differences"]))
        self.assertTrue(all(record["review_required"] for record in report["differences"]))

    def test_content_unit_is_inherited_without_coordinate_conversion(self):
        old = operation([item(evidence="D(1,1,2,3,2,3,4,1,4)")], width=8, height=10)
        content = old["result"]["contents"][0]
        content["unit"] = "inch"
        del content["pages"][0]["unit"]
        report = compare_responses(old, deepcopy(old))
        entry = report["unchanged"][0]["old"]
        self.assertEqual(entry["page_context"], [{
            "page_number": 1, "width": 8, "height": 10, "unit": "inch",
        }])
        self.assertEqual(entry["polygons"][0]["points"], [[1, 2], [3, 2], [3, 4], [1, 4]])

    def test_incompatible_content_units_disable_geometric_matching(self):
        old = operation([item("PART-001", key="row 1")])
        new = operation([item("PART-002", key="row 2")])
        for operation_value, unit in ((old, "inch"), (new, "pixel")):
            content = operation_value["result"]["contents"][0]
            content["unit"] = unit
            del content["pages"][0]["unit"]
        report = compare_responses(old, new)
        self.assertEqual(report["coverage"]["schema"]["matched_old"], 0)
        self.assertEqual(len(report["differences"]), 2)

    def test_explicit_page_unit_takes_precedence_over_content_unit(self):
        old = operation([item()])
        old["result"]["contents"][0]["unit"] = "inch"
        report = compare_responses(old, deepcopy(old))
        self.assertEqual(report["unchanged"][0]["old"]["page_context"][0]["unit"], "pixel")

    def test_word_confidence_does_not_fabricate_line_confidence(self):
        raw_line = line("SYNTHETIC")
        raw_line["span"] = {"offset": 0, "length": 9}
        del raw_line["confidence"]
        old = operation(lines=[raw_line])
        old["result"]["contents"][0]["pages"][0]["words"] = [{
            "content": "SYNTHETIC", "span": {"offset": 0, "length": 9},
            "confidence": 0.99, "source": source(),
        }]
        report = compare_responses(old, deepcopy(old))
        record, = report["ocr_unchanged"]
        self.assertIsNone(record["old"]["confidence"])
        self.assertEqual(record["old"]["raw"]["span"], {"offset": 0, "length": 9})
        self.assertTrue(record["review_required"])
        self.assertIn("old: missing confidence", record["review_reasons"])

    def test_cli_comparison_export(self):
        self.assertIs(compare_responses, compare_documents)
        self.assertEqual(compare_responses(operation(), operation()),
                         compare_documents(operation(), operation()))

    def test_rest_rectangle_source_uses_width_and_height_not_corners(self):
        self.assertEqual(parse_source("D(1, 10, 20, 30, 40)"), [{
            "page_number": 1,
            "points": [[10.0, 20.0], [40.0, 20.0], [40.0, 60.0], [10.0, 60.0]],
        }])
        self.assertEqual(parse_source("D(2,.1,.2,.3,.4)")[0]["points"][1], [0.4, 0.2])
        self.assertEqual(len(parse_source("D(1,10,20,30,40); " + source())), 2)
        for invalid in ("D(1,10,20,-30,40)", "D(1,10,20,30,0)",
                        "D(1,1e308,0,1e308,1)", "D(1,10,20,30,40,50)",
                        "D(1,10,20,30,40)unsupported"):
            with self.subTest(invalid=invalid):
                self.assertEqual(parse_source(invalid), [])

    def test_rest_rectangle_matches_equivalent_polygon_geometry(self):
        report = compare_responses(
            operation(lines=[line("REST evidence", evidence="D(1,10,10,100,10)")]),
            operation(lines=[line("REST evidence")]))
        record, = report["ocr_unchanged"]
        self.assertEqual(record["match"]["method"], "exact_text_geometry")
        self.assertEqual(record["match"]["certainty"], "high")
        self.assertFalse(record["review_required"])
        self.assertEqual(record["old"]["source"], "D(1,10,10,100,10)")

    def test_schema_uncertainties_preserve_every_entry_and_side(self):
        old = operation([item()])
        uncertainty = {
            "type": "string", "valueString": "Small print unreadable",
            "source": "D(1,10,10,100,10)", "confidence": 0.25,
        }
        field = {"type": "array", "valueArray": [uncertainty, deepcopy(uncertainty)],
                 "source": source()}
        old["result"]["contents"][0]["fields"]["Uncertainties"] = field
        before = deepcopy(old)
        report = compare_responses(old, deepcopy(old))
        self.assertEqual(len(report["uncertainties"]), 4)
        self.assertEqual([entry["side"] for entry in report["uncertainties"]],
                         ["old", "old", "new", "new"])
        record = report["uncertainties"][0]
        self.assertEqual(record["text"], "Small print unreadable")
        self.assertEqual(record["raw"], uncertainty)
        self.assertEqual(record["raw_field"], field)
        self.assertEqual(record["polygons"], parse_source(uncertainty["source"]))
        self.assertEqual(record["confidence"], 0.25)
        self.assertTrue(report["review_required"])
        self.assertEqual(report["coverage"]["uncertainty_count"], 4)
        self.assertTrue(any("Small print unreadable" in warning for warning in report["warnings"]))
        self.assertEqual(old, before)
        self.assertEqual(report["unchanged"][0]["match"]["certainty"], "high")
        json.dumps(report, allow_nan=False)

    def test_service_warnings_retained_at_all_levels_even_without_contents(self):
        old = operation()
        repeated = {"code": "SyntheticWarning", "message": "Evidence incomplete", "target": "page 1"}
        old["warnings"] = [repeated, deepcopy(repeated)]
        old["result"]["warnings"] = ["Synthetic result warning"]
        old["result"]["contents"][0]["warnings"] = [{"message": "Synthetic content warning"}]
        old["result"]["contents"][0]["pages"][0]["warnings"] = [{"message": "Synthetic page warning"}]
        new = {"status": "Failed", "warnings": [{"code": "SyntheticFailureWarning"}]}
        report = compare_responses(old, new)
        self.assertEqual(len(report["service_warnings"]), 6)
        self.assertEqual(report["coverage"]["service_warning_count"], 6)
        self.assertEqual(report["service_warnings"][0]["raw"], repeated)
        self.assertEqual(report["service_warnings"][1]["raw"], repeated)
        self.assertEqual(report["service_warnings"][-1]["side"], "new")
        self.assertEqual(report["service_warnings"][0]["path"], "$.warnings[0]")
        self.assertTrue(report["review_required"])
        self.assertTrue(any("Evidence incomplete" in warning for warning in report["warnings"]))
        json.dumps(report, allow_nan=False)

    def test_malformed_uncertainties_are_retained_and_flagged(self):
        for field in (None, {"valueString": "Unexpected shape"},
                      {"valueArray": ["Unreadable", None, {"other": "retained"}]}):
            with self.subTest(field=field):
                old = operation()
                old["result"]["contents"][0]["fields"]["Uncertainties"] = field
                report = compare_responses(old, operation())
                self.assertTrue(report["uncertainties"])
                self.assertTrue(all(entry["malformed"] for entry in report["uncertainties"]))
                self.assertTrue(report["review_required"])
                self.assertEqual(report["uncertainties"][0]["raw_field"], field)

    def test_empty_diagnostics_do_not_require_review(self):
        old = operation([item()])
        old["warnings"] = []
        old["result"]["contents"][0]["fields"]["Uncertainties"] = {"type": "array", "valueArray": []}
        report = compare_responses(old, deepcopy(old))
        self.assertFalse(report["review_required"])
        self.assertEqual(report["uncertainties"], [])
        self.assertEqual(report["service_warnings"], [])

    def test_stable_role_pairs_changed_part_identifier(self):
        report = compare_documents(operation([item()]), operation([item("PN-B/002")]))
        difference, = report["differences"]
        self.assertEqual(difference["change"], "modified")
        self.assertEqual(difference["match"]["method"], "unique_region_category_key")
        self.assertEqual(difference["match"]["certainty"], "high")
        self.assertEqual(difference["old"]["raw_text"], "PN-A/001")
        self.assertEqual(difference["new"]["raw_text"], "PN-B/002")

    def test_normalization_is_only_nfc_and_whitespace(self):
        self.assertEqual(normalize_text("  Cafe\u0301\t X\n"), "Café X")
        report = compare_documents(
            operation([item("Cafe\u0301\t A")]), operation([item("Café A")]))
        self.assertEqual(len(report["unchanged"]), 1)
        for old, new in (("pn-001", "PN-001"), ("A-1", "A1"), ("01", "1"), ("１", "1")):
            with self.subTest(old=old, new=new):
                self.assertEqual(len(compare_documents(
                    operation([item(old)]), operation([item(new)]))["differences"]), 1)

    def test_duplicate_keys_do_not_pair_by_position(self):
        old = operation([item("A", evidence="unknown"), item("B", evidence="unknown")])
        new = operation([item("C", evidence="unknown"), item("D", evidence="unknown")])
        report = compare_documents(old, new)
        self.assertEqual(len(report["differences"]), 4)
        self.assertEqual(report["coverage"]["schema"]["matched_old"], 0)
        self.assertTrue(any("Duplicate schema identity" in warning for warning in report["warnings"]))
        self.assertTrue(all(record["review_required"] for record in report["differences"]))
        self.assertTrue(all(record["change"].startswith("unpaired") for record in report["differences"]))

    def test_duplicate_key_unique_geometry_fallback_is_uncertain(self):
        old = operation([item("PART-001", evidence=source(y=10)), item("PART-002", evidence=source(y=50))])
        new = operation([item("PART-003", evidence=source(y=50)), item("PART-004", evidence=source(y=10))])
        report = compare_documents(old, new)
        self.assertEqual(len(report["differences"]), 2)
        for record in report["differences"]:
            self.assertEqual(record["match"]["method"], "geometry_text")
            self.assertEqual(record["match"]["certainty"], "uncertain")
            self.assertTrue(record["review_required"])
        self.assertEqual(report["differences"][0]["new"]["raw_text"], "PART-004")

    def test_ambiguous_fallback_does_not_cascade(self):
        old = operation([item("same"), item("same")])
        new = operation([item("same"), item("same")])
        report = compare_documents(old, new)
        self.assertEqual(report["coverage"]["schema"]["matched_old"], 0)
        self.assertEqual(len(report["differences"]), 4)

    def test_exact_identity_includes_region_and_category(self):
        for changed in (item(region="packaging"), item(category="label")):
            report = compare_documents(
                operation([item(evidence="unknown")]),
                operation([dict(changed, valueObject={
                    **changed["valueObject"],
                    "RawText": {"valueString": "PN-A/001", "source": "unknown", "confidence": 0.99},
                })]))
            self.assertEqual(len(report["differences"]), 2)

    def test_source_coordinates_are_preserved_without_scaling(self):
        raw_source = "D(2,1.5,2,30,2,30,4.5,1.5,4.5)"
        self.assertEqual(parse_source(raw_source), [{
            "page_number": 2,
            "points": [[1.5, 2.0], [30.0, 2.0], [30.0, 4.5], [1.5, 4.5]],
        }])
        report = compare_documents(
            operation([item(evidence=raw_source)]), operation([item(evidence=raw_source)]))
        self.assertEqual(report["unchanged"][0]["old"]["source"], raw_source)
        self.assertEqual(report["unchanged"][0]["old"]["polygons"], parse_source(raw_source))
        self.assertEqual(len(parse_source(source() + "; " + source(page=2))), 2)

    def test_unsupported_partial_and_invalid_sources_are_not_guessed(self):
        for value in ("unknown", "D(1,1,2)", source() + " garbage",
                      "D(0,1,1,2,1,2,2,1,2)", "D(1,1e999,1,2,1,2,2,1,2)",
                      {"polygon": [1, 2]}, [source(), "unknown"]):
            with self.subTest(source=value):
                self.assertEqual(parse_source(value), [])
                report = compare_documents(operation([item(evidence=value)]), operation([item(evidence=value)]))
                unchanged, = report["unchanged"]
                self.assertTrue(unchanged["review_required"])
                self.assertEqual(unchanged["old"]["source"], value)

    def test_missing_source_or_confidence_surfaces_on_unchanged(self):
        for missing in ("source", "confidence"):
            raw = item()
            del raw["valueObject"]["RawText"][missing]
            report = compare_documents(operation([raw]), operation([raw]))
            unchanged, = report["unchanged"]
            self.assertEqual(unchanged["match"]["certainty"], "high")
            self.assertTrue(unchanged["review_required"])
            self.assertTrue(any(f"missing {missing}" in reason for reason in unchanged["review_reasons"]))

    def test_low_confidence_is_not_match_confidence(self):
        raw = item(confidence=0.3)
        report = compare_documents(operation([raw]), operation([raw]))
        unchanged, = report["unchanged"]
        self.assertEqual(unchanged["match"]["score"], 1)
        self.assertEqual(unchanged["match"]["certainty"], "high")
        self.assertEqual(unchanged["old"]["confidence"], 0.3)
        self.assertTrue(unchanged["review_required"])
        self.assertEqual(report["coverage"]["schema"]["review_required"], 1)

    def test_all_fields_and_raw_evidence_are_preserved(self):
        raw = item(detail="Tolerance: ±0.01")
        raw["valueObject"]["Extra"] = {"valueString": "X", "source": source(y=50), "confidence": 0.1}
        old = operation([raw])
        before = deepcopy(old)
        report = compare_documents(old, deepcopy(old))
        entry = report["unchanged"][0]["old"]
        self.assertEqual(entry["raw"], raw)
        self.assertEqual(entry["detail"], "Tolerance: ±0.01")
        self.assertEqual(entry["field_evidence"]["Extra"]["confidence"], 0.1)
        self.assertTrue(report["unchanged"][0]["review_required"])
        self.assertEqual(old, before)
        json.dumps(report, ensure_ascii=False, allow_nan=False)

    def test_detail_only_changes_are_not_lost(self):
        report = compare_documents(
            operation([item(detail="10 mm")]), operation([item(detail="12 mm")]))
        record, = report["differences"]
        self.assertEqual(record["change"], "interpretation_only")
        self.assertTrue(record["detail_changed"])
        self.assertTrue(record["review_required"])
        self.assertEqual(record["old"]["detail"], "10 mm")
        self.assertEqual(record["new"]["detail"], "12 mm")
        self.assertEqual(report["coverage"]["schema"]["modified_pairs"], 0)
        self.assertEqual(report["coverage"]["schema"]["interpretation_only_pairs"], 1)

    def test_raw_text_change_still_modified_when_detail_also_changes(self):
        report = compare_documents(
            operation([item("SYN-A", detail="Synthetic description")]),
            operation([item("SYN-B", detail="Reworded description")]))
        record, = report["differences"]
        self.assertEqual(record["change"], "modified")
        self.assertTrue(record["detail_changed"])
        self.assertEqual(report["coverage"]["schema"]["modified_pairs"], 1)
        self.assertEqual(report["coverage"]["schema"]["interpretation_only_pairs"], 0)

    def test_generated_key_change_without_raw_text_change_is_not_modified(self):
        report = compare_documents(
            operation([item("SYN-A", key="generated role A")]),
            operation([item("SYN-A", key="generated role B")]))
        record, = report["differences"]
        self.assertEqual(record["change"], "interpretation_only")
        self.assertFalse(record["detail_changed"])
        self.assertTrue(record["review_required"])
        self.assertEqual(report["coverage"]["schema"]["modified_pairs"], 0)

    def test_unpaired_items_are_not_confirmed_additions_or_deletions(self):
        report = compare_documents(operation([item("X")]), operation())
        difference, = report["differences"]
        self.assertEqual(difference["change"], "unpaired_old")
        self.assertIsNone(difference["new"])
        self.assertTrue(difference["review_required"])
        report = compare_documents(operation(), operation([item("X")]))
        self.assertEqual(report["differences"][0]["change"], "unpaired_new")

    def test_dimensions_and_page_numbers_are_not_ignored(self):
        for new in (
            operation([item("PART-002", key="row 2")], width=2000),
            operation([item("PART-002", key="row 2", evidence=source(page=2))]),
        ):
            report = compare_documents(operation([item("PART-001")]), new)
            self.assertEqual(report["coverage"]["schema"]["matched_old"], 0)

    def test_independent_ocr_channel_catches_schema_omission(self):
        report = compare_documents(
            operation([item()], [line("Voltage 220 V")]),
            operation([item()], [line("Voltage 240 V")]))
        self.assertEqual(len(report["unchanged"]), 1)
        self.assertEqual(report["differences"], [])
        difference, = report["ocr_differences"]
        self.assertEqual(difference["change"], "modified")
        self.assertTrue(difference["review_required"])
        self.assertEqual(report["coverage"]["completeness"], "not_guaranteed")

    def test_duplicate_ocr_lines_preserve_multiplicity_with_geometry(self):
        report = compare_documents(
            operation(lines=[line("DUPLICATE", y=10), line("DUPLICATE", y=50)]),
            operation(lines=[line("DUPLICATE", y=50)]))
        self.assertEqual(len(report["ocr_unchanged"]), 1)
        unpaired, = report["ocr_differences"]
        self.assertEqual(unpaired["old"]["raw"]["source"], source(y=10))
        self.assertEqual(unpaired["change"], "unpaired_old")
        self.assertEqual(report["coverage"]["ocr"]["old_total"], 2)
        self.assertEqual(report["coverage"]["ocr"]["matched_old"], 1)

    def test_duplicate_ocr_without_geometry_remains_ambiguous(self):
        report = compare_documents(
            operation(lines=[line("COPY", evidence="unknown"), line("COPY", evidence="unknown")]),
            operation(lines=[line("COPY", evidence="unknown")]))
        self.assertEqual(len(report["ocr_differences"]), 3)
        self.assertEqual(report["ocr_unchanged"], [])

    def test_missing_ocr_confidence_does_not_hide_unchanged(self):
        raw = line("UNCHANGED")
        del raw["confidence"]
        report = compare_documents(operation(lines=[raw]), operation(lines=[raw]))
        unchanged, = report["ocr_unchanged"]
        self.assertTrue(unchanged["review_required"])
        self.assertEqual(unchanged["match"]["certainty"], "high")

    def test_exact_contiguous_split_and_merge(self):
        joined = operation(lines=[line("alpha beta", height=25)])
        split = operation(lines=[line("alpha", y=10), line("beta", y=25)])
        for old, new in ((joined, split), (split, joined)):
            with self.subTest(old_lines=len(old["result"]["contents"][0]["pages"][0]["lines"])):
                report = compare_documents(old, new)
                reconciliation, = report["ocr_differences"]
                self.assertEqual(reconciliation["change"], "reconciled")
                self.assertEqual(reconciliation["match"]["method"], "contiguous_exact_join")
                self.assertTrue(reconciliation["review_required"])
                coverage = report["coverage"]["ocr"]
                self.assertEqual(coverage["matched_old"], coverage["old_total"])
                self.assertEqual(coverage["matched_new"], coverage["new_total"])
                self.assertEqual(coverage["unpaired_old"] + coverage["unpaired_new"], 0)

    def test_split_join_does_not_delete_punctuation_or_join_words(self):
        report = compare_documents(
            operation(lines=[line("AB-CD", height=25)]),
            operation(lines=[line("AB", y=10), line("CD", y=25)]))
        self.assertFalse(any(record["change"] == "reconciled" for record in report["ocr_differences"]))

    def test_split_join_requires_contiguous_same_page_geometry(self):
        joined = operation(lines=[line("alpha beta", height=25)])
        alternatives = [
            [line("alpha", y=10), line("intervening", y=20), line("beta", y=25)],
            [line("alpha", y=10), line("beta", y=500)],
            [line("alpha", y=10), line("beta", y=25, page=2)],
            [line("alpha", y=10), line("beta", y=25, x=500)],
            [line("alpha", evidence="unknown"), line("beta", evidence="unknown")],
        ]
        for lines in alternatives:
            with self.subTest(lines=lines):
                report = compare_documents(joined, operation(lines=lines))
                self.assertFalse(any(record["change"] == "reconciled"
                                     for record in report["ocr_differences"]))
                self.assertTrue(report["ocr_differences"])
                self.assertTrue(all(record["review_required"] for record in report["ocr_differences"]))

    def test_failed_and_missing_extraction_never_claims_completeness(self):
        report = compare_documents(operation([item()]), {"status": "Failed"})
        self.assertEqual(report["differences"][0]["change"], "unpaired_old")
        self.assertTrue(any("not Succeeded" in warning for warning in report["warnings"]))
        self.assertTrue(any("missing result.contents" in warning for warning in report["warnings"]))

    def test_invalid_entries_retained_and_flagged(self):
        report = compare_documents(operation([None], [None]), operation())
        self.assertEqual(report["coverage"]["schema"]["old_total"], 1)
        self.assertEqual(report["coverage"]["ocr"]["old_total"], 1)
        self.assertIsNone(report["differences"][0]["old"]["raw"])
        self.assertTrue(report["differences"][0]["review_required"])
        json.dumps(report, allow_nan=False)

    def test_multiple_contents_keep_all_items_and_ocr(self):
        old = operation([item()], [line("same")])
        old["result"]["contents"].append(deepcopy(old["result"]["contents"][0]))
        report = compare_documents(old, deepcopy(old))
        self.assertEqual(report["coverage"]["schema"]["old_total"], 2)
        self.assertEqual(report["coverage"]["ocr"]["old_total"], 2)
        self.assertEqual(report["coverage"]["schema"]["matched_old"], 2)
        self.assertTrue(all(record["match"]["certainty"] == "uncertain" for record in report["unchanged"]))
        self.assertEqual(len(report["ocr_unchanged"]), 2)

    def test_threshold_validation(self):
        for invalid in (-0.1, 1.1, float("nan"), "0.8", True):
            with self.subTest(threshold=invalid):
                with self.assertRaises(ValueError):
                    compare_documents(operation(), operation(), confidence_threshold=invalid)


if __name__ == "__main__":
    unittest.main()
