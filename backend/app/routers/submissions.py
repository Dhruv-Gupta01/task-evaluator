import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import LlmSpend, Run, Submission
from app.schemas import SubmissionListItem, SubmissionSchema, UploadResponse
from app.services import budget, submission_service, task_runner
from app.workers import task_queue

router = APIRouter()
settings = get_settings()


def _raise_if_over_budget(db: Session, reason: str | None) -> None:
    if reason:
        raise HTTPException(status_code=400, detail=reason)


@router.get("/submissions", response_model=list[SubmissionListItem])
def list_submissions(db: Session = Depends(get_db)) -> list[SubmissionListItem]:
    rows = db.execute(
        select(Submission).order_by(Submission.uploaded_at.desc())
    ).scalars().all()
    return [submission_service.to_list_item(s) for s in rows]


@router.get("/submissions/{submission_id}", response_model=SubmissionSchema)
def get_submission(submission_id: str, db: Session = Depends(get_db)) -> SubmissionSchema:
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")
    return submission_service.to_schema(submission)


@router.post("/submissions", response_model=UploadResponse, status_code=201)
async def upload_submission(file: UploadFile, db: Session = Depends(get_db)) -> UploadResponse:
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="file must be a .zip")

    submission_id = str(uuid.uuid4())
    submission_dir = settings.storage_dir / "submissions" / submission_id
    submission_dir.mkdir(parents=True, exist_ok=True)
    zip_path = submission_dir / "raw.zip"

    contents = await file.read()
    zip_path.write_bytes(contents)

    task_name = file.filename.rsplit(".", 1)[0]
    submission = Submission(
        id=submission_id,
        task_name=task_name,
        uploaded_at=datetime.now(UTC),
        zip_path=str(zip_path),
        build_status="not-run",
    )
    db.add(submission)
    db.commit()

    return UploadResponse(id=submission_id)


