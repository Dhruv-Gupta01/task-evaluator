"""The Flag system (N15): runs the Flag rules, keeps one row per rule + key with a person's
ruling on it, and returns the record for the UI and the evidence bundle.

A re-run never drops a ruling: a flag found again keeps its status, a flag no longer found
is kept with present=False, and a flag found for the first time starts open."""

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import Flag, Run, Submission
from app.services import flag_rules

STATUSES = ("open", "accepted", "disputed")


def _other_canaries(db: Session, submission: Submission) -> dict[str, list[str]]:
    """Private canary GUID -> other tasks (by name) whose upload carries it."""
    out: dict[str, list[str]] = {}
    others = db.query(Submission).filter(Submission.id != submission.id, Submission.task_name != submission.task_name)
    for other in others:
        if not other.extracted_path or not Path(other.extracted_path).is_dir():
            continue
        for guid in flag_rules.canaries(Path(other.extracted_path)):
            if guid != flag_rules.PUBLIC_CANARY:
                out.setdefault(guid, []).append(other.task_name)
    return out


def previous_upload(db: Session, submission: Submission) -> Submission | None:
    """The latest earlier upload of the same task (what versioning compares against)."""
    return (
        db.query(Submission)
        .filter(
            Submission.task_name == submission.task_name,
            Submission.id != submission.id,
            Submission.uploaded_at < submission.uploaded_at,
            Submission.extracted_path.is_not(None),
        )
        .order_by(Submission.uploaded_at.desc())
        .first()
    )


def _rubric_results(db: Session, submission_id: str) -> list[dict] | None:
    run = db.query(Run).filter_by(submission_id=submission_id, kind="rubric_check").first()
    try:
        return (json.loads(run.logs) or {}).get("results") if run and run.logs else None
    except ValueError:
        return None


def _record_run(
    db: Session, submission_id: str, notes: dict, now: datetime, open_blockers: int, version: dict | None = None
) -> None:
    run = db.query(Run).filter_by(submission_id=submission_id, kind="flags", run_index=0).one_or_none()
    if run is None:
        run = Run(submission_id=submission_id, kind="flags", run_index=0)
        db.add(run)
    run.status = "failed" if open_blockers else "passed"
    run.started_at = run.started_at or now
    run.finished_at = now
    run.logs = json.dumps({"notes": notes, "at": now.isoformat(), "version": version})


def run_static(db: Session, submission: Submission) -> dict:
    """Runs every static rule on the extracted task and merges the findings into the flags table."""
    now = datetime.now(UTC)
    earlier = previous_upload(db, submission)
    ctx = flag_rules.load_context(
        Path(submission.extracted_path),
        other_canaries=_other_canaries(db, submission),
        rubric_results=_rubric_results(db, submission.id),
        previous_root=Path(earlier.extracted_path) if earlier else None,
    )
    results = flag_rules.evaluate(ctx)
    existing = {f.fingerprint: f for f in db.query(Flag).filter_by(submission_id=submission.id)}
    found = set()
    for rule_id, result in results.items():
        rule = flag_rules.RULES[rule_id]
        for finding in result.findings:
            fingerprint = f"{rule_id}:{finding.key}" if finding.key else rule_id
            found.add(fingerprint)
            flag = existing.get(fingerprint)
            if flag is None:
                flag = Flag(
                    submission_id=submission.id, rule=rule_id, check=rule.check, fingerprint=fingerprint,
                    status="open", first_seen=now, history_json="[]",
                )
                db.add(flag)
            flag.severity = finding.severity or rule.severity
            flag.title = rule.title
            flag.detail = finding.detail
            flag.evidence_json = json.dumps(finding.evidence)
            flag.suggested_status = finding.suggested_status
            flag.present = True
            flag.last_seen = now
    for fingerprint, flag in existing.items():
        if flag.rule in results and fingerprint not in found:
            flag.present = False
    db.flush()
    open_blockers = sum(
        1 for f in db.query(Flag).filter_by(submission_id=submission.id)
        if f.present and f.severity == flag_rules.BLOCKER and f.status == "open"
    )
    version = None
    if earlier is not None:
        from app.services import versioning

        version = {"against": earlier.id, **versioning.compare(Path(earlier.extracted_path), Path(submission.extracted_path))}
    _record_run(db, submission.id, {r: res.note for r, res in results.items()}, now, open_blockers, version)
    db.commit()
    return record(db, submission.id)


def set_status(db: Session, flag: Flag, status: str, by: str, reason: str) -> None:
    """A person's ruling. Accepting or disputing needs a name and a reason; reopening needs a name."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    by, reason = by.strip(), reason.strip()
    if not by:
        raise ValueError("say who is making this ruling")
    if status in ("accepted", "disputed") and not reason:
        raise ValueError(f"a reason is required to mark a flag {status}")
    now = datetime.now(UTC)
    history = json.loads(flag.history_json or "[]")
    history.append({"status": status, "by": by, "reason": reason, "at": now.isoformat(), "from": flag.status})
    flag.status, flag.status_by, flag.status_reason, flag.status_at = status, by, reason, now
    flag.history_json = json.dumps(history)
    db.commit()


def _serialize(flag: Flag) -> dict:
    return {
        "id": flag.id,
        "rule": flag.rule,
        "check": flag.check,
        "severity": flag.severity,
        "title": flag.title,
        "detail": flag.detail,
        "evidence": json.loads(flag.evidence_json or "[]"),
        "present": flag.present,
        "suggested_status": flag.suggested_status,
        "status": flag.status,
        "status_by": flag.status_by,
        "status_reason": flag.status_reason,
        "status_at": flag.status_at.isoformat() if flag.status_at else None,
        "history": json.loads(flag.history_json or "[]"),
        "first_seen": flag.first_seen.isoformat(),
        "last_seen": flag.last_seen.isoformat(),
    }


_ORDER = {flag_rules.BLOCKER: 0, flag_rules.ADVISORY: 1}


def record(db: Session, submission_id: str) -> dict:
    """The record block: every flag with its ruling and history, counts, and every rule with
    whether it has run here (and what it looked at) or which batch builds it."""
    flags = sorted(
        db.query(Flag).filter_by(submission_id=submission_id),
        key=lambda f: (not f.present, _ORDER.get(f.severity, 2), [int(x) for x in f.rule.split(".")], f.fingerprint),
    )
    run = db.query(Run).filter_by(submission_id=submission_id, kind="flags", run_index=0).one_or_none()
    logs = json.loads(run.logs) if run and run.logs else {}
    notes = logs.get("notes") or {}
    live = [f for f in flags if f.present]
    return {
        "last_run": logs.get("at"),
        # S9: version class against the latest earlier upload of the same task, when there is one
        "version": logs.get("version"),
        "counts": {
            "open_blockers": sum(1 for f in live if f.severity == flag_rules.BLOCKER and f.status == "open"),
            "open_advisories": sum(1 for f in live if f.severity != flag_rules.BLOCKER and f.status == "open"),
            "accepted": sum(1 for f in live if f.status == "accepted"),
            "disputed": sum(1 for f in live if f.status == "disputed"),
            "no_longer_found": sum(1 for f in flags if not f.present),
        },
        "flags": [_serialize(f) for f in flags],
        "rules": [
            {
                "id": r.id, "check": r.check, "title": r.title, "severity": r.severity, "method": r.method,
                "batch": r.batch, "ran": r.id in notes, "note": notes.get(r.id),
            }
            for r in flag_rules.RULES.values()
        ],
    }
