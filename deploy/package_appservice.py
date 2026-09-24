"""Build a deterministic, source-only App Service ZIP without local configuration."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import tomllib
import zipfile


ASSETS = {"static": {".html", ".css", ".js"}, "pricing": {".json"}}
REQUIREMENTS = b".[appservice]\n"
REQUIRED = {
    "pyproject.toml",
    "cu_diff/__init__.py",
    "cu_diff/appservice.py",
    "cu_diff/static/index.html",
    "cu_diff/static/app.css",
    "cu_diff/static/app.js",
}


def _linked(path: Path) -> bool:
    if path.is_symlink():
        return True
    if not path.exists():
        return False
    return bool(
        getattr(path.lstat(), "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _regular(path: Path) -> None:
    info = path.lstat()
    if path.is_symlink() or (
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    ):
        raise ValueError(f"Symlinks/reparse points are forbidden: {path.name}")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Not a regular file: {path.name}")


def package_files(root: Path) -> dict[str, bytes]:
    """Allow only package-root Python, known static types, and pricing JSON.

    No recursive extension-only scan: a Python file in a customer/upload/cache
    directory must not become deployable code merely because of its extension.
    """
    root = Path(os.path.abspath(root))
    for parent in (root, *root.parents):
        if _linked(parent):
            raise ValueError("Source root must not traverse links or junctions")
    files: dict[str, bytes] = {}
    candidates = [root / "pyproject.toml"]
    package = root / "cu_diff"
    for directory in (package, *(package / name for name in ASSETS)):
        if _linked(directory):
            raise ValueError(f"Linked package directory: {directory.name}")
        if not directory.is_dir():
            raise ValueError(f"Missing package directory: {directory.name}")
        for path in sorted(directory.iterdir()):
            # Reject links even when their extension is not allowlisted.
            if _linked(path):
                raise ValueError(f"Linked package entry: {path.name}")
            allowed = {".py"} if directory == package else ASSETS[directory.name]
            if path.is_file() and not path.name.startswith(".") and path.suffix in allowed:
                candidates.append(path)
    for path in candidates:
        _regular(path)
        files[path.relative_to(root).as_posix()] = path.read_bytes()
    missing = REQUIRED - files.keys()
    if missing:
        raise ValueError(f"Missing required deployment files: {', '.join(sorted(missing))}")
    project = tomllib.loads(files["pyproject.toml"].decode("utf-8"))
    extras = project.get("project", {}).get("optional-dependencies", {})
    if not extras.get("appservice"):
        raise ValueError("pyproject.toml must define the appservice extra")
    for name, content in files.items():
        if name.startswith("cu_diff/pricing/"):
            json.loads(content)
    files["requirements.txt"] = REQUIREMENTS
    return dict(sorted(files.items()))


def build_package(root: Path, output: Path) -> list[str]:
    files = package_files(root)
    output = Path(os.path.abspath(output))
    for parent in (output, *output.parents):
        if _linked(parent):
            raise ValueError("Output must not traverse links or junctions")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids overwriting an existing package or hardlinked file.
    with output.open("xb") as stream:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content)
    return list(files)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True, help="New ZIP path; never overwritten")
    arguments = parser.parse_args()
    manifest = build_package(arguments.root, arguments.output)
    print(json.dumps({"file_count": len(manifest), "files": manifest}, indent=2))


if __name__ == "__main__":
    main()
