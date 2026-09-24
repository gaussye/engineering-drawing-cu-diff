import argparse
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from cu_diff.cli import compare, crop, extract
from cu_diff.client import Client


class CropTests(unittest.TestCase):
    def test_extract_parallelizes_only_cu_and_preserves_outputs_and_usage(self):
        from tests.test_analysis_options import config
        rendezvous = threading.Barrier(2)

        class SyntheticClient(Client):
            def ensure_analyzer(self):
                return "synthetic", {}

            def request(self, method, url, body=None, extra_headers=None):
                if method != "POST":
                    raise AssertionError("Unexpected synthetic operation")
                return {}, {"Operation-Location": self.url("analyzerResults/synthetic")}

            def poll(self, url, *, usage_entry=None):
                rendezvous.wait(timeout=5)
                return {"status": "Succeeded", "usage": {"documentPagesStandard": 1},
                        "result": {"contents": [{"pages": []}]}}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            paths = {}
            for role in ("old", "new"):
                paths[role] = root / f"{role}.pdf"
                with pymupdf.open() as pdf:
                    pdf.new_page().insert_text((40, 50), role)
                    pdf.save(paths[role])
            before = {role: path.read_bytes() for role, path in paths.items()}
            output = root / "output"
            with patch("cu_diff.cli.Client", SyntheticClient):
                extract(argparse.Namespace(**paths, config=config_path, output=output,
                                           allow_azure_upload=True))
            for role, path in paths.items():
                self.assertEqual(path.read_bytes(), before[role])
                self.assertEqual(json.loads((output / f"{role}.response.json").read_text())["status"],
                                 "Succeeded")
                self.assertTrue((output / f"{role}.inspection.json").exists())
            report = json.loads((output / "usage-cost.json").read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["requests"]["new"], 2)
            self.assertEqual(len(report["entries"]), 2)

    def test_failed_extraction_does_not_create_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            failed = root / "failed.json"
            failed.write_text('{"status":"Failed","error":{"code":"Synthetic"}}', encoding="utf-8")
            output = root / "output"
            with self.assertRaisesRegex(ValueError, "did not succeed"):
                compare(argparse.Namespace(old=failed, new=failed, output=output))
            self.assertFalse(output.exists())

    def test_crop_mapping_and_read_only_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "synthetic.pdf"
            with pymupdf.open() as document:
                document.new_page(width=300, height=200).insert_text((40, 50), "SYNTHETIC ONLY")
                document.save(source)
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            output = root / "crop.pdf"
            args = argparse.Namespace(pdf=source, output=output, page=1,
                                      rect=[20, 30, 200, 150], dpi=300)
            crop(args)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            mapping = json.loads(output.with_suffix(".mapping.json").read_text(encoding="utf-8"))
            self.assertEqual(mapping["crop_pt"], [20, 30, 200, 150])
            self.assertEqual(mapping["source_sha256"], before)
            with pymupdf.open(output) as document:
                self.assertEqual(document[0].rect.width, 180)
                self.assertEqual(document[0].rect.height, 120)
            args.output = source
            with self.assertRaisesRegex(ValueError, "overwrite"):
                crop(args)
            args.output = output
            args.page = 0
            with self.assertRaisesRegex(ValueError, "1-based"):
                crop(args)


if __name__ == "__main__":
    unittest.main()
