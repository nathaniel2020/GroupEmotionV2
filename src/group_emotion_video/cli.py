from __future__ import annotations

import argparse
import json
from typing import Any

from .config import load_config
from .logging_utils import RunLogger
from .pipeline import LightweightPipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Lightweight group emotion video pipeline")
    parser.add_argument("--config", action="append", default=[], help="Override config path. Repeatable.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("plan", help="Print the simplified workflow")

    annotate_parser = subparsers.add_parser("annotate", help="Annotate one sample manifest")
    annotate_parser.add_argument("--sample", required=True, help="Path to a sample JSON file")
    annotate_parser.add_argument("--dry-run", action="store_true", help="Skip live LLM call and write stub output")

    subparsers.add_parser("status", help="Summarize current outputs")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    run_logger = RunLogger(config, stage=args.command)
    pipeline = LightweightPipeline(config, run_logger)

    try:
        if args.command == "plan":
            payload: Any = {"stages": pipeline.describe()}
        elif args.command == "annotate":
            payload = pipeline.annotate_sample(args.sample, dry_run=bool(args.dry_run))
        elif args.command == "status":
            payload = pipeline.status()
        else:  # pragma: no cover
            raise ValueError(f"Unsupported command: {args.command}")

        print(json.dumps(payload, ensure_ascii=False, indent=2))
        run_logger.finalize("completed", summary={"command": args.command})
        return 0
    except Exception as exc:
        run_logger.error("command_failed", exc, command=args.command)
        run_logger.finalize("failed", summary={"command": args.command, "error": str(exc)})
        return 1

