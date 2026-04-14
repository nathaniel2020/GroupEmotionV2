from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from group_emotion_video.annotation_stats import organize_annotation_jsons, summarize_available_annotation_jsons


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Count available annotation JSON files: status=done and no reject keyword anywhere in the JSON payload."
    )
    parser.add_argument("input_root", help="Directory containing annotation JSON files")
    parser.add_argument("--output-root", help="Optional directory to store categorized JSON files")
    parser.add_argument("--reject-keyword", default="reject", help="Keyword used to exclude JSON payloads")
    parser.add_argument(
        "--transfer-mode",
        choices=("copy", "hardlink", "symlink"),
        default="copy",
        help="How categorized files should be stored when --output-root is provided",
    )
    parser.add_argument(
        "--clear-output",
        action="store_true",
        help="Remove the existing output directory before writing categorized files",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.output_root:
        summary = organize_annotation_jsons(
            args.input_root,
            args.output_root,
            reject_keyword=args.reject_keyword,
            transfer_mode=args.transfer_mode,
            clear_output=args.clear_output,
        )
    else:
        summary = summarize_available_annotation_jsons(args.input_root, reject_keyword=args.reject_keyword)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
