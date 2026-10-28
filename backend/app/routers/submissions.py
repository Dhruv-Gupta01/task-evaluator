import json
import uuid
from datetime import UTC, datetime

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile
from sqlalchemy import func, select
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import LlmSpend, Run, Submission
from app.schemas import SubmissionListItem, SubmissionSchema, UploadResponse
from app.services import budget, submission_diff, submission_service, task_runner
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
async def trigger_validate(
    submission_id: str, build: bool = True, db: Session = Depends(get_db)
) -> dict[str, str]:
    """build=false only extracts and checks the files: enough for the Docker-free stages
    (Static Checks, Sufficiency, Edge Cases) without building an image."""
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")

    submission.build_status = "pending"
    db.commit()

    await task_queue.submit(f"{submission_id}:validate", task_runner.run_validate(submission_id, build=build))
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
    submission_id: str, n: int = 3, stress: bool = False, db: Session = Depends(get_db)
) -> dict[str, str]:
    """Runs n oracle trials, sequentially, always appended to any earlier
    ones on this submission (Gate 1 needs 3 *consecutive* 1.0 runs, so every
    run's own result stays visible, not just the latest). stress=true runs
    them under CPU load (kind "oracle_stress"); those never count for
    the gate."""
    if n < 1 or n > settings.max_agent_trials:
        raise HTTPException(
            status_code=400, detail=f"n must be between 1 and {settings.max_agent_trials}"
        )
    _require_built_submission(submission_id, db)
    kind = "oracle_stress" if stress else "oracle"

    last = (
        db.query(func.max(Run.run_index))
        .filter_by(submission_id=submission_id, kind=kind)
        .scalar()
    )
    first_index = 0 if last is None else last + 1
    for run_index in range(first_index, first_index + n):
        db.add(Run(submission_id=submission_id, kind=kind, run_index=run_index, status="pending"))
    db.commit()

    await task_queue.submit(f"{submission_id}:{kind}", task_runner.run_oracle_trials(submission_id, n, stress))
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
    max_cost_usd: float | None = None,
    cost_mode: str = "total",
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Runs n agent trials. By default they replace any earlier trials;
    append=true keeps them and adds n more after the last one.
    reasoning_effort sets the agent's reasoning level for these trials only
    (default: AGENT_REASONING_EFFORT from .env). agent picks codex (AGENT_MODEL),
    claude-code (CLAUDE_AGENT_MODEL) or terminus-2 for these trials only
    (default: HARBOR_AGENT). max_cost_usd caps this run's cost instead of
    AGENT_RUN_BUDGET_USD: cost_mode "total" is the whole run, "per_trial" means
    max_cost_usd x n for the run (the job stops when that total is reached; a
    single trial is not cut off on its own, except Claude Code, which enforces
    its share itself). The monthly budget still applies on top."""
    if agent is not None and agent not in task_runner.AGENTS:
        raise HTTPException(
            status_code=400, detail=f"agent must be one of {', '.join(task_runner.AGENTS)}"
        )
    if reasoning_effort is not None and reasoning_effort not in task_runner.REASONING_EFFORTS:
        raise HTTPException(
            status_code=400,
            detail=f"reasoning_effort must be one of {', '.join(task_runner.REASONING_EFFORTS)}",
        )
    if cost_mode not in ("total", "per_trial"):
        raise HTTPException(status_code=400, detail="cost_mode must be total or per_trial")
    if max_cost_usd is not None and not 0 < max_cost_usd <= 10_000:
        raise HTTPException(status_code=400, detail="max_cost_usd must be between 0 and 10000")
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

    run_cap = max_cost_usd * n if max_cost_usd and cost_mode == "per_trial" else max_cost_usd
    await task_queue.submit(
        f"{submission_id}:agent",
        task_runner.run_agent_trials(submission_id, n, append, reasoning_effort, agent, run_cap),
    )
    return {"status": "started"}


@router.post("/submissions/{submission_id}/sufficiency", status_code=202)
async def trigger_sufficiency(
    submission_id: str, runs: int = 1, db: Session = Depends(get_db)
) -> dict[str, str]:
    """`runs` (1 to 3) independent judge calls; the majority decides and
    disagreement is reported. Cost grows with the number of runs."""
    if not 1 <= runs <= 3:
        raise HTTPException(status_code=400, detail="runs must be 1, 2 or 3")
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
        f"{submission_id}:sufficiency", task_runner.run_sufficiency_check(submission_id, runs)
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


@router.get("/agents")
def get_agent_models() -> dict[str, str]:
    """The model each agent choice in the UI will actually run, from .env."""
    return {a: task_runner._agent_model(a) for a in ("codex", "claude-code")}


@router.get("/agent-models")
def get_agent_model_choices() -> dict[str, list[str]]:
    """Per agent, the models the UI offers for one run: the .env model first."""
    return task_runner.model_choices()


@router.get("/budget")
def get_budget(db: Session = Depends(get_db)) -> dict[str, float]:
    """This calendar month's tracked LLM spend against LLM_BUDGET_USD (0 = no cap)."""
    return {
        "spent_usd": round(budget.spent_this_month(db), 4),
        "limit_usd": settings.llm_budget_usd,
    }


