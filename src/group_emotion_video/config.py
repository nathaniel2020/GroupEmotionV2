from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


def _deep_merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def load_config(config_paths: str | list[str] | None = None) -> dict[str, Any]:
    root_dir = Path.cwd().resolve()
    base_path = root_dir / "configs" / "base.yaml"
    if not base_path.exists():
        raise FileNotFoundError(f"Missing base config: {base_path}")

    with base_path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    extra_paths: list[str] = []
    if isinstance(config_paths, str):
        extra_paths = [config_paths]
    elif isinstance(config_paths, list):
        extra_paths = list(config_paths)

    for raw_path in extra_paths:
        path = Path(raw_path)
        if not path.is_absolute():
            path = root_dir / raw_path
        with path.open("r", encoding="utf-8") as handle:
            config = _deep_merge(config, yaml.safe_load(handle) or {})

    config.setdefault("project", {})
    config["project"]["root_dir"] = str(root_dir)
    return config
