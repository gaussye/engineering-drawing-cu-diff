"""Synthetic packaging tests. Never read customer config or contact Azure."""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "package_appservice", ROOT / "deploy" / "package_appservice.py"
)
packaging = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packaging)


class DeploymentPackageTests(unittest.TestCase):
    def setUp(self):
        # Scratch data remains inside the ignored project-local directory.
        self.scratch = ROOT / "local" / ("package-test-" + uuid.uuid4().hex)
        self.source = self.scratch / "source"
        self.source.mkdir(parents=True)
        self.add(
            "pyproject.toml",
            '[build-system]\nrequires = ["setuptools>=68"]\n'
            'build-backend = "setuptools.build_meta"\n'
            '[project]\nname = "synthetic"\nversion = "0.1.0"\n'
            '[project.optional-dependencies]\nappservice = ["Flask", "azure-identity"]\n',
        )
        self.add("cu_diff/__init__.py", "")
        self.add("cu_diff/appservice.py", "print('synthetic')\n")
        self.add("cu_diff/web.py", "TEST = True\n")
        self.add("cu_diff/static/index.html", "<main>synthetic</main>")
        self.add("cu_diff/static/app.css", "main {}")
        self.add("cu_diff/static/app.js", "'use strict';")
        self.add("cu_diff/pricing/synthetic.json", '{"rates": []}')
        self.output = self.scratch / "app.zip"

    def tearDown(self):
        shutil.rmtree(self.scratch)

    def add(self, relative, content):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_manifest_contains_only_source_assets_and_install_requirements(self):
        expected = {
            "pyproject.toml", "requirements.txt", "cu_diff/__init__.py",
            "cu_diff/appservice.py", "cu_diff/web.py",
            "cu_diff/static/index.html", "cu_diff/static/app.css",
            "cu_diff/static/app.js", "cu_diff/pricing/synthetic.json",
        }
        manifest = packaging.build_package(self.source, self.output)
        self.assertEqual(set(manifest), expected)
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(set(archive.namelist()), expected)
            self.assertEqual(archive.read("requirements.txt"), b".[appservice]\n")
            self.assertEqual(archive.read("cu_diff/static/app.js"), b"'use strict';")
            self.assertEqual(archive.testzip(), None)

    def test_secrets_customer_artifacts_and_unlisted_types_are_excluded(self):
        private = [
            ".env", ".env.production", "local/config.json", "local/customer.pdf",
            ".git/config", ".venv/site-packages/secret.py", "build/copied.py",
            "tests/test_secret.py", "config.example.json", "requirements.txt",
            "cu_diff/credentials.json", "cu_diff/.secret.py", "cu_diff/customer.pdf",
            "cu_diff/customer.png", "cu_diff/uploads/secret.py",
            "cu_diff/cache/secret.py", "cu_diff/__pycache__/module.pyc",
            "cu_diff/static/customer.jpg", "cu_diff/static/customer.svg",
            "cu_diff/static/config.json", "cu_diff/static/.secret.js",
            "cu_diff/static/uploads/customer.html",
            "cu_diff/pricing/.secret.json", "cu_diff/pricing/local/secret.json",
        ]
        for name in private:
            self.add(name, "SYNTHETIC_SECRET_SENTINEL")
        packaging.build_package(self.source, self.output)
        with zipfile.ZipFile(self.output) as archive:
            for name in archive.namelist():
                self.assertNotIn(b"SYNTHETIC_SECRET_SENTINEL", archive.read(name), name)
            self.assertEqual(archive.read("requirements.txt"), b".[appservice]\n")

    def test_reproducible_zip_independent_of_source_timestamps(self):
        packaging.build_package(self.source, self.output)
        first = self.output.read_bytes()
        for source in self.source.rglob("*"):
            os.utime(source, (1_700_000_000, 1_700_000_000))
        second = self.scratch / "again.zip"
        packaging.build_package(self.source, second)
        self.assertEqual(first, second.read_bytes())

    def test_existing_archive_not_overwritten(self):
        self.output.write_bytes(b"preserve")
        with self.assertRaises(FileExistsError):
            packaging.build_package(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), b"preserve")

    def test_missing_entrypoint_rejected_before_writing(self):
        (self.source / "cu_diff/appservice.py").unlink()
        with self.assertRaisesRegex(ValueError, "Missing required"):
            packaging.build_package(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_appservice_extra_rejected(self):
        self.add("pyproject.toml", '[project]\nname = "synthetic"\n')
        with self.assertRaisesRegex(ValueError, "appservice extra"):
            packaging.build_package(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_invalid_pricing_json_rejected(self):
        self.add("cu_diff/pricing/broken.json", "{")
        with self.assertRaises(ValueError):
            packaging.build_package(self.source, self.output)

    def link(self, target, path, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except OSError as error:
            self.skipTest(f"Symlink creation unavailable: {error}")

    def test_symlink_source_file_rejected(self):
        target = self.add("local/private.py", "SECRET")
        self.link(target, self.source / "cu_diff/linked.py")
        with self.assertRaisesRegex(ValueError, "Linked package entry"):
            packaging.build_package(self.source, self.output)

    def test_unlisted_symlink_in_package_rejected(self):
        target = self.add("local/private.json", "SECRET")
        self.link(target, self.source / "cu_diff/secret.json")
        with self.assertRaises(ValueError):
            packaging.build_package(self.source, self.output)

    def test_symlink_package_directory_rejected(self):
        static = self.source / "cu_diff/static"
        target = self.source / "other-static"
        static.rename(target)
        self.link(target, static, directory=True)
        with self.assertRaisesRegex(ValueError, "Linked package"):
            packaging.build_package(self.source, self.output)

    def test_symlink_root_rejected(self):
        linked = self.scratch / "linked"
        self.link(self.source, linked, directory=True)
        with self.assertRaisesRegex(ValueError, "Source root"):
            packaging.build_package(linked, self.output)

    def test_symlink_output_parent_rejected(self):
        target = self.scratch / "elsewhere"
        target.mkdir()
        linked = self.scratch / "linked-output"
        self.link(target, linked, directory=True)
        with self.assertRaisesRegex(ValueError, "Output"):
            packaging.build_package(self.source, linked / "app.zip")
        self.assertFalse((target / "app.zip").exists())

    def test_reparse_detection_without_symlink_privileges(self):
        class ReparseStat:
            st_file_attributes = 0x400

        with patch.object(Path, "lstat", return_value=ReparseStat()), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "is_symlink", return_value=False):
            self.assertTrue(packaging._linked(Path("synthetic-junction")))

    def test_link_rejection_without_symlink_privileges(self):
        linked = self.source / "cu_diff" / "web.py"
        original = packaging._linked
        with patch.object(packaging, "_linked", side_effect=lambda path: path == linked or original(path)):
            with self.assertRaisesRegex(ValueError, "Linked package entry"):
                packaging.build_package(self.source, self.output)
        self.assertFalse(self.output.exists())


@unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 is unavailable")
class DeploymentQuotaTests(unittest.TestCase):
    def test_quota_preflight_fails_closed_without_azure_calls(self):
        script = ROOT / "deploy" / "Deploy-AppService.ps1"
        command = r"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $args[0], [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Deployment script syntax error' }
$function = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Assert-BasicQuota'
}, $true)
if (-not $function) { throw 'Quota validator missing' }
Invoke-Expression $function.Extent.Text
function az { throw 'Azure must not be called in quota unit tests' }
$valid = @{ properties = @{ name = @{ value = 'B2' }; limit = @{ value = 1 }; currentValue = -1 } }
Assert-BasicQuota @($valid)
$invalid = @(
    @(),
    @(@{ properties = @{ name = @{ value = 'B1' }; limit = @{ value = 2 } } }),
    @(@{ properties = @{ name = @{ value = 'B2' }; limit = @{ value = 0 } } }),
    @(@{ properties = @{ name = @{ value = 'B2' }; limit = @{ value = -1 } } }),
    @(@{ properties = @{ name = @{ value = 'B2' }; limit = @{ value = 'unknown' } } }),
    @(@{ properties = @{ name = @{ value = 'B2' } } }),
    @($valid, $valid)
)
foreach ($case in $invalid) {
    $rejected = $false
    try { Assert-BasicQuota $case } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid quota accepted' }
}
Write-Output 'quota preflight passed'
"""
        # Only the validator AST is loaded, never the deployment script body.
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command",
             "& { " + command + " } '" + str(script).replace("'", "''") + "'"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("quota preflight passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
