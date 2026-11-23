"""The Grader Isolation gate (way 2 of the Worker flag).

It reads `verifier_isolation.analyze_worker_requirement` (way 1) and turns it into a gate state:
`ok` (not_required / present / equivalent), `missing`, or `unclear` (the detection is not sure). A
reviewer can overrule it with a written reason; the ruling is kept against a fingerprint of the
grader's files, so a changed verifier is always checked again. Static and heuristic, so it is
advisory unless GRADER_ISOLATION_ENFORCE is on.
"""
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


def override_error(result: str, reason: str, adjudicator: str) -> str | None:
    if result not in OVERRIDE_RESULTS:
        return f"result must be one of {', '.join(OVERRIDE_RESULTS)}"
    if not reason.strip():
        return "a written reason is required"
    if len(reason) > 2000:
        return "the reason is too long (2000 characters at most)"
    if not adjudicator.strip():
        return "the adjudicator's name is required"
    return None


def apply_override(evaluation: dict, override: dict | None, current_fingerprint: str) -> dict:
    """The effective state: the reviewer's ruling when it was made on the grader as it is now."""
    out = {**evaluation, "source": "detected", "override": None, "override_stale": False}
    if not override:
        return out
    if override.get("fingerprint") != current_fingerprint:
        out["override_stale"] = True
        out["override"] = override
        return out
    out["override"] = override
    out["source"] = "reviewer"
    out["state"] = "missing" if override.get("result") == "missing" else "ok"
    return out
