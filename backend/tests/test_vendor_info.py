"""Vendored counts read from the files, and the drift reminder (VENDOR_STALE_DAYS)."""

import re
from datetime import date, timedelta
from pathlib import Path

from app.config import get_settings
from app.services import vendor_info

REPO = Path(__file__).resolve().parents[2]

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
