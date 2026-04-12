from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


ConfigPathArg = str | Path | list[str | Path] | tuple[str | Path, ...] | None


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if yaml is None:
        raise RuntimeError("PyYAML is required to load configs. Install dependencies with `python3 -m pip install -e .`.")
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def resolve_config_paths(config_path: ConfigPathArg) -> list[str]:
    if config_path is None:
        return []
    raw_items = [config_path] if isinstance(config_path, (str, Path)) else list(config_path)
    resolved: list[str] = []
    for item in raw_items:
        for token in str(item).split(","):
            token = token.strip()
            if token:
                resolved.append(token)
    return resolved


def normalize_config(config: dict[str, Any], *, root_dir: Path, config_paths: list[str]) -> dict[str, Any]:
    project = config.setdefault("project", {})
    project.setdefault("name", root_dir.name)
    project["root_dir"] = str(root_dir)
    project["runtime_dir"] = str(root_dir / project.get("runtime_dir", "runtime"))
    project["prompts_dir"] = str(root_dir / project.get("prompts_dir", "prompts"))
    project["output_dir"] = str(root_dir / project.get("output_dir", "runtime/outputs"))
    project["config_paths"] = config_paths

    annotation = config.setdefault("annotation", {})
    annotation.setdefault("required_fields", ["group_emotion", "emotion_intensity", "evidence", "confidence"])
    annotation.setdefault("min_confidence", 0.65)
    annotation.setdefault("review_confidence", 0.80)
    annotation.setdefault("require_evidence", True)
    annotation.setdefault("random_review_ratio", 0.10)

    logging_cfg = config.setdefault("logging", {})
    logging_cfg.setdefault("level", "INFO")
    logging_cfg.setdefault("console_level", "INFO")
    logging_cfg.setdefault("save_raw_llm_payloads", True)

    llm = config.setdefault("llm", {})
    llm.setdefault("enabled", False)
    llm.setdefault("api_key_env", "OPENAI_API_KEY")
    llm.setdefault("base_url", "https://api.openai.com/v1")
    llm.setdefault("model", "gpt-4.1-mini")
    llm.setdefault("timeout_sec", 120)
    llm.setdefault("temperature", 0.1)
    llm.setdefault("max_retries", 3)
    llm.setdefault("retry_backoff_sec", 2)
    llm.setdefault("max_images_per_request", 6)
    llm.setdefault("use_remote_video_url", False)
    llm.setdefault("response_format", "json_object")
    return config


def load_config(config_path: ConfigPathArg = None) -> dict[str, Any]:
    root_dir = Path.cwd()
    config = load_yaml(root_dir / "configs" / "base.yaml")
    config_paths = resolve_config_paths(config_path)
    for override_path in config_paths:
        config = deep_merge(config, load_yaml(Path(override_path)))
    return normalize_config(config, root_dir=root_dir, config_paths=config_paths)


def config_fingerprint(config: dict[str, Any]) -> str:
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

