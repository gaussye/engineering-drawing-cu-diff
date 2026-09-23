import importlib.util
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import pymupdf

if importlib.util.find_spec("flask") is None:
    raise unittest.SkipTest("Install .[web] to run local web tests")

from cu_diff.client import CUError
from cu_diff.web import MAX_BYTES, SESSION_TTL, create_app
from cu_diff.web_evidence import locations


BASE = "http://127.0.0.1:8765"


def pdf_bytes(text="SYNTHETIC A", pages=1, rotation=0):
    with pymupdf.open() as pdf:
        for index in range(pages):
            page = pdf.new_page(width=720, height=360)
            page.insert_text((72, 100), f"{text} PAGE {index + 1}")
            page.draw_rect(pymupdf.Rect(72, 72, 144, 108), color=(1, 0, 0))
            page.set_rotation(rotation)
        return pdf.tobytes()


def operation(text, pages=1):
    fields = {
        "Region": {"valueString": "title"}, "Category": {"valueString": "title"},
        "Key": {"valueString": "synthetic label"}, "Detail": {"valueString": "synthetic"},
        "RawText": {"valueString": text, "confidence": 0.91,
                    "source": f"D({pages},1,1,2,1,2,1.5,1,1.5)"},
    }
    return {"status": "Succeeded", "result": {"contents": [{
        "unit": "inch", "fields": {"Items": {"valueArray": [{"valueObject": fields}]}},
        "pages": [{"pageNumber": i + 1, "width": 10, "height": 5, "lines": []}
                  for i in range(pages)],
    }]}}


