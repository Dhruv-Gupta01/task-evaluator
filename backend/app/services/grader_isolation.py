"""Grader Isolation gate (way 2 of the Worker flag): turns the way-1 analysis into `ok`, `missing` or
`unclear`. Advisory unless GRADER_ISOLATION_ENFORCE is on. Nobody can overrule it: a task that does not
pass is revised and uploaded again."""
import hashlib
from pathlib import Path

from app.services.verifier_isolation import analyze_worker_requirement

OK_RESULTS = ("not_required", "present", "equivalent")
OVERRIDE_RESULTS = ("not_required", "equivalent", "missing")
_HEURISTIC_ONLY = "`python -m pytest` runs from"


def fingerprint(task_root: Path) -> str:
    """Hash of what decides the verdict: tests/, task.toml and the environment Dockerfile."""
    h = hashlib.sha256()
    files = [p for p in (task_root / "tests").rglob("*") if p.is_file()] if (task_root / "tests").is_dir() else []
    files += [p for p in (task_root / "task.toml", task_root / "environment" / "Dockerfile") if p.is_file()]
    for p in sorted(files, key=lambda p: p.relative_to(task_root).as_posix()):
        h.update(p.relative_to(task_root).as_posix().encode() + b"\0")
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"?")
        h.update(b"\0")
    return h.hexdigest()


def evaluate(task_root: Path) -> dict:
    a = analyze_worker_requirement(task_root)
    result = a["result"]
    if result in OK_RESULTS and not a["also_unprotected"]:
        state = "ok"
    elif result == "present":
        state = "unclear"  # a Worker, but agent code also runs outside it
    elif a["evidence"] and all(_HEURISTIC_ONLY in e for e in a["evidence"]):
        state = "unclear"  # only the shared-mode sys.path note, not a run of agent code
    else:
        state = "missing"
    return {
        "state": state,
        "analysis": result,
        "evidence": a["evidence"],
        "absent": a["absent"],
        "also_unprotected": a["also_unprotected"],
        "note": a["note"],
        "fingerprint": fingerprint(task_root),
    }


def gate_status(state: str) -> str:
    return "passed" if state == "ok" else "failed"


def blocks(state: str, enforce: bool) -> bool:
    return bool(enforce) and state != "ok"

