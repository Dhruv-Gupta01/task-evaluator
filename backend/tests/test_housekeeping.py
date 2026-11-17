"""Batch 0: CORS for PUT (K1), secret redaction on export (K4), vendored counts
read from the files plus the drift reminder (K12)."""

import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.services import redaction, vendor_info

REPO = Path(__file__).resolve().parents[2]


def _preflight(method: str):
    origin = get_settings().cors_origins[0]
    return TestClient(app).options(
        "/submissions/any/reviewer-decision",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "content-type",
        },
    )


def test_preflight_allows_put():
    r = _preflight("PUT")
    assert r.status_code == 200
    assert "PUT" in r.headers["access-control-allow-methods"]


def test_preflight_still_refuses_delete():
    assert _preflight("DELETE").status_code == 400


FAKE = {
    "anthropic-key": "sk-ant-api03-" + "A1b2C3d4" * 6,
    "openai-key": "sk-proj-" + "Z9y8X7w6" * 5,
    "google-key": "AIza" + "B" * 35,
    "github-token": "ghp_" + "c" * 36,
    "aws-key": "AKIA" + "ABCDEFGHIJKLMNOP",
    "groq-key": "gsk_" + "d" * 40,
    "huggingface-token": "hf_" + "e" * 34,
}


@pytest.mark.parametrize("label,secret", sorted(FAKE.items()))
def test_each_key_kind_is_redacted(label, secret):
    counts: dict[str, int] = {}
    out = redaction.redact_secrets({"logs": [f"env: X={secret} done"]}, counts)
    assert secret not in str(out)
    assert counts == {label: 1}
    assert f"<redacted:{label}>" in out["logs"][0]


PEM = "-----{} RSA PRIVATE" + " KEY-----"


def test_env_dump_assignment_and_bearer_and_private_key():
    text = (
        "OPENAI_API_KEY=" + FAKE["openai-key"] + "\n"
        "MY_SERVICE_TOKEN=plainvalue123456\n"
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123\n"
        + PEM.format("BEGIN") + "\nMIIEow\n" + PEM.format("END") + "\n"
    )
    counts: dict[str, int] = {}
    out = redaction.redact_secrets(text, counts)
    assert "plainvalue123456" not in out and "MIIEow" not in out
    assert "abcdefghijklmnopqrstuvwxyz0123" not in out
    assert "MY_SERVICE_TOKEN=<redacted>" in out
    assert counts == {"openai-key": 1, "secret-assignment": 1, "bearer-token": 1, "private-key": 1}


def test_configured_key_of_any_shape_is_redacted(monkeypatch):
    odd = "custom-shape-key-0123456789"
    monkeypatch.setenv("SOMEVENDOR_API_KEY", odd)
    redaction._known_secrets.cache_clear()
    try:
        counts: dict[str, int] = {}
        out = redaction.redact_secrets(f"agent printed {odd} twice: {odd}", counts)
    finally:
        monkeypatch.delenv("SOMEVENDOR_API_KEY")
        redaction._known_secrets.cache_clear()
    assert odd not in out
    assert counts["configured-key"] == 2


@pytest.mark.parametrize(
    "text",
    [
        '{"n_input_tokens": 123456789, "cost_usd": 0.42}',
        "LLM_MAX_OUTPUT_TOKENS=128000",
        "ENV OPENAI_API_KEY=${OPENAI_API_KEY}",
        "sha256:" + "f" * 64,
        "pip install scikit-learn==1.5.2",
        "OPENAI_API_KEY=<redacted:openai-key>",
    ],
)
def test_ordinary_text_is_left_alone(text):
    counts: dict[str, int] = {}
    assert redaction.redact_secrets(text, counts) == text
    assert counts == {}


def test_redaction_is_idempotent():
    once = redaction.redact_secrets("k=" + FAKE["anthropic-key"])
    assert redaction.redact_secrets(once) == once


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/Users/alice/task/x", "/Users/<redacted>/task/x"),
        ("/home/bob/.cache", "/home/<redacted>/.cache"),
        ("C:\\Users\\carol\\te50\\a", "C:\\Users\\<redacted>\\te50\\a"),
        ("C:\\\\Users\\\\dave\\\\x", "C:\\\\Users\\\\<redacted>\\\\x"),
        ("C:/Users/erin/x", "C:/Users/<redacted>/x"),
    ],
)
def test_host_paths(raw, expected):
    assert redaction.redact_host_paths(raw) == expected


def test_rubric_count_is_read_from_the_vendored_file():
    text = vendor_info.RUBRIC_FILE.read_text(encoding="utf-8")
    expected = len(re.findall(r"^\[\[criteria\]\]", text, re.M))
    assert expected > 0
    assert vendor_info.rubric_info()["criteria"] == expected


def test_check_count_is_read_from_the_vendored_folder():
    assert vendor_info.checks_info()["scripts"] == len(list(vendor_info.CHECKS_DIR.glob("check-*.sh")))


def test_drift_reminder(monkeypatch):
    info = vendor_info.checks_info()
    pinned = date.fromisoformat(info["date"])
    limit = 30
    monkeypatch.setattr(get_settings(), "vendor_stale_days", limit)
    fresh = vendor_info.checks_info(today=pinned + timedelta(days=limit))
    old = vendor_info.rubric_info(today=pinned + timedelta(days=limit + 1))
    assert not fresh["stale"] and fresh["reminder"] is None
    assert old["stale"] and "diff_vendored_checks.py" in old["reminder"]
    monkeypatch.setattr(get_settings(), "vendor_stale_days", 0)
    assert not vendor_info.checks_info(today=pinned + timedelta(days=999))["stale"]


def test_no_hard_coded_criteria_counts_left():
    stale = re.compile(r"\b3[4-9][- ]criteri")
    hits = []
    for folder, pattern in ((REPO / "backend" / "app", "*.py"), (REPO / "src", "*.ts*")):
        for path in folder.rglob(pattern):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if stale.search(line):
                    hits.append(f"{path.relative_to(REPO)}:{n}")
    assert hits == []
