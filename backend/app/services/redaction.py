"""Redaction for anything that leaves the platform (evidence download, exported test
evidence). Stored data and the UI keep the raw text; only exports are cleaned.

Two kinds: host usernames in absolute paths (C10) and secrets (known issue 4).
Codex and Claude Code receive the provider key inside the task container, so an
agent that dumps its environment writes the key into the trial log."""

import os
import re
from functools import lru_cache

from app.config import get_settings

# Only the username segment is removed, so the rest of the path stays useful.
_HOST_PATH_RE = re.compile(r"((?:/|\\\\?)(?:Users|home)(?:/|\\\\?))([^/\\\s\"']+)")

_SECRET_PATTERNS = (
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("groq-key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}")),
    ("fireworks-key", re.compile(r"\bfw_[A-Za-z0-9]{20,}")),
    ("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    (
        "private-key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
    ),
    ("bearer-token", re.compile(r"(?i)(?<=bearer )[A-Za-z0-9._~+/=-]{20,}")),
)

# NAME=value where NAME looks like a secret. Upper-case names only, so JSON fields
# such as "n_input_tokens" are left alone. Values starting with $ (a variable
# reference) or < (already redacted) are kept.
_ASSIGNMENT_RE = re.compile(
    r"\b([A-Z][A-Z0-9_]*(?:API_KEY|_SECRET|SECRET_KEY|_TOKEN|ACCESS_KEY|PASSWORD))"
    r"([\"']?\s*[=:]\s*[\"']?)"
    r"([^\s\"'$<,;][^\s\"',;]{7,})"
)
_SECRET_NAME_RE = re.compile(r"(?i)(api_key|secret|token|password)$")


def redact_host_paths(value):
    """Removes the username from `/Users/<user>/`, `/home/<user>/` and
    `C:\\Users\\<user>\\` paths in strings, dicts and lists."""
    if isinstance(value, str):
        return _HOST_PATH_RE.sub(lambda m: f"{m.group(1)}<redacted>", value)
    if isinstance(value, dict):
        return {k: redact_host_paths(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_host_paths(v) for v in value]
    return value


@lru_cache(maxsize=1)
def _known_secrets() -> tuple[str, ...]:
    """Key values this machine actually holds: every secret-named setting from .env
    and every secret-named environment variable. Matched exactly, so a key of any
    shape is removed even when no pattern knows its format."""
    values: set[str] = set()
    for name, val in get_settings().model_dump().items():
        if _SECRET_NAME_RE.search(name) and isinstance(val, str):
            values.add(val.strip())
    for name, val in os.environ.items():
        if _SECRET_NAME_RE.search(name):
            values.add(val.strip())
    return tuple(sorted((v for v in values if len(v) >= 12), key=len, reverse=True))


def _redact_text(text: str, counts: dict[str, int]) -> str:
    for secret in _known_secrets():
        if secret in text:
            counts["configured-key"] = counts.get("configured-key", 0) + text.count(secret)
            text = text.replace(secret, "<redacted:configured-key>")
    for label, pattern in _SECRET_PATTERNS:
        text, n = pattern.subn(f"<redacted:{label}>", text)
        if n:
            counts[label] = counts.get(label, 0) + n

    def _assignment(m: re.Match) -> str:
        counts["secret-assignment"] = counts.get("secret-assignment", 0) + 1
        return f"{m.group(1)}{m.group(2)}<redacted>"

    return _ASSIGNMENT_RE.sub(_assignment, text)


def redact_secrets(value, counts: dict[str, int] | None = None):
    """Replaces API keys, tokens and private keys in strings, dicts and lists with
    `<redacted:kind>`. Pass `counts` to learn how many of each kind were removed."""
    if counts is None:
        counts = {}
    if isinstance(value, str):
        return _redact_text(value, counts)
    if isinstance(value, dict):
        return {k: redact_secrets(v, counts) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_secrets(v, counts) for v in value]
    return value
