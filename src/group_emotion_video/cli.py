from __future__ import annotations

import argparse
import json

from .workflow import Workflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="group-emotion-video")
    parser.add_argument("--config", action="append", dest="config_paths", default=[])
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_reference_parser = subparsers.add_parser("prepare-reference")
    prepare_reference_parser.add_argument("--excel-path", dest="excel_path")
    prepare_reference_parser.add_argument("--sheet-name", dest="sheet_name")
    prepare_reference_parser.add_argument("--schema-source-path", dest="schema_source_path")
    prepare_reference_parser.add_argument("--label-seed-path", dest="label_seed_path")
    prepare_reference_parser.add_argument("--query-seed-catalog-path", dest="query_seed_catalog_path")
    prepare_reference_parser.add_argument("--annotation-domains-path", dest="annotation_domains_path")

    register_local_parser = subparsers.add_parser("register-local-videos")
    register_local_parser.add_argument("--input-root", required=True, dest="input_root")
    register_local_parser.add_argument("--scene-text", dest="scene_text")
    register_local_parser.add_argument("--trigger-text", dest="trigger_text")
    register_local_parser.add_argument("--query-text", dest="query_text")
    register_local_parser.add_argument("--tag", action="append", dest="tags", default=[])
    register_local_parser.add_argument(
        "--transfer-mode",
        choices=("copy", "hardlink", "symlink", "move"),
        default="copy",
        dest="transfer_mode",
    )
    register_local_parser.add_argument("--replace-existing", action="store_true", dest="replace_existing")
    register_local_parser.add_argument("--default-duration-sec", type=float, dest="default_duration_sec")

    import_legacy_parser = subparsers.add_parser("import-01301-runtime")
    import_legacy_parser.add_argument("--legacy-runtime-root", required=True, dest="legacy_runtime_root")
    import_legacy_parser.add_argument("--raw-root", dest="raw_root")
    import_legacy_parser.add_argument("--limit", type=int, dest="limit")
    import_legacy_parser.add_argument("--replace-existing", action="store_true", dest="replace_existing")
    import_legacy_parser.add_argument("--progress", action=argparse.BooleanOptionalAction, default=True, dest="progress")

    for name in ("seed-queries", "crawl", "preprocess", "annotate", "download-loop", "pipeline-loop", "export", "status"):
        subparsers.add_parser(name)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--steps", required=True, help="Comma-separated steps")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workflow = Workflow.from_config_paths(args.config_paths)

    if args.command == "prepare-reference":
        result = workflow.prepare_reference(
            excel_path=args.excel_path,
            sheet_name=args.sheet_name,
            schema_source_path=args.schema_source_path,
            label_seed_path=args.label_seed_path,
            query_seed_catalog_path=args.query_seed_catalog_path,
            annotation_domains_path=args.annotation_domains_path,
        )
    elif args.command == "register-local-videos":
        result = workflow.register_local_videos(
            input_root=args.input_root,
            scene_text=args.scene_text,
            trigger_text=args.trigger_text,
            query_text=args.query_text,
            tags=args.tags,
            transfer_mode=args.transfer_mode,
            replace_existing=args.replace_existing,
            default_duration_sec=args.default_duration_sec,
        )
    elif args.command == "import-01301-runtime":
        result = workflow.import_01301_runtime(
            legacy_runtime_root=args.legacy_runtime_root,
            raw_root=args.raw_root,
            limit=args.limit,
            replace_existing=args.replace_existing,
            progress=args.progress,
        )
    elif args.command == "seed-queries":
        result = workflow.seed_queries()
    elif args.command == "crawl":
        result = workflow.crawl()
    elif args.command == "preprocess":
        result = workflow.preprocess()
    elif args.command == "annotate":
        result = workflow.annotate()
    elif args.command == "download-loop":
        result = workflow.download_loop()
    elif args.command == "pipeline-loop":
        result = workflow.pipeline_loop()
    elif args.command == "export":
        result = workflow.export()
    elif args.command == "status":
        result = workflow.status()
    elif args.command == "run":
        steps = [step.strip() for step in args.steps.split(",") if step.strip()]
        result = workflow.run(steps)
    else:  # pragma: no cover
        raise ValueError(f"Unsupported command: {args.command}")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
