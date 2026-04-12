from __future__ import annotations

import argparse
import json

from .workflow import Workflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="group-emotion-video")
    parser.add_argument("--config", action="append", dest="config_paths", default=[])
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("prepare-reference", "seed-queries", "crawl", "preprocess", "annotate", "export", "status"):
        subparsers.add_parser(name)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--steps", required=True, help="Comma-separated steps")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workflow = Workflow.from_config_paths(args.config_paths)

    if args.command == "prepare-reference":
        result = workflow.prepare_reference()
    elif args.command == "seed-queries":
        result = workflow.seed_queries()
    elif args.command == "crawl":
        result = workflow.crawl()
    elif args.command == "preprocess":
        result = workflow.preprocess()
    elif args.command == "annotate":
        result = workflow.annotate()
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
