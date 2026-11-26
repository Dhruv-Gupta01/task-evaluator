"""Does task.toml follow the standard format? (Static Check; authors fix what fails.)

The format is Terminal-Bench's own task template (vendor/tb_template/) for what must be there and what type it
has, and Harbor's field list (vendor/harbor_schema/, dumped from the installed package) for what exists at all.
Harbor ignores unknown keys silently, so a misspelled setting simply does nothing; here it fails.
Comments and field order are not compared.
"""
from __future__ import annotations

import difflib
import json
import tomllib
from pathlib import Path

_VENDOR = Path(__file__).resolve().parents[2] / "vendor"
_TEMPLATE = _VENDOR / "tb_template" / "task-template.toml"
_SCHEMA = _VENDOR / "harbor_schema" / "task_config_fields.json"

_OPTIONAL = {"metadata.author_organization"}          # the template itself marks it optional
_OUR_METADATA = {"oracle_solutions"}                  # this platform's own field
_REQUIRED_VALUES = {"verifier.environment_mode": "separate", "environment.network_mode": "public"}
# tables whose keys are the author's own, or deeper than the field list goes
_FREE = {"task", "metadata", "source", "solution.env", "environment.env", "verifier.env", "environment.healthcheck",
         "verifier.environment", "environment.mcp_servers", "steps", "artifacts", "verifier.collect"}


def _template_fields() -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in tomllib.loads(_TEMPLATE.read_text()).items():
        if isinstance(value, dict):
            out.update({f"{key}.{k}": v for k, v in value.items()})
        else:
            out[key] = value
    return out


def _kind(value) -> str:
    if isinstance(value, bool):
        return "true or false"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "text in quotes"
    if isinstance(value, list):
        return "a list"
    return "a table"


def _get(data: dict, dotted: str):
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _suggest(name: str, known: list[str]) -> str:
    close = difflib.get_close_matches(name, known, n=1, cutoff=0.7)
    return f" (did you mean `{close[0]}`?)" if close else ""


def compare(data: dict) -> tuple[list[str], list[str]]:
    """(things that fail, extra fields that are only flagged)."""
    problems: list[str] = []
    extra: list[str] = []
    template = _template_fields()

    for field, example in template.items():
        present, value = _get(data, field)
        if not present:
            if field not in _OPTIONAL:
                problems.append(f"{field} is missing")
            continue
        if _kind(value) != _kind(example):
            problems.append(f"{field} must be {_kind(example)}, not {_kind(value)}")
        elif field in ("metadata.tags", "artifacts") and not all(isinstance(v, str) for v in value) and field == "metadata.tags":
            problems.append(f"{field} must be a list of text values")
        elif isinstance(example, str) and field not in _OPTIONAL and field not in _REQUIRED_VALUES and not value.strip():
            problems.append(f"{field} is empty")

    for field, wanted in _REQUIRED_VALUES.items():
        present, value = _get(data, field)
        if present and value != wanted:
            problems.append(f'{field} must be "{wanted}", not "{value}"')
    for section in ("agent", "verifier"):
        present, value = _get(data, f"{section}.network_mode")
        if present and value != "public":
            problems.append(f'{section}.network_mode must be "public", not "{value}"')
    if _get(data, "environment.allow_internet")[0]:
        problems.append('environment.allow_internet is out of date: remove it and use network_mode = "public"')

    known = set(json.loads(_SCHEMA.read_text())["fields"])
    top = sorted({k.split(".")[0] for k in known})
    for key, value in data.items():
        if key == "version":
            extra.append("version (Harbor does not read it; its own field is schema_version)")
            continue
        if key not in known:
            problems.append(f"unknown field or section `{key}`{_suggest(key, top)}")
            continue
        if key in _FREE or not isinstance(value, dict):
            continue
        children = [k.split(".", 1)[1] for k in known if k.startswith(key + ".")]
        for child in value:
            if f"{key}.{child}" not in known:
                problems.append(f"unknown field `{key}.{child}`{_suggest(child, children)}")

    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        expected = {f.split(".", 1)[1] for f in template if f.startswith("metadata.")} | _OUR_METADATA
        extra.extend(f"metadata.{k}" for k in metadata if k not in expected)
    return problems, extra


def check_task_toml_format(task_root: Path, report) -> None:
    try:
        data = tomllib.loads((task_root / "task.toml").read_text())
    except (OSError, ValueError) as exc:
        report.add("task_toml_format", "fail", f"task.toml could not be read as TOML: {exc}")
        return
    problems, extra = compare(data)
    if problems:
        report.add("task_toml_format", "fail",
                   f"task.toml differs from the standard format ({len(problems)}): " + "; ".join(problems) + ".")
    else:
        report.add("task_toml_format", "info", "task.toml follows the standard format.")
    if extra:
        report.add("task_toml_extra_fields", "warn",
                   "Fields beyond the standard format (flagged only, they do not fail): " + ", ".join(extra) + ".")