class FakeClient:
    gate = None
    fail = False

    def __init__(self, config):
        self.events = []

    def ensure_analyzer(self, *, allow_create=True):
        if allow_create:
            raise AssertionError("Web must not create analyzers")
        return "synthetic", {}

    def analyze(self, path, cache, identifier, analyzer, *, allow_submit=True):
        if self.gate is not None:
            self.gate.wait(3)
        if self.fail:
            raise CUError("Synthetic CU failure")
        with pymupdf.open(path) as pdf:
            text = pdf[0].get_text()
            pages = len(pdf)
        return operation(text, pages), {
            "cache_hit": True, "usage": {"documentPagesStandard": pages},
            "selected_completion_model": "synthetic",
        }


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = create_app({"completion_model": "synthetic"},
                              self.root / "data", self.root / "cache",
                              client_factory=FakeClient)
        self.client = self.app.test_client()
        self.boot = self.client.get("/api/bootstrap", base_url=BASE).get_json()
        self.headers = {"X-CSRF-Token": self.boot["csrf_token"], "Origin": BASE}
        FakeClient.gate, FakeClient.fail = None, False

    def tearDown(self):
        if FakeClient.gate:
            FakeClient.gate.set()
        self.app.extensions["review_store"].executor.shutdown(wait=True)
        self.temp.cleanup()

    def upload(self, role, data=None, name="synthetic.pdf"):
        return self.client.put(f"/api/documents/{role}", data=data or pdf_bytes(),
                               content_type="application/pdf", base_url=BASE,
                               headers=self.headers | {"X-Filename": name})

    def compare(self, revision):
        return self.client.post("/api/compare", json={"revision": revision},
                                base_url=BASE, headers=self.headers)

    def wait_job(self, identifier):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/jobs/{identifier}", base_url=BASE)
            body = response.get_json()
            if body["status"] not in ("running", "queued"):
                return body
            time.sleep(0.01)
        self.fail("Job did not complete")

    def test_immediate_preview_without_analysis(self):
        result = self.upload("old", pdf_bytes(pages=2))
        self.assertEqual(result.status_code, 200)
        doc = result.get_json()["document"]
        self.assertEqual(doc["page_count"], 2)
        image = self.client.get(f"/api/documents/{doc['id']}/pages/2?width=600", base_url=BASE)
        self.assertEqual(image.status_code, 200)
        self.assertTrue(image.data.startswith(b"\x89PNG"))
        self.assertEqual(len(self.app.extensions["review_store"].jobs), 0)
        self.assertEqual(image.headers["Cache-Control"], "no-store")

    def test_jobs_return_two_sided_multipage_coordinates(self):
        self.upload("old", pdf_bytes("SYNTHETIC A", pages=2))
        revision = self.upload("new", pdf_bytes("SYNTHETIC B", pages=2)).get_json()["revision"]
        job = self.compare(revision)
        self.assertEqual(job.status_code, 202)
        result = self.wait_job(job.get_json()["job_id"])
        self.assertEqual(result["status"], "succeeded")
        item = result["result"]["items"][0]
        self.assertEqual(item["change"], "modified")
        for role in ("old", "new"):
            self.assertEqual(item[role]["locations"][0]["page"], 2)
            self.assertAlmostEqual(item[role]["locations"][0]["x"], 0.1)
            self.assertAlmostEqual(item[role]["locations"][0]["y"], 0.2)
        self.assertNotIn("protected_paths", result)
        self.assertEqual(result["result"]["graphics_coverage"]["status"], "completed")
        self.assertEqual(result["result"]["graphics_coverage"]["fallback_pages"], 2)

    def test_bootstrap_reports_actual_graphical_pipeline(self):
        self.assertIs(self.boot["graphics_enabled"], True)
        self.assertIs(self.boot["model_comparison_enabled"], False)

    def test_model_stage_is_additive_and_passes_explicit_upload_permission(self):
        store = self.app.extensions["review_store"]
        store.model_options["enabled"] = True
        self.upload("old", pdf_bytes("SYNTHETIC A"))
        revision = self.upload("new", pdf_bytes("SYNTHETIC B")).get_json()["revision"]
        candidate = {"id": "M001", "channel": "model", "change": "model_review"}
        semantic = {"items": [candidate], "coverage": {"enabled": True, "unprocessed": 1},
                    "warnings": ["Synthetic model coverage remains partial."]}
        with patch("cu_diff.model_compare.compare_with_model", return_value=semantic) as call:
            result = self.wait_job(self.compare(revision).get_json()["job_id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertIn(candidate, result["result"]["items"])
        self.assertTrue(any(i["channel"] == "schema" for i in result["result"]["items"]))
        self.assertTrue(result["result"]["graphics_coverage"])
        self.assertEqual(result["result"]["model_coverage"], semantic["coverage"])
        self.assertIn(semantic["warnings"][0], result["result"]["warnings"])
        self.assertFalse(call.call_args.kwargs["allow_submit"])

    def test_model_stage_failure_never_silently_falls_back_to_rules(self):
        self.app.extensions["review_store"].model_options["enabled"] = True
        self.upload("old", pdf_bytes("SYNTHETIC A"))
        revision = self.upload("new", pdf_bytes("SYNTHETIC B")).get_json()["revision"]
        with patch("cu_diff.model_compare.compare_with_model", side_effect=CUError("Synthetic model failure")):
            result = self.wait_job(self.compare(revision).get_json()["job_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("Synthetic model failure", result["error"])
        self.assertNotIn("result", result)

    def test_table_pipeline_preserves_items_diagnostics_and_coverage(self):
        self.upload("old", pdf_bytes("SYNTHETIC A"))
        revision = self.upload("new", pdf_bytes("SYNTHETIC B")).get_json()["revision"]
        item = {"id": "T001", "channel": "tables", "change": "table_column_added",
                "old": None, "new": {"raw_text": "SYNTHETIC", "locations": []}}
        table_result = {"items": [item], "coverage": {"status": "completed", "matched_tables": 1},
                        "warnings": ["Synthetic table requires source review."]}
        with patch("cu_diff.document_tables.compare_document_tables", return_value=table_result) as compare:
            job_id = self.compare(revision).get_json()["job_id"]
            result = self.wait_job(job_id)
        self.assertEqual(result["status"], "succeeded")
        self.assertIn(item, result["result"]["items"])
        self.assertEqual(result["result"]["table_coverage"], table_result["coverage"])
        self.assertIn(table_result["warnings"][0], result["result"]["warnings"])
        self.assertTrue(result["result"]["graphics_coverage"])
        compare.assert_called_once()
        self.assertEqual(len(compare.call_args.args), 4)

    def test_table_stage_failure_cannot_silently_return_no_table_differences(self):
        self.upload("old", pdf_bytes("SYNTHETIC A"))
        revision = self.upload("new", pdf_bytes("SYNTHETIC B")).get_json()["revision"]
        with patch("cu_diff.document_tables.compare_document_tables", side_effect=ValueError("Synthetic table failure")):
            job_id = self.compare(revision).get_json()["job_id"]
            result = self.wait_job(job_id)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Synthetic table failure", result["error"])
        self.assertNotIn("result", result)

    def test_graphics_stage_failure_is_explicit_not_a_success_result(self):
        from cu_diff.graphics import GraphicsError
        self.upload("old", pdf_bytes("SYNTHETIC A"))
        revision = self.upload("new", pdf_bytes("SYNTHETIC B")).get_json()["revision"]
        with patch("cu_diff.graphics.compare_graphics", side_effect=GraphicsError("Synthetic graphical failure")):
            job_id = self.compare(revision).get_json()["job_id"]
            result = self.wait_job(job_id)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Synthetic graphical failure", result["error"])
        self.assertNotIn("result", result)

    def test_replace_invalidates_result_and_old_file(self):
        self.upload("old")
        new_doc = self.upload("new", pdf_bytes("SYNTHETIC B")).get_json()
        job_id = self.compare(new_doc["revision"]).get_json()["job_id"]
        self.assertEqual(self.wait_job(job_id)["status"], "succeeded")
        self.upload("new", pdf_bytes("SYNTHETIC C"))
        state = self.client.get(f"/api/jobs/{job_id}", base_url=BASE).get_json()
        self.assertEqual(state["status"], "stale")
        self.assertNotIn("result", state)
        self.assertEqual(self.client.get(
            f"/api/documents/{new_doc['document']['id']}/pages/1", base_url=BASE).status_code, 404)

    def test_replacement_during_job_never_publishes_stale_results(self):
        FakeClient.gate = threading.Event()
        self.upload("old")
        revision = self.upload("new", pdf_bytes("SYNTHETIC B")).get_json()["revision"]
        job_id = self.compare(revision).get_json()["job_id"]
        self.upload("old", pdf_bytes("SYNTHETIC C"))
        FakeClient.gate.set()
        state = self.wait_job(job_id)
        self.assertEqual(state["status"], "stale")
        self.assertNotIn("result", state)

    def test_failure_is_visible(self):
        FakeClient.fail = True
        self.upload("old")
        revision = self.upload("new").get_json()["revision"]
        job_id = self.compare(revision).get_json()["job_id"]
        state = self.wait_job(job_id)
        self.assertEqual(state["status"], "failed")
        self.assertIn("Synthetic CU failure", state["error"])
        self.assertNotIn("result", state)

    def test_missing_files_and_stale_revision(self):
        self.assertEqual(self.compare(0).status_code, 400)
        self.upload("old")
        self.upload("new")
        self.assertEqual(self.compare(0).status_code, 409)

    def test_identical_bytes_are_rejected_before_any_cu_call(self):
        data = pdf_bytes()
        self.upload("old", data, name="old.pdf")
        revision = self.upload("new", data, name="different-name.pdf").get_json()["revision"]
        store = self.app.extensions["review_store"]
        store.client_factory = Mock(side_effect=AssertionError("CU must not be called"))
        response = self.compare(revision)
        self.assertEqual(response.status_code, 409)
        self.assertIn("SHA256相同", response.get_json()["error"])
        store.client_factory.assert_not_called()
        self.assertEqual(store.jobs, {})

    def test_host_origin_csrf_and_session_isolation(self):
        self.assertEqual(self.client.get("/api/health", base_url="http://evil.test:8765").status_code, 403)
        self.assertEqual(self.client.put("/api/documents/old", data=pdf_bytes(),
                                        content_type="application/pdf", base_url=BASE).status_code, 403)
        bad = self.headers | {"Origin": "https://evil.test"}
        self.assertEqual(self.client.post("/api/compare", json={"revision": 0},
                                         base_url=BASE, headers=bad).status_code, 403)
        doc = self.upload("old").get_json()["document"]
        other = self.app.test_client()
        other.get("/api/bootstrap", base_url=BASE)
        self.assertEqual(other.get(f"/api/documents/{doc['id']}/pages/1",
                                   base_url=BASE).status_code, 404)

    def test_content_validation_and_safe_name(self):
        self.assertEqual(self.upload("old", b"not a pdf").status_code, 400)
        self.assertEqual(self.upload("old", b"%PDF-broken").status_code, 400)
        self.assertEqual(self.upload("old", pdf_bytes(pages=21)).status_code, 400)
        self.assertEqual(self.upload("old", b"%PDF-" + b"x" * MAX_BYTES).status_code, 413)
        doc = self.upload("old", name="..%2F..%2Foutside.pdf").get_json()["document"]
        self.assertEqual(doc["name"], "outside.pdf")
        self.assertFalse((self.root / "outside.pdf").exists())
        self.assertEqual(self.client.get(f"/api/documents/{doc['id']}/pages/0",
                                        base_url=BASE).status_code, 404)
        self.assertEqual(self.client.get(f"/api/documents/{doc['id']}/pages/1?width=99999",
                                        base_url=BASE).status_code, 400)

    def test_rotated_pdf_is_normalized_without_visual_change(self):
        for rotation in (90, 180, 270):
            with self.subTest(rotation=rotation):
                raw = pdf_bytes(rotation=rotation)
                doc = self.upload("old", raw).get_json()["document"]
                self.assertTrue(doc["rotation_normalized"])
                self.assertEqual(doc["pages"][0]["width_pt"], 720 if rotation == 180 else 360)
                self.assertEqual(doc["pages"][0]["height_pt"], 360 if rotation == 180 else 720)
                store = self.app.extensions["review_store"]
                stored = next(iter(store.sessions.values())).documents["old"]
                self.assertEqual(stored.path.read_bytes(), raw)
                with pymupdf.open(stream=raw) as original, pymupdf.open(stored.analysis_path) as normalized:
                    self.assertEqual(normalized[0].rotation, 0)
                    self.assertEqual(original[0].get_pixmap().samples, normalized[0].get_pixmap().samples)

    def test_password_protected_pdf_is_rejected(self):
        with pymupdf.open(stream=pdf_bytes()) as pdf:
            encrypted = pdf.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256,
                                   owner_pw="synthetic-owner", user_pw="synthetic-user")
        self.assertEqual(self.upload("old", encrypted).status_code, 400)

    def test_repeated_rotation_normalization_is_byte_stable(self):
        raw = pdf_bytes(rotation=270)
        self.upload("old", raw)
        store = self.app.extensions["review_store"]
        session = next(iter(store.sessions.values()))
        first = session.documents["old"].analysis_path.read_bytes()
        self.upload("old", raw)
        second = session.documents["old"].analysis_path.read_bytes()
        self.assertEqual(first, second)

    def test_rotated_original_cache_is_reused_when_geometry_matches(self):
        self.upload("old", pdf_bytes(rotation=270))
        store = self.app.extensions["review_store"]
        document = next(iter(store.sessions.values())).documents["old"]
        response = operation("SYNTHETIC")
        response["result"]["contents"][0]["pages"][0].update(width=5, height=10)
        client = Mock()
        client.analyze.return_value = (response, {"cache_hit": True})
        _, metadata = store.analyze_document(client, document, "synthetic", {})
        client.analyze.assert_called_once()
        self.assertEqual(client.analyze.call_args.args[0], document.path)
        self.assertFalse(client.analyze.call_args.kwargs["allow_submit"])
        self.assertEqual(metadata["coordinate_basis"], "original-cache-displayed-page")

    def test_rotated_original_cache_requires_matching_geometry(self):
        self.upload("old", pdf_bytes(rotation=270))
        store = self.app.extensions["review_store"]
        document = next(iter(store.sessions.values())).documents["old"]
        client = Mock()
        client.analyze.return_value = (operation("SYNTHETIC"), {"cache_hit": True})
        _, metadata = store.analyze_document(client, document, "synthetic", {})
        self.assertEqual(client.analyze.call_count, 2)
        self.assertEqual(client.analyze.call_args.args[0], document.analysis_path)
        self.assertEqual(metadata["coordinate_basis"], "rotation-normalized-page")

    def test_rotation_cache_requires_pixel_equivalence_not_only_size(self):
        self.upload("old", pdf_bytes(rotation=270))
        store = self.app.extensions["review_store"]
        document = next(iter(store.sessions.values())).documents["old"]
        document.pages[0]["display_equivalent"] = False
        response = operation("SYNTHETIC")
        response["result"]["contents"][0]["pages"][0].update(width=5, height=10)
        client = Mock()
        client.analyze.return_value = (response, {"cache_hit": True})
        store.analyze_document(client, document, "synthetic", {})
        self.assertEqual(client.analyze.call_count, 2)
        self.assertEqual(client.analyze.call_args.args[0], document.analysis_path)
    def test_delete_and_cleanup_expired_session(self):
        self.upload("old")
        store = self.app.extensions["review_store"]
        session = next(iter(store.sessions.values()))
        removed = self.client.delete("/api/documents/old", base_url=BASE, headers=self.headers)
        self.assertEqual(removed.status_code, 200)
        self.assertFalse(list(session.directory.glob("*.pdf")))
        session.last_used = time.time() - SESSION_TTL - 10
        store.cleanup()
        self.assertNotIn(session.id, store.sessions)


class GeometryTests(unittest.TestCase):
    def test_only_evidence_side_has_frames(self):
        pages = [{"number": 1, "width_pt": 720, "height_pt": 360}]
        boxes, error = locations(None, pages)
        self.assertEqual(boxes, [])
        self.assertIn("无对应", error)

    def test_missing_or_incompatible_source_has_no_invented_box(self):
        pages = [{"number": 1, "width_pt": 720, "height_pt": 360}]
        self.assertEqual(locations({}, pages)[0], [])
        entry = {"polygons": [{"page_number": 1, "points": [[1, 1], [2, 1], [2, 2], [1, 2]]}],
                 "page_context": [{"page_number": 1, "unit": "inch", "width": 5, "height": 10}]}
        boxes, error = locations(entry, pages)
        self.assertEqual(boxes, [])
        self.assertIn("不符", error)

    def test_out_of_bounds_and_multipage(self):
        pages = [{"number": 2, "width_pt": 720, "height_pt": 360}]
        entry = {"polygons": [{"page_number": 2, "points": [[1, 1], [2, 1], [2, 2], [1, 2]]}],
                 "page_context": [{"page_number": 2, "unit": "inch", "width": 10, "height": 5}]}
        boxes, error = locations(entry, pages)
        self.assertIsNone(error)
        self.assertEqual(boxes[0]["page"], 2)
        entry["polygons"][0]["points"][0] = [-1, 1]
        self.assertEqual(locations(entry, pages)[0], [])


if __name__ == "__main__":
    unittest.main()
