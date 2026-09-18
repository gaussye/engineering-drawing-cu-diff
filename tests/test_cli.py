import argparse
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import pymupdf

from cu_diff.cli import compare, crop


class CropTests(unittest.TestCase):
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
