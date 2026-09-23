"""Unchanged BOM text is not proof an unpaired drawing callout was deleted."""

from copy import deepcopy
import unittest

from cu_diff.compare import compare_documents
from test_compare import item, line, operation, source


def fixture(callout):
    bom_source = source(x=500, y=100, width=300)
    callout_source = source(x=100, y=500)
    entries = [item("12 LABEL 8A 1 pcs SYN-ID", evidence=bom_source)]
    lines = [line("LABEL 8A", evidence=source(x=520, y=100))]
    if callout:
        entries.append(item("LABEL 8A", key="callout", category="label", region="cable",
                            evidence=callout_source))
        lines.append(line("LABEL 8A", evidence=callout_source))
    return operation(entries, lines)


class AnnotationTests(unittest.TestCase):
    def test_occurrence_review_retains_absence_uncertainty_and_bom_context(self):
        old, new = fixture(True), fixture(False)
        before = deepcopy((old, new))
        result = compare_documents(old, new)
        record, = [r for r in result["differences"] if r["change"] == "annotation_occurrence_changed"]
        self.assertIsNone(record["new"])
        self.assertEqual(record["old"]["raw_text"], "LABEL 8A")
        self.assertTrue(record["review_required"])
        self.assertEqual(record["match"]["certainty"], "uncertain")
        self.assertEqual(record["annotation_comparison"]["counts_outside_bom"], {"old": 1, "new": 0})
        self.assertEqual(record["annotation_comparison"]["counts_including_bom"], {"old": 2, "new": 1})
        self.assertEqual(record["annotation_context"]["new"]["raw_text"], "12 LABEL 8A 1 pcs SYN-ID")
        self.assertEqual((old, new), before)

    def test_reverse_direction_and_moved_same_label(self):
        result = compare_documents(fixture(False), fixture(True))
        record, = [r for r in result["differences"] if r["change"] == "annotation_occurrence_changed"]
        self.assertIsNone(record["old"])
        self.assertEqual(record["annotation_comparison"]["counts_outside_bom"], {"old": 0, "new": 1})
        result = compare_documents(fixture(True), fixture(True))
        self.assertFalse(any(r["change"] == "annotation_occurrence_changed" for r in result["differences"]))

    def test_missing_ocr_bom_changed_or_ambiguous_occurrences_do_not_upgrade(self):
        for problem in ("no_ocr", "changed_bom", "duplicate", "missing_source", "failed"):
            old, new = fixture(True), fixture(False)
            content = new["result"]["contents"][0]
            if problem == "no_ocr":
                content["pages"][0]["lines"] = []
            elif problem == "changed_bom":
                content["fields"]["Items"]["valueArray"][0]["valueObject"]["RawText"]["valueString"] = "12 LABEL 8A 2 pcs SYN-ID"
            elif problem == "duplicate":
                old["result"]["contents"][0]["pages"][0]["lines"].append(
                    line("LABEL 8A", evidence=source(x=200, y=600)))
            elif problem == "missing_source":
                content["pages"][0]["lines"][0]["source"] = None
            else:
                new["status"] = "Failed"
            result = compare_documents(old, new)
            self.assertFalse(any(r["change"] == "annotation_occurrence_changed" for r in result["differences"]), problem)


if __name__ == "__main__":
    unittest.main()
