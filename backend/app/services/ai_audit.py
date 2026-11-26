"""The one manual ruling on the platform: a named person approves or disapproves the AI Detection result.

GPTZero is a machine opinion about writing. Here a person decides: `approved` makes the check pass,
`disapproved` makes it fail, whatever the detector said. The detector's own result is kept beside it.
A ruling is kept against a fingerprint of the two files that were judged, so a rewrite needs a new look.
No other check on the platform can be ruled on: it fails or passes, and a failing task is revised.
"""
import hashlib
from datetime import UTC, datetime
from pathlib import Path

RULINGS = ("approved", "disapproved")
_FILES = ("instruction.md", "solution/solve.sh")


def fingerprint(task_root: Path) -> str:
    h = hashlib.sha256()
    for rel in _FILES:
        path = task_root / rel
        h.update(rel.encode() + b"\0")
        h.update(path.read_bytes() if path.is_file() else b"<missing>")
        h.update(b"\0")
    return h.hexdigest()


def decision_error(ruling: str, adjudicator: str, reason: str) -> str | None:
    if ruling not in RULINGS:
        return f"ruling must be one of {', '.join(RULINGS)}"
    if not adjudicator.strip() or len(adjudicator) > 200:
        return "your name is required"
    if not reason.strip() or len(reason) > 2000:
        return "a written reason is required (2000 characters at most)"
    return None


def entry(ruling: str, adjudicator: str, reason: str, fp: str) -> dict:
    return {"ruling": ruling, "adjudicator": adjudicator.strip(), "reason": reason.strip(),
            "fingerprint": fp, "at": datetime.now(UTC).isoformat()}


def effective(detected_status: str, decision: dict | None, current_fp: str | None) -> tuple[str, bool]:
    """(status to show, is the ruling stale). A stale ruling no longer applies."""
    if not decision:
        return detected_status, False
    if current_fp is None or decision.get("fingerprint") != current_fp:
        return detected_status, True
    return ("passed" if decision.get("ruling") == "approved" else "failed"), False
