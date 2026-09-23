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


def graphics(args: argparse.Namespace) -> None:
    from .client import digest
    from .graphics import compare_graphics
    responses, hashes = {}, {}
    for role in ("old", "new"):
        path = getattr(args, role)
        hashes[role] = digest(path.read_bytes())
        response_path = getattr(args, f"{role}_response")
        metadata_path = getattr(args, f"{role}_metadata")
        if bool(response_path) != bool(metadata_path):
            raise ValueError("CU layout requires both response and matching metadata; no inferred file association")
        if response_path:
            metadata = read_json(metadata_path)
            if metadata.get("document_sha256") != hashes[role]:
                raise ValueError(f"{role} cached CU document hash does not match the PDF")
            responses[role] = read_json(response_path)
            if responses[role].get("status") != "Succeeded":
                raise ValueError(f"{role} CU response did not succeed")
    selected = {change for change, flag in (("visual_moved", "include_translation"),
                                           ("visual_scaled", "include_scaling"))
                if getattr(args, flag, False)}
    result = compare_graphics(args.old, args.new, responses.get("old"), responses.get("new"),
                              dpi=args.dpi, include_transformations=bool(selected))
    result["items"] = [item for item in result["items"]
                       if item["change"] not in {"visual_moved", "visual_scaled"}
                       or item["change"] in selected]
    result["coverage"]["selected_transformations"] = sorted(selected)
    result["provenance"] = {"document_sha256": hashes, "azure_calls": 0,
                            "chronology": "User-supplied old/new direction",
                            "coordinate_basis": "Displayed PDF page, normalized x/y; no elastic warp"}
    args.output.mkdir(parents=True, exist_ok=True)
    save_json(args.output / "graphics.json", result)
    from .report import cell
    lines = ["# 本地图形差异候选", "", "只使用本地PDF和可选已校验CU缓存，不调用Azure。",
             "设计内容单独比较；仅按命令行选项列出视图平移/等比绘图缩放，变换框不是设计残差。"
             "外观残差仍需复核，不推断实物尺寸或材料改变。",
             "", "| 编号 | 类型 | 旧侧区域 | 新侧区域 |",
             "|---|---|---|---|"]
    for item in result["items"]:
        lines.append("| " + " | ".join([
            cell(item["id"]), cell(item["key"]), cell((item["old"] or {}).get("locations")),
            cell((item["new"] or {}).get("locations")),
        ]) + " |")
    lines.extend(["", "## 覆盖与限制", "", "```json",
                  json.dumps(result["coverage"], ensure_ascii=False, indent=2), "```"])
    (args.output / "graphics.zh.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Local graphical comparison saved: {args.output.resolve()}")


def tables(args: argparse.Namespace) -> None:
    from .client import digest
    from .document_tables import compare_document_tables
    from .report import write_table_report
    responses, hashes = {}, {}
    for role in ("old", "new"):
        path = getattr(args, role)
        hashes[role] = digest(path.read_bytes())
        metadata = read_json(getattr(args, f"{role}_metadata"))
        if metadata.get("document_sha256") != hashes[role]:
            raise ValueError(f"{role} cached CU document hash does not match the PDF")
        response = read_json(getattr(args, f"{role}_response"))
        if response.get("status") != "Succeeded" or not response.get("result", {}).get("contents"):
            raise ValueError(f"{role} CU response did not succeed or has no content")
        responses[role] = response
    result = compare_document_tables(args.old, args.new, responses["old"], responses["new"])
    result["provenance"] = {
        "document_sha256": hashes, "azure_calls": 0,
        "chronology": "User-supplied old/new direction",
        "coordinate_basis": "Displayed PDF page with matching CU source geometry",
    }
    args.output.mkdir(parents=True, exist_ok=True)
    save_json(args.output / "tables.json", result)
    write_table_report(result, args.output / "tables.zh.md")
    print(f"Local table comparison saved: {args.output.resolve()}")


def model_compare(args: argparse.Namespace) -> None:
    from .client import digest
    from .model_compare import compare_with_model, options
    from .report import write_model_report
    config = read_json(args.config)
    if not options(config)["enabled"]:
        raise ValueError("Enable model_comparison only after approving the existing deployment boundary")
    responses = {}
    for role in ("old", "new"):
        path = getattr(args, role)
        if read_json(getattr(args, f"{role}_metadata")).get("document_sha256") != digest(path.read_bytes()):
            raise ValueError(f"{role} cached CU document hash does not match the PDF")
        responses[role] = read_json(getattr(args, f"{role}_response"))
    client = Client(config)
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        analyzer_id, analyzer = client.ensure_analyzer(allow_create=False)
        result = compare_with_model(
            args.old, args.new, responses["old"], responses["new"], client=client,
            cache=args.cache_dir, analyzer_id=analyzer_id, analyzer=analyzer,
            allow_submit=args.allow_azure_upload,
            progress=lambda message: print(message, flush=True))
        save_json(args.output / "model-comparison.json", result)
        write_model_report(result, args.output / "model-comparison.zh.md")
        print(f"Model comparison saved locally: {args.output.resolve()}")
    finally:
        events = args.output / "model-api-events.json"
        previous = read_json(events).get("events", []) if events.exists() else []
        save_json(events, {"events": previous + client.events})


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
    graphical = commands.add_parser("graphics", help="Offline graphical comparison; never calls Azure")
    graphical.add_argument("--old", type=Path, required=True)
    graphical.add_argument("--new", type=Path, required=True)
    for role in ("old", "new"):
        graphical.add_argument(f"--{role}-response", type=Path)
        graphical.add_argument(f"--{role}-metadata", type=Path)
    graphical.add_argument("--dpi", type=int, default=200)
    graphical.add_argument("--include-translation", action="store_true",
                           help="Include optional full-view translation frames, not design changes")
    graphical.add_argument("--include-scaling", action="store_true",
                           help="Include verified uniform drawing scale, not physical part dimensions")
    graphical.add_argument("--output", type=Path, default=Path("output") / "graphics")
    graphical.set_defaults(action=graphics)
    tabular = commands.add_parser("tables", help="Offline non-BOM table evidence; never calls Azure")
    for role in ("old", "new"):
        tabular.add_argument(f"--{role}", type=Path, required=True)
        tabular.add_argument(f"--{role}-response", type=Path, required=True)
        tabular.add_argument(f"--{role}-metadata", type=Path, required=True)
    tabular.add_argument("--output", type=Path, default=Path("output") / "tables")
    tabular.set_defaults(action=tables)
    model = commands.add_parser("model-compare", help="Model region pairing and source-grounded CU crop rereading")
    for role in ("old", "new"):
        model.add_argument(f"--{role}", type=Path, required=True)
        model.add_argument(f"--{role}-response", type=Path, required=True)
        model.add_argument(f"--{role}-metadata", type=Path, required=True)
    model.add_argument("--config", type=Path, required=True)
    model.add_argument("--cache-dir", type=Path, required=True)
    model.add_argument("--output", type=Path, default=Path("output") / "model")
    model.add_argument("--allow-azure-upload", action="store_true")
    model.set_defaults(action=model_compare)
    args = parser.parse_args()
    try:
        args.action(args)
    except (CUError, ValueError, OSError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