@router.post("/submissions/{submission_id}/validate", status_code=202)
async def trigger_validate(submission_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")

    submission.build_status = "pending"
    db.commit()

    await task_queue.submit(f"{submission_id}:validate", task_runner.run_validate(submission_id))
    return {"status": "started"}


def _require_built_submission(submission_id: str, db: Session) -> Submission:
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")
    if submission.image_tag is None:
        raise HTTPException(status_code=400, detail="build the image first (run Validate & Build)")
    return submission


@router.post("/submissions/{submission_id}/oracle", status_code=202)
async def trigger_oracle(
    submission_id: str, n: int = 3, db: Session = Depends(get_db)
) -> dict[str, str]:
    """Runs n oracle trials, sequentially, always appended to any earlier
    ones on this submission (Gate 1 needs 3 *consecutive* 1.0 runs, so every
    run's own result stays visible, not just the latest)."""
    if n < 1 or n > settings.max_agent_trials:
        raise HTTPException(
            status_code=400, detail=f"n must be between 1 and {settings.max_agent_trials}"
        )
    _require_built_submission(submission_id, db)

    last = (
        db.query(func.max(Run.run_index))
        .filter_by(submission_id=submission_id, kind="oracle")
        .scalar()
    )
    first_index = 0 if last is None else last + 1
    for run_index in range(first_index, first_index + n):
        db.add(Run(submission_id=submission_id, kind="oracle", run_index=run_index, status="pending"))
    db.commit()

    await task_queue.submit(f"{submission_id}:oracle", task_runner.run_oracle_trials(submission_id, n))
    return {"status": "started"}


@router.post("/submissions/{submission_id}/nop", status_code=202)
async def trigger_nop(submission_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    _require_built_submission(submission_id, db)
    await task_queue.submit(f"{submission_id}:nop", task_runner.run_nop(submission_id))
    return {"status": "started"}


@router.post("/submissions/{submission_id}/agent-trials", status_code=202)
async def trigger_agent_trials(
    submission_id: str,
    n: int = 5,
    append: bool = False,
    reasoning_effort: str | None = None,
    agent: str | None = None,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Runs n agent trials. By default they replace any earlier trials;
    append=true keeps them and adds n more after the last one.
    reasoning_effort sets the agent's reasoning level for these trials only
    (default: AGENT_REASONING_EFFORT from .env). agent picks codex (AGENT_MODEL),
    claude-code (CLAUDE_AGENT_MODEL) or terminus-2 for these trials only
    (default: HARBOR_AGENT)."""
    if agent is not None and agent not in task_runner.AGENTS:
        raise HTTPException(
            status_code=400, detail=f"agent must be one of {', '.join(task_runner.AGENTS)}"
        )
    if reasoning_effort is not None and reasoning_effort not in task_runner.REASONING_EFFORTS:
        raise HTTPException(
            status_code=400,
            detail=f"reasoning_effort must be one of {', '.join(task_runner.REASONING_EFFORTS)}",
        )
    if n < 1 or n > settings.max_agent_trials:
        raise HTTPException(
            status_code=400, detail=f"n must be between 1 and {settings.max_agent_trials}"
        )
    _require_built_submission(submission_id, db)
    _raise_if_over_budget(db, budget.exceeded_message(db))

    if append:
        last = (
            db.query(func.max(Run.run_index))
            .filter_by(submission_id=submission_id, kind="agent")
            .scalar()
        )
        first_index = 0 if last is None else last + 1
    else:  # a plain re-run overwrites prior agent trial results
        db.query(Run).filter_by(submission_id=submission_id, kind="agent").delete()
        first_index = 0
    for run_index in range(first_index, first_index + n):
        db.add(Run(submission_id=submission_id, kind="agent", run_index=run_index, status="pending"))
    db.commit()

    await task_queue.submit(
        f"{submission_id}:agent", task_runner.run_agent_trials(submission_id, n, append, reasoning_effort, agent)
    )
    return {"status": "started"}


@router.post("/submissions/{submission_id}/sufficiency", status_code=202)
async def trigger_sufficiency(submission_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    _raise_if_over_budget(db, budget.judge_exceeded_message(db))
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")
    if submission.extracted_path is None:
        raise HTTPException(
            status_code=400, detail="validate the submission first (files must be extracted)"
        )

    run = (
        db.query(Run).filter_by(submission_id=submission_id, kind="sufficiency", run_index=0).one_or_none()
    )
    if run is None:
        run = Run(submission_id=submission_id, kind="sufficiency", run_index=0)
        db.add(run)
    run.status = "pending"
    db.commit()

    await task_queue.submit(
        f"{submission_id}:sufficiency", task_runner.run_sufficiency_check(submission_id)
    )
    return {"status": "started"}


@router.post("/submissions/{submission_id}/leakage-scan", status_code=202)
async def trigger_leakage_scan(submission_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")
    if submission.extracted_path is None:
        raise HTTPException(
            status_code=400, detail="validate the submission first (files must be extracted)"
        )

    run = (
        db.query(Run).filter_by(submission_id=submission_id, kind="leakage_scan", run_index=0).one_or_none()
    )
    if run is None:
        run = Run(submission_id=submission_id, kind="leakage_scan", run_index=0)
        db.add(run)
    run.status = "pending"
    db.commit()

    await task_queue.submit(
        f"{submission_id}:leakage_scan", task_runner.run_leakage_scan(submission_id)
    )
    return {"status": "started"}


@router.post("/submissions/{submission_id}/code-smell", status_code=202)
async def trigger_code_smell(submission_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    _raise_if_over_budget(db, budget.judge_exceeded_message(db))
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")
    if submission.extracted_path is None:
        raise HTTPException(
            status_code=400, detail="validate the submission first (files must be extracted)"
        )

    run = (
        db.query(Run).filter_by(submission_id=submission_id, kind="code_smell", run_index=0).one_or_none()
    )
    if run is None:
        run = Run(submission_id=submission_id, kind="code_smell", run_index=0)
        db.add(run)
    run.status = "pending"
    db.commit()

    await task_queue.submit(
        f"{submission_id}:code_smell", task_runner.run_code_smell_check(submission_id)
    )
    return {"status": "started"}


_REVIEW_REPORT_TERMINAL = {"passed", "failed"}


@router.post("/submissions/{submission_id}/review-report", status_code=202)
async def trigger_review_report(submission_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    _raise_if_over_budget(db, budget.judge_exceeded_message(db))
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")
    if submission.extracted_path is None:
        raise HTTPException(
            status_code=400, detail="validate the submission first (files must be extracted)"
        )

    schema = submission_service.to_schema(submission)
    missing = []
    if schema.build.status not in _REVIEW_REPORT_TERMINAL:
        missing.append("build")
    if schema.oracle.status not in _REVIEW_REPORT_TERMINAL:
        missing.append("oracle")
    if schema.nop.status not in _REVIEW_REPORT_TERMINAL:
        missing.append("nop")
    if schema.sufficiency.status not in _REVIEW_REPORT_TERMINAL:
        missing.append("sufficiency")
    if schema.agent_trials.status not in _REVIEW_REPORT_TERMINAL:
        missing.append("agent_trials")
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"run these gates to completion first: {', '.join(missing)}",
        )

    run = (
        db.query(Run).filter_by(submission_id=submission_id, kind="review_report", run_index=0).one_or_none()
    )
    if run is None:
        run = Run(submission_id=submission_id, kind="review_report", run_index=0)
        db.add(run)
    run.status = "pending"
    db.commit()

    await task_queue.submit(
        f"{submission_id}:review_report", task_runner.run_review_report(submission_id)
    )
    return {"status": "started"}


@router.get("/budget")
def get_budget(db: Session = Depends(get_db)) -> dict[str, float]:
    """This calendar month's tracked LLM spend against LLM_BUDGET_USD (0 = no cap)."""
    return {
        "spent_usd": round(budget.spent_this_month(db), 4),
        "limit_usd": settings.llm_budget_usd,
    }


@router.post("/submissions/{submission_id}/failure-analysis", status_code=202)
async def trigger_failure_analysis(
    submission_id: str, rubric: str = "default", db: Session = Depends(get_db)
) -> dict[str, str]:
    """`harbor analyze` on the finished agent trials. Advisory, like the other
    LLM stages; it costs a little (a cheap model) and counts toward the
    budget. rubric="tb" swaps in the vendored, richer 6-criterion
    Terminal-Bench rubric instead of Harbor's own default 2-criterion one."""
    if rubric not in task_runner.RUBRICS:
        raise HTTPException(
            status_code=400, detail=f"rubric must be one of {', '.join(task_runner.RUBRICS)}"
        )
    _raise_if_over_budget(db, budget.exceeded_message(db))
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")
    agent_status = submission_service.to_schema(submission).agent_trials.status
    if agent_status not in _REVIEW_REPORT_TERMINAL:
        raise HTTPException(status_code=400, detail="run the agent trials to completion first")

    run = (
        db.query(Run).filter_by(submission_id=submission_id, kind="failure_analysis", run_index=0).one_or_none()
    )
    if run is None:
        run = Run(submission_id=submission_id, kind="failure_analysis", run_index=0)
        db.add(run)
    run.status = "pending"
    db.commit()

    await task_queue.submit(
        f"{submission_id}:failure_analysis", task_runner.run_failure_analysis(submission_id, rubric)
    )
    return {"status": "started"}


@router.post("/submissions/{submission_id}/cheat-trial", status_code=202)
async def trigger_cheat_trial(
    submission_id: str,
    agent: str | None = None,
    reasoning_effort: str | None = None,
    cheat_prompt: str = "default",
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """One trial run with an explicit directive to cheat spliced into the
    instruction, to confirm the anti-cheat design holds when the agent is told
    to cheat, not just when it happens to behave. agent picks codex,
    claude-code or terminus-2 for this trial only (default: HARBOR_AGENT).
    reasoning_effort overrides AGENT_REASONING_EFFORT for this trial only,
    same as agent-trials -- otherwise this trial silently inherits whatever
    that setting happens to be, which is easy to leave stale/low without
    noticing (confirmed live: a real cheat trial once ran at low effort
    purely because of that, not a deliberate choice). cheat_prompt="tb"
    splices in the vendored, more aggressive Terminal-Bench red-team-style
    directive instead of the platform's own hand-written one."""
    if agent is not None and agent not in task_runner.AGENTS:
        raise HTTPException(
            status_code=400, detail=f"agent must be one of {', '.join(task_runner.AGENTS)}"
        )
    if reasoning_effort is not None and reasoning_effort not in task_runner.REASONING_EFFORTS:
        raise HTTPException(
            status_code=400,
            detail=f"reasoning_effort must be one of {', '.join(task_runner.REASONING_EFFORTS)}",
        )
    if cheat_prompt not in task_runner.CHEAT_PROMPTS:
        raise HTTPException(
            status_code=400,
            detail=f"cheat_prompt must be one of {', '.join(task_runner.CHEAT_PROMPTS)}",
        )
    _raise_if_over_budget(db, budget.exceeded_message(db))
    _require_built_submission(submission_id, db)

    run = (
        db.query(Run).filter_by(submission_id=submission_id, kind="cheat_trial", run_index=0).one_or_none()
    )
    if run is None:
        run = Run(submission_id=submission_id, kind="cheat_trial", run_index=0)
        db.add(run)
    run.status = "pending"
    db.commit()

    await task_queue.submit(
        f"{submission_id}:cheat_trial",
        task_runner.run_cheat_trial(submission_id, agent, reasoning_effort, cheat_prompt),
    )
    return {"status": "started"}


@router.post("/submissions/{submission_id}/rubric-check", status_code=202)
async def trigger_rubric_check(
    submission_id: str, rubric: str = "default", db: Session = Depends(get_db)
) -> dict[str, str]:
    """`harbor check`: an evaluator agent scores the whole task against a
    rubric. Advisory. Needs ANTHROPIC_API_KEY (CHECK_AGENT defaults to
    claude-code). rubric="tb" swaps in the vendored, richer 35-criterion
    Terminal-Bench rubric instead of Harbor's own default 11-criterion one."""
    if rubric not in task_runner.RUBRICS:
        raise HTTPException(
            status_code=400, detail=f"rubric must be one of {', '.join(task_runner.RUBRICS)}"
        )
    _raise_if_over_budget(db, budget.exceeded_message(db))
    _require_built_submission(submission_id, db)

    run = (
        db.query(Run).filter_by(submission_id=submission_id, kind="rubric_check", run_index=0).one_or_none()
    )
    if run is None:
        run = Run(submission_id=submission_id, kind="rubric_check", run_index=0)
        db.add(run)
    run.status = "pending"
    db.commit()

    await task_queue.submit(
        f"{submission_id}:rubric_check", task_runner.run_rubric_check(submission_id, rubric)
    )
    return {"status": "started"}


@router.post("/submissions/{submission_id}/static-checks", status_code=202)
async def trigger_static_checks(submission_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    """Mechanical, LLM-free checks over instruction.md/test.sh/Dockerfile/zip
    layout. No Docker, no LLM budget -- only needs the submission extracted."""
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")
    if submission.extracted_path is None:
        raise HTTPException(
            status_code=400, detail="validate the submission first (files must be extracted)"
        )

    run = (
        db.query(Run).filter_by(submission_id=submission_id, kind="static_checks", run_index=0).one_or_none()
    )
    if run is None:
        run = Run(submission_id=submission_id, kind="static_checks", run_index=0)
        db.add(run)
    run.status = "pending"
    db.commit()

    await task_queue.submit(
        f"{submission_id}:static_checks", task_runner.run_static_checks_stage(submission_id)
    )
    return {"status": "started"}


@router.post("/submissions/{submission_id}/tb-static-checks", status_code=202)
async def trigger_tb_static_checks(submission_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    """Terminal-Bench's own official static checks (vendor/tb_checks/). No
    Docker, no LLM budget -- only needs the submission extracted. Separate
    from static-checks above, which runs a different, platform-specific rule
    set -- neither replaces the other."""
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")
    if submission.extracted_path is None:
        raise HTTPException(
            status_code=400, detail="validate the submission first (files must be extracted)"
        )

    run = (
        db.query(Run).filter_by(submission_id=submission_id, kind="tb_static_checks", run_index=0).one_or_none()
    )
    if run is None:
        run = Run(submission_id=submission_id, kind="tb_static_checks", run_index=0)
        db.add(run)
    run.status = "pending"
    db.commit()

    await task_queue.submit(
        f"{submission_id}:tb_static_checks", task_runner.run_tb_static_checks_stage(submission_id)
    )
    return {"status": "started"}


@router.post("/submissions/{submission_id}/ai-detection", status_code=202)
async def trigger_ai_detection(submission_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    """Terminal-Bench's own Layer 3 AI-usage detection (vendor/tb_checks/,
    calls the real GPTZero API). No Docker, no LLM budget -- only needs the
    submission extracted. Genuinely optional in Terminal-Bench's own
    workflow too; runs a graceful no-op if GPTZERO_API_KEY isn't set."""
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")
    if submission.extracted_path is None:
        raise HTTPException(
            status_code=400, detail="validate the submission first (files must be extracted)"
        )

    run = (
        db.query(Run).filter_by(submission_id=submission_id, kind="ai_detection", run_index=0).one_or_none()
    )
    if run is None:
        run = Run(submission_id=submission_id, kind="ai_detection", run_index=0)
        db.add(run)
    run.status = "pending"
    db.commit()

    await task_queue.submit(
        f"{submission_id}:ai_detection", task_runner.run_ai_detection_stage(submission_id)
    )
    return {"status": "started"}


@router.get("/submissions/{submission_id}/evidence")
def get_evidence_bundle(submission_id: str, db: Session = Depends(get_db)) -> Response:
    """C5: one downloadable bundle of the evidence a task review needs --
    the Docker build log, the static checks output, and the harbor check
    rubric review -- instead of a human pulling each one out of the UI/DB
    by hand. Each section is null with a `not_run` note if that stage
    hasn't been triggered yet, rather than silently omitted. Also includes
    a C8 auto-filled summary/README rendered from the same data, so nothing
    manually assembled from this bundle needs a [fill] placeholder."""
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")

    schema = submission_service.to_schema(submission)
    total_cost_usd = db.query(func.sum(LlmSpend.cost_usd)).filter_by(
        submission_id=submission_id
    ).scalar()

    def _json_or_raw(logs: str | None) -> dict | str | None:
        if logs is None:
            return None
        try:
            return json.loads(logs)
        except json.JSONDecodeError:
            return logs

    bundle = {
        "submission_id": submission_id,
        "task_name": submission.task_name,
        "checksum": schema.checksum.model_dump(),
        "build": {
            "status": schema.build.status,
            "log": schema.build.logs,
        },
        "static_checks": {
            "status": schema.static_checks.status,
            "not_run": schema.static_checks.status == "not-run",
            "report": _json_or_raw(schema.static_checks.logs),
        },
        "tb_static_checks": {
            "status": schema.tb_static_checks.status,
            "not_run": schema.tb_static_checks.status == "not-run",
            "report": _json_or_raw(schema.tb_static_checks.logs),
        },
        "rubric_check": {
            "status": schema.rubric_check.status,
            "not_run": schema.rubric_check.status == "not-run",
            "report": _json_or_raw(schema.rubric_check.logs),
        },
        "summary_markdown": submission_service.build_summary_markdown(schema, total_cost_usd),
    }
    # C10: strip host usernames from every absolute /Users|home/<user>/...
    # path before this leaves the platform -- the raw versions are fine in
    # the UI/DB, just never in something meant for outside reading.
    bundle = submission_service.redact_host_paths(bundle)
    body = json.dumps(bundle, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{submission_id}-evidence.json"'
        },
    )
