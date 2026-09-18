"""Command line entry points. Customer-derived output is local only."""

import argparse
import json
import sys
from pathlib import Path

from .client import CUError, Client, save_json


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def inspect_pdf(path: Path) -> dict:
    import pymupdf
    from .client import digest
    with pymupdf.open(path) as document:
        return {
            "sha256": digest(path.read_bytes()),
            "pages": [{"page": page.number + 1, "width_pt": page.rect.width,
                       "height_pt": page.rect.height,
                       "native_word_count": len(page.get_text("words")),
                       "raster_image_count": len(page.get_images()),
                       "vector_path_count": len(page.get_drawings())}
                      for page in document],
        }


def extract(args: argparse.Namespace) -> None:
    if not args.allow_azure_upload:
        raise ValueError("Upload requires --allow-azure-upload after resource/data-boundary approval")
    config = read_json(args.config)
    client = Client(config)
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        analyzer_id, analyzer = client.ensure_analyzer()
        save_json(args.output / "analyzer.json", analyzer)
        for role in ("old", "new"):
            path = getattr(args, role)
            inspection = inspect_pdf(path)
            save_json(args.output / f"{role}.inspection.json", inspection)
            response, metadata = client.analyze(path, args.output / "cache", analyzer_id, analyzer)
            save_json(args.output / f"{role}.response.json", response)
            save_json(args.output / f"{role}.metadata.json", metadata)
            print(f"{role}: Succeeded; cache_hit={metadata['cache_hit']}", flush=True)
    finally:
        events_path = args.output / "api-events.json"
        previous = read_json(events_path).get("events", []) if events_path.exists() else []
        save_json(events_path, {"events": previous + client.events})


def diagnose(args: argparse.Namespace) -> None:
    client = Client(read_json(args.config))
    base, _ = client.request("GET", client.url("analyzers/prebuilt-document"))
    defaults, _ = client.request("GET", client.url("defaults"))
    result = {"endpoint": client.endpoint, "supported_models": base.get("supportedModels"),
              "defaults": defaults, "events": client.events,
              "requested_model": client.config["completion_model"]}
    result["requested_model_supported"] = (
        client.config["completion_model"] in base.get("supportedModels", {}).get("completion", [])
    )
    save_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def compare(args: argparse.Namespace) -> None:
    from .compare import compare_responses
    from .report import write_report
    old, new = read_json(args.old), read_json(args.new)
    for role, response in (("old", old), ("new", new)):
        if response.get("status", "").lower() != "succeeded":
            raise ValueError(f"{role} CU operation did not succeed; refusing a success-shaped report")
        if not response.get("result", {}).get("contents"):
            raise ValueError(f"{role} CU operation has no content; cannot conclude no changes")
    result = compare_responses(old, new)
    result["provenance"] = {
        "old_response": str(args.old.resolve()), "new_response": str(args.new.resolve()),
        "chronology": "User supplied old/new order; not inferred from part IDs or filenames",
    }
    args.output.mkdir(parents=True, exist_ok=True)
    save_json(args.output / "comparison.json", result)
    write_report(result, args.output / "comparison.zh.md")
    print(f"Comparison saved locally: {args.output.resolve()}")


def crop(args: argparse.Namespace) -> None:
    import pymupdf
    if args.pdf.resolve() == args.output.resolve():
        raise ValueError("Crop output must not overwrite its source PDF")
    if not 72 <= args.dpi <= 1200:
        raise ValueError("Crop DPI must be between 72 and 1200")
    with pymupdf.open(args.pdf) as document:
        if not 1 <= args.page <= len(document):
            raise ValueError("Crop page must be a valid 1-based physical page")
        if args.rect[0] >= args.rect[2] or args.rect[1] >= args.rect[3]:
            raise ValueError("Crop rectangle must have positive width and height")
        page = document[args.page - 1]
        rect = pymupdf.Rect(args.rect) & page.rect
        if rect.is_empty:
            raise ValueError("Crop rectangle lies outside the page")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(args.dpi / 72, args.dpi / 72),
                                clip=rect, alpha=False)
        # PDF input preserves high-resolution crop dimensions for CU.
        with pymupdf.open() as output:
            cropped = output.new_page(width=rect.width, height=rect.height)
            cropped.insert_image(cropped.rect, stream=pixmap.tobytes("png"))
            output.save(args.output)
        save_json(args.output.with_suffix(".mapping.json"), {
            "source_sha256": inspect_pdf(args.pdf)["sha256"], "page": args.page,
            "crop_pt": list(rect), "dpi": args.dpi,
            "mapping": "original_pt = crop_CU_inch * 72 + crop_pt_top_left",
            "pixel_width": pixmap.width, "pixel_height": pixmap.height,
        })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    diagnosis = commands.add_parser("diagnose")
    diagnosis.add_argument("--config", type=Path, required=True)
    diagnosis.add_argument("--output", type=Path, default=Path("output") / "diagnosis.json")
    diagnosis.set_defaults(action=diagnose)
    extraction = commands.add_parser("extract")
    extraction.add_argument("--config", type=Path, required=True)
    extraction.add_argument("--old", type=Path, required=True)
    extraction.add_argument("--new", type=Path, required=True)
    extraction.add_argument("--output", type=Path, default=Path("output"))
    extraction.add_argument("--allow-azure-upload", action="store_true")
    extraction.set_defaults(action=extract)
    comparison = commands.add_parser("compare")
    comparison.add_argument("--old", type=Path, required=True)
    comparison.add_argument("--new", type=Path, required=True)
    comparison.add_argument("--output", type=Path, default=Path("output"))
    comparison.set_defaults(action=compare)
    cropping = commands.add_parser("crop")
    cropping.add_argument("--pdf", type=Path, required=True)
    cropping.add_argument("--page", type=int, default=1)
    cropping.add_argument("--rect", type=float, nargs=4, required=True,
                          metavar=("X0", "Y0", "X1", "Y1"))
    cropping.add_argument("--dpi", type=int, default=600)
    cropping.add_argument("--output", type=Path, required=True)
    cropping.set_defaults(action=crop)
    args = parser.parse_args()
    try:
        args.action(args)
    except (CUError, ValueError, OSError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
