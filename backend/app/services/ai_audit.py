"""Human audit for AI detection (Terminal-Bench's AI-usage policy, the human layer).

Layer 2 (the rubric's instruction_concision) and Layer 3 (GPTZero) are machine opinions. Here a named
person approves, disputes or is unsure about the AI use in `instruction.md` and `solution/solve.sh`, and
makes an overall call. The ruling is kept against a fingerprint of those two files, so a rewritten
instruction needs a new look. Advisory evidence: it never changes a gate.
"""
import hashlib
from datetime import UTC, datetime
from pathlib import Path

TARGETS = ("instruction.md", "solution/solve.sh", "overall")
RULINGS = ("approved", "disputed", "unsure")
_FILES = ("instruction.md", "solution/solve.sh")


def fingerprint(task_root: Path) -> str:
    h = hashlib.sha256()
    for rel in _FILES:
        p = task_root / rel
        h.update(rel.encode() + b"\0")
        try:
            h.update(p.read_bytes() if p.is_file() else b"<missing>")
        except OSError:
            h.update(b"?")
        h.update(b"\0")
    return h.hexdigest()


def decision_error(target: str, ruling: str, adjudicator: str, reason: str) -> str | None:
    if target not in TARGETS:
        return f"target must be one of {', '.join(TARGETS)}"
    if ruling not in RULINGS:
        return f"ruling must be one of {', '.join(RULINGS)}"
    if not adjudicator.strip() or len(adjudicator) > 200:
        return "the adjudicator's name is required"
    if len(reason) > 2000:
        return "the reason is too long (2000 characters at most)"
    if ruling != "unsure" and not reason.strip():
        return "a written reason is required to approve or dispute"
    return None


def entry(ruling: str, adjudicator: str, reason: str, fp: str) -> dict:
    return {
        "ruling": ruling,
        "adjudicator": adjudicator.strip(),
        "reason": reason.strip(),
        "fingerprint": fp,
        "at": datetime.now(UTC).isoformat(),
    }


def view(stored: dict | None, current_fp: str | None) -> dict:
    """{decisions: {target: entry + stale}, history, stale: [targets], summary}."""
    current = (stored or {}).get("current") or {}
    decisions = {t: {**e, "stale": current_fp is not None and e.get("fingerprint") != current_fp} for t, e in current.items()}
    stale = [t for t in TARGETS if decisions.get(t, {}).get("stale")]
    if not decisions:
        return {"decisions": {}, "history": (stored or {}).get("history", []), "stale": [], "summary": "No human audit yet."}
    counts = {r: sum(1 for d in decisions.values() if d["ruling"] == r and not d["stale"]) for r in RULINGS}
    parts = [f"{n} {r}" for r, n in counts.items() if n]
    open_targets = [t for t in TARGETS if t not in decisions or decisions[t]["stale"]]
    summary = ", ".join(parts) or "no current ruling"
    if open_targets:
        summary += "; still open: " + ", ".join(open_targets)
    return {"decisions": decisions, "history": (stored or {}).get("history", []), "stale": stale, "summary": summary}
