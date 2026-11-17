"""Which upstream rule set the vendored Terminal-Bench files come from, read from the
files themselves so no count or commit is hard-coded anywhere else (K12)."""

import re
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path

from app.config import get_settings

VENDOR_DIR = Path(__file__).resolve().parents[2] / "vendor"
CHECKS_DIR = VENDOR_DIR / "tb_checks" / "scripts"
RUBRIC_FILE = VENDOR_DIR / "tb_prompts" / "task-implementation.toml"
DIFF_SCRIPT = "backend/scripts/diff_vendored_checks.py"


def _provenance(folder: Path, today: date | None = None) -> dict:
    info: dict = {"commit": None, "date": None, "age_days": None, "stale": False, "reminder": None}
    try:
        text = (folder / "PROVENANCE.md").read_text(encoding="utf-8")
    except OSError:
        return info
    m = re.search(r"Commit:\s*`([0-9a-f]{7,40})`(?:\s*\(([\d-]+)\))?", text)
    if not m:
        return info
    info["commit"], info["date"] = m.group(1), m.group(2)
    if info["date"]:
        try:
            pinned = date.fromisoformat(info["date"])
        except ValueError:
            return info
        info["age_days"] = ((today or datetime.now(UTC).date()) - pinned).days
        limit = get_settings().vendor_stale_days
        if limit and info["age_days"] > limit:
            info["stale"] = True
            info["reminder"] = (
                f"The vendored upstream commit is {info['age_days']} days old (limit {limit}). "
                f"Run `uv run python {DIFF_SCRIPT}` to see what upstream changed."
            )
    return info


def checks_info(today: date | None = None) -> dict:
    """Commit, date, age and the number of check-*.sh scripts that run
    (check-cpu-threads.py is a helper, not counted)."""
    info = _provenance(CHECKS_DIR.parent, today)
    info["scripts"] = len(list(CHECKS_DIR.glob("check-*.sh")))
    return info


def rubric_info(today: date | None = None) -> dict:
    """Commit, date, age and the number of [[criteria]] in the vendored TB rubric."""
    info = _provenance(RUBRIC_FILE.parent, today)
    try:
        criteria = tomllib.loads(RUBRIC_FILE.read_text(encoding="utf-8")).get("criteria") or []
    except (OSError, tomllib.TOMLDecodeError):
        criteria = []
    info["criteria"] = len(criteria)
    return info