@router.post("/submissions/{submission_id}/failure-analysis", status_code=202)
async def trigger_failure_analysis(
    submission_id: str, rubric: str = "tb", db: Session = Depends(get_db)
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
    n: int = 1,
    max_cost_usd: float | None = None,
    model: str | None = None,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """n (1 to 3) trials, one after another, with an explicit directive to
    cheat spliced into the instruction, to confirm the anti-cheat design holds
    when the agent is told to cheat, not just when it happens to behave. They
    replace earlier cheat trials. max_cost_usd caps the whole run instead of
    AGENT_RUN_BUDGET_USD (the monthly budget still applies). agent picks codex,
    claude-code or terminus-2 for this trial only (default: HARBOR_AGENT).
    reasoning_effort overrides AGENT_REASONING_EFFORT for this trial only,
    same as agent-trials -- otherwise this trial silently inherits whatever
    that setting happens to be, which is easy to leave stale/low without
    noticing (confirmed live: a real cheat trial once ran at low effort
    purely because of that, not a deliberate choice). cheat_prompt="tb"
    splices in the vendored, more aggressive Terminal-Bench red-team-style
    directive instead of the platform's own hand-written one. model overrides
    the agent's .env model (AGENT_MODEL / CLAUDE_AGENT_MODEL) for this run only;
    leave it out to run the same model Agent Trials use."""
    if agent is not None and agent not in task_runner.AGENTS:
        raise HTTPException(
            status_code=400, detail=f"agent must be one of {', '.join(task_runner.AGENTS)}"
        )
    if model:
        model_error = task_runner.valid_model_override(agent or settings.harbor_agent, model)
        if model_error:
            raise HTTPException(status_code=400, detail=model_error)
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
    if not 1 <= n <= 3:
        raise HTTPException(status_code=400, detail="n must be 1, 2 or 3")
    if max_cost_usd is not None and not 0 < max_cost_usd <= 10_000:
        raise HTTPException(status_code=400, detail="max_cost_usd must be between 0 and 10000")
    _raise_if_over_budget(db, budget.exceeded_message(db))
    _require_built_submission(submission_id, db)

    db.query(Run).filter_by(submission_id=submission_id, kind="cheat_trial").delete()
    for run_index in range(n):
        db.add(Run(submission_id=submission_id, kind="cheat_trial", run_index=run_index, status="pending"))
    db.commit()

    await task_queue.submit(
        f"{submission_id}:cheat_trial",
        task_runner.run_cheat_trial(
            submission_id, agent, reasoning_effort, cheat_prompt, max_cost_usd, model
        ),
    )
    return {"status": "started"}


@router.post("/submissions/{submission_id}/rubric-check", status_code=202)
async def trigger_rubric_check(
    submission_id: str, rubric: str = "tb", db: Session = Depends(get_db)
) -> dict[str, str]:
    """`harbor check`: an evaluator agent scores the whole task against a
    rubric. Advisory. Needs ANTHROPIC_API_KEY (CHECK_AGENT defaults to
    claude-code). The default is the vendored 36-criterion Terminal-Bench
    rubric (4.1 A.4); rubric="default" uses Harbor's own 11-criterion one."""
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


@router.post("/submissions/{submission_id}/variant-probe", status_code=202)
async def trigger_variant_probe(submission_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    """Advisory. A model proposes forms of the Oracle's answer that the
    instruction allows; each runs through the real verifier as an Oracle trial
    on a temporary copy of the task (sequentially, up to 4). A rejected variant
    means the grader may accept fewer forms than the instruction does. Needs a
    passing Oracle run first, so a failure can't be blamed on a broken task."""
    _raise_if_over_budget(db, budget.judge_exceeded_message(db))
    _require_built_submission(submission_id, db)
    oracle_passed = (
        db.query(Run).filter_by(submission_id=submission_id, kind="oracle", reward=1).first() is not None
    )
    if not oracle_passed:
        raise HTTPException(status_code=400, detail="run the Oracle and get a pass first")

    run = db.query(Run).filter_by(submission_id=submission_id, kind="variant_probe", run_index=0).one_or_none()
    if run is None:
        run = Run(submission_id=submission_id, kind="variant_probe", run_index=0)
        db.add(run)
    run.status = "pending"
    db.commit()

    await task_queue.submit(
        f"{submission_id}:variant_probe", task_runner.run_variant_probe(submission_id)
    )
    return {"status": "started"}


@router.post("/submissions/{submission_id}/weak-probe", status_code=202)
async def trigger_weak_probe(submission_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    """Advisory. Up to 6 deliberately broken versions of the Oracle's answer
    run through the real verifier (one Oracle trial each, sequentially); each
    must score 0. One that scores 1 means the grader is weaker than the
    instruction. Needs a passing Oracle run first."""
    _raise_if_over_budget(db, budget.judge_exceeded_message(db))
    _require_built_submission(submission_id, db)
    if db.query(Run).filter_by(submission_id=submission_id, kind="oracle", reward=1).first() is None:
        raise HTTPException(status_code=400, detail="run the Oracle and get a pass first")
    run = db.query(Run).filter_by(submission_id=submission_id, kind="weak_probe", run_index=0).one_or_none()
    if run is None:
        run = Run(submission_id=submission_id, kind="weak_probe", run_index=0)
        db.add(run)
    run.status = "pending"
    db.commit()
    await task_queue.submit(f"{submission_id}:weak_probe", task_runner.run_weak_probe(submission_id))
    return {"status": "started"}


@router.post("/submissions/{submission_id}/edge-cases", status_code=202)
async def trigger_edge_cases(
    submission_id: str, probe: bool = False, probe_covered: bool = False, db: Session = Depends(get_db)
) -> dict[str, str]:
    """Advisory. One judge call lists the edge cases an expert would handle
    (from the instruction alone), a second checks each against the tests.
    Uncovered cases are findings for a human. No Docker needed, unless
    probe=true: then every uncovered case also runs through the real
    verifier as a deliberately wrong Oracle copy (needs an Oracle pass).
    probe_covered=true (with probe) probes the covered cases too, to check
    the judge's "covered" verdicts against the real grader."""
    _raise_if_over_budget(db, budget.judge_exceeded_message(db))
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")
    if submission.extracted_path is None:
        raise HTTPException(status_code=400, detail="validate the submission first (files must be extracted)")
    if probe_covered and not probe:
        raise HTTPException(status_code=400, detail="probe_covered needs probe=true")
    if probe:
        _require_built_submission(submission_id, db)
        if db.query(Run).filter_by(submission_id=submission_id, kind="oracle", reward=1).first() is None:
            raise HTTPException(status_code=400, detail="run the Oracle and get a pass first")
    run = db.query(Run).filter_by(submission_id=submission_id, kind="edge_cases", run_index=0).one_or_none()
    if run is None:
        run = Run(submission_id=submission_id, kind="edge_cases", run_index=0)
        db.add(run)
    run.status = "pending"
    db.commit()
    await task_queue.submit(f"{submission_id}:edge_cases", task_runner.run_edge_case_check(submission_id, probe, probe_covered))
    return {"status": "started"}


def _queue_row(db: Session, submission_id: str, kind: str) -> None:
    run = db.query(Run).filter_by(submission_id=submission_id, kind=kind, run_index=0).one_or_none()
    if run is None:
        run = Run(submission_id=submission_id, kind=kind, run_index=0)
        db.add(run)
    run.status = "pending"
    db.commit()


@router.post("/submissions/{submission_id}/answer-exposure", status_code=202)
async def trigger_answer_exposure(submission_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    """Advisory, no LLM, no Docker: can the agent find the answer in the files
    it is given (names, copies of solution/ or tests/ files, asserted values)?
    Uses the built image's file list too when Build recorded one."""
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")
    if submission.extracted_path is None:
        raise HTTPException(status_code=400, detail="validate the submission first (files must be extracted)")
    _queue_row(db, submission_id, "answer_exposure")
    await task_queue.submit(
        f"{submission_id}:answer_exposure", task_runner.run_answer_exposure_stage(submission_id)
    )
    return {"status": "started"}


@router.post("/submissions/{submission_id}/image-inventory", status_code=202)
async def trigger_image_inventory(submission_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    """List the built image's files and package versions again (Build does it
    automatically; this is for submissions built before that)."""
    _require_built_submission(submission_id, db)
    _queue_row(db, submission_id, "image_inventory")
    await task_queue.submit(
        f"{submission_id}:image_inventory", task_runner.run_image_inventory_stage(submission_id)
    )
    return {"status": "started"}


@router.post("/submissions/{submission_id}/fresh-build", status_code=202)
async def trigger_fresh_build(submission_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    """Advisory: does the task still build and solve today? A no-cache,
    freshly pulled build plus one Oracle run with every RUN step rebuilt."""
    _require_built_submission(submission_id, db)
    _queue_row(db, submission_id, "fresh_build")
    await task_queue.submit(f"{submission_id}:fresh_build", task_runner.run_fresh_build(submission_id))
    return {"status": "started"}


@router.get("/submissions/{submission_id}/changes")
def get_changes(submission_id: str, against: str | None = None, db: Session = Depends(get_db)) -> dict:
    """What changed since an earlier upload of the same task: files added,
    removed and changed, tests removed or added, instruction length. `against`
    names the earlier submission; by default the latest earlier upload with
    the same task name."""
    current = db.get(Submission, submission_id)
    if current is None or current.extracted_path is None:
        raise HTTPException(status_code=404, detail="submission not found or not extracted")
    if against:
        earlier = db.get(Submission, against)
    else:
        earlier = (
            db.query(Submission)
            .filter(
                Submission.task_name == current.task_name,
                Submission.id != current.id,
                Submission.uploaded_at < current.uploaded_at,
                Submission.extracted_path.is_not(None),
            )
            .order_by(Submission.uploaded_at.desc())
            .first()
        )
    if earlier is None or earlier.extracted_path is None:
        return {"against": None}
    result = submission_diff.diff_tasks(Path(earlier.extracted_path), Path(current.extracted_path))
    return {"against": earlier.id, "against_uploaded_at": earlier.uploaded_at.isoformat(), **result}


class ReviewerDecision(BaseModel):
    key: str
    decision: str  # grader_wrong | agent_wrong | hard_not_ambiguous | unsure
    note: str = ""


_DECISIONS = ("grader_wrong", "agent_wrong", "hard_not_ambiguous", "unsure")


@router.put("/submissions/{submission_id}/reviewer-decision")
def set_reviewer_decision(
    submission_id: str, body: ReviewerDecision, db: Session = Depends(get_db)
) -> dict:
    """Records a reviewer's ruling on a contested flag (key identifies the flag,
    e.g. the trials in a cluster). Kept with the submission; never changes a gate."""
    if db.get(Submission, submission_id) is None:
        raise HTTPException(status_code=404, detail="submission not found")
    if body.decision not in _DECISIONS:
        raise HTTPException(status_code=400, detail=f"decision must be one of {', '.join(_DECISIONS)}")
    if not body.key or len(body.key) > 500 or len(body.note) > 2000:
        raise HTTPException(status_code=400, detail="key or note is empty or too long")
    run = db.query(Run).filter_by(submission_id=submission_id, kind="reviewer_decision", run_index=0).one_or_none()
    if run is None:
        run = Run(submission_id=submission_id, kind="reviewer_decision", run_index=0, status="passed")
        db.add(run)
    decisions = submission_service._reviewer_decisions(run)
    decisions[body.key] = {
        "decision": body.decision,
        "note": body.note,
        "at": datetime.now(UTC).isoformat(),
    }
    run.logs = json.dumps(decisions)
    db.commit()
    return decisions


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
    spend_rows = db.query(LlmSpend).filter_by(submission_id=submission_id).all()
    total_cost_usd = sum(r.cost_usd for r in spend_rows) if spend_rows else None
    cost_report = submission_service.build_cost_report(spend_rows)

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
        "variant_probe": {
            "status": schema.variant_probe.status,
            "not_run": schema.variant_probe.status == "not-run",
            "report": _json_or_raw(schema.variant_probe.logs),
        },
        "weak_probe": {
            "status": schema.weak_probe.status,
            "not_run": schema.weak_probe.status == "not-run",
            "report": _json_or_raw(schema.weak_probe.logs),
        },
        "edge_cases": {
            "status": schema.edge_cases.status,
            "not_run": schema.edge_cases.status == "not-run",
            "report": _json_or_raw(schema.edge_cases.logs),
        },
        "reviewer_decisions": schema.reviewer_decisions,
        "rubric_check": {
            "status": schema.rubric_check.status,
            "not_run": schema.rubric_check.status == "not-run",
            "report": _json_or_raw(schema.rubric_check.logs),
        },
        "run_limits": submission_service.run_limits(submission, schema),
        "oracle": {
            "flaky": schema.oracle.flaky,
            "flaky_note": schema.oracle.flaky_note,
            "stress_note": schema.oracle.stress_note,
            "expected_tests": schema.oracle.expected_tests,
        },
        # Per-test results, score-details files and flags of every Harbor run.
        "verifier_reports": submission_service.verifier_reports(schema),
        "cheat_trial": {
            "status": schema.cheat_trial.status,
            "summary": schema.cheat_trial.summary,
            "trials": [
                {k: v for k, v in (_json_or_raw(r.logs) or {}).items() if k != "trial_log"}
                if isinstance(_json_or_raw(r.logs), dict) else None
                for r in schema.cheat_trial.runs
            ],
        },
        "answer_exposure": {
            "status": schema.answer_exposure.status,
            "not_run": schema.answer_exposure.status == "not-run",
            "report": _json_or_raw(schema.answer_exposure.logs),
        },
        "fresh_build": {
            "status": schema.fresh_build.status,
            "not_run": schema.fresh_build.status == "not-run",
            "report": _json_or_raw(schema.fresh_build.logs),
        },
        # Installed package versions in the built image (and its task files).
        "image_inventory": {
            "status": schema.image_inventory.status,
            "not_run": schema.image_inventory.status == "not-run",
            "report": _json_or_raw(schema.image_inventory.logs),
        },
        "cost": cost_report,
        "summary_markdown": submission_service.build_summary_markdown(
            schema, total_cost_usd, cost_report
        ),
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
