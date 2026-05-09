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

    for name in ("seed-queries", "crawl", "preprocess", "annotate", "download-loop", "pipeline-loop", "status"):
        subparsers.add_parser(name)

    stats_parser = subparsers.add_parser("dataset-stats")
    stats_parser.add_argument("--top-n", type=int, default=20, dest="top_n")

    review_list_parser = subparsers.add_parser("review-list")
    review_list_parser.add_argument("--status", default="pending", dest="status")
    review_list_parser.add_argument("--limit", type=int, default=20, dest="limit")

    review_complete_parser = subparsers.add_parser("review-complete")
    review_complete_parser.add_argument("--review-uid", required=True, dest="review_uid")
    review_complete_parser.add_argument("--annotation-json", required=True, dest="annotation_json_path")
    review_complete_parser.add_argument("--reviewer", dest="reviewer")
    review_complete_parser.add_argument("--notes", dest="review_notes")

    review_cancel_parser = subparsers.add_parser("review-cancel")
    review_cancel_parser.add_argument("--review-uid", required=True, dest="review_uid")
    review_cancel_parser.add_argument("--reviewer", dest="reviewer")
    review_cancel_parser.add_argument("--notes", dest="review_notes")

    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--limit", type=int, dest="limit")
    export_parser.add_argument("--min-confidence", type=float, dest="min_confidence")
    export_parser.add_argument(
        "--cap", action="append", dest="caps", default=[],
        help="Cap samples per annotation value: field=value:max_count (repeatable)",
    )

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--steps", required=True, help="Comma-separated steps")
    return parser


def _parse_caps(raw: list[str]) -> dict[tuple[str, str], int]:
    """Parse ``--cap field=value:count`` arguments into {(field, value): count}."""
    caps: dict[tuple[str, str], int] = {}
    for token in raw:
        try:
            field_value, count_str = token.rsplit(":", 1)
            field, value = field_value.split("=", 1)
        except ValueError:
            raise SystemExit(f"Invalid --cap format '{token}', expected field=value:count")
        caps[(field.strip(), value.strip())] = int(count_str)
    return caps


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
        caps = _parse_caps(args.caps)
        result = workflow.export(limit=args.limit, min_confidence=args.min_confidence, caps=caps)
    elif args.command == "status":
        result = workflow.status()
    elif args.command == "dataset-stats":
        result = workflow.dataset_stats(top_n=args.top_n)
    elif args.command == "review-list":
        result = workflow.list_reviews(status=args.status, limit=args.limit)
    elif args.command == "review-complete":
        result = workflow.complete_review(
            review_uid=args.review_uid,
            annotation_json_path=args.annotation_json_path,
            reviewer=args.reviewer,
            review_notes=args.review_notes,
        )
    elif args.command == "review-cancel":
        result = workflow.cancel_review(
            review_uid=args.review_uid,
            reviewer=args.reviewer,
            review_notes=args.review_notes,
        )
    elif args.command == "run":
        steps = [step.strip() for step in args.steps.split(",") if step.strip()]
        result = workflow.run(steps)
    else:  # pragma: no cover
        raise ValueError(f"Unsupported command: {args.command}")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
