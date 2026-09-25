import asyncio
import json
import shutil
import traceback
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_settings
from app.db import SessionLocal
from app.models import Run, Submission
from app.services import (
    budget,
    code_smell_judge,
    docker_orchestrator,
    harbor_runner,
    leakage_scan,
    review_report,
    sufficiency_judge,
    validation_service,
)
from app.services.llm import usage as llm_usage
from app.services.task_config import TaskConfig

settings = get_settings()


def _get_task_config(submission: Submission) -> TaskConfig:
    if submission.task_config_json:
        return TaskConfig.model_validate_json(submission.task_config_json)
    return TaskConfig()


# LiteLLM provider prefixes for deriving the agent model from LLM_PROVIDER.
_LITELLM_PREFIX = {
    "anthropic": "anthropic",
    "openai": "openai",
    "gemini": "gemini",
    "groq": "groq",
    "fireworks": "fireworks_ai",
}


def _agent_kwargs() -> dict[str, str]:
    """Options for `harbor run --agent-kwarg`. Harbor >= 0.23 rejects options
    an agent doesn't declare, so each agent only gets the ones it knows."""
    agent = settings.harbor_agent
    kwargs: dict[str, str] = {}
    if agent.startswith("terminus"):
        kwargs["max_turns"] = str(settings.llm_max_iters)
    if agent.startswith("terminus") or agent == "codex":
        if settings.agent_reasoning_effort:
            kwargs["reasoning_effort"] = settings.agent_reasoning_effort
    if agent == "codex":
        if settings.codex_version:
            kwargs["version"] = settings.codex_version
        if settings.codex_web_search:
            kwargs["web_search"] = settings.codex_web_search
    return kwargs


_CODEX_SETUP_TIMEOUT_SEC = 1200


def _agent_setup_timeout_sec() -> int:
    """0 means Harbor's own default."""
    if settings.agent_setup_timeout_sec:
        return settings.agent_setup_timeout_sec
    return _CODEX_SETUP_TIMEOUT_SEC if settings.harbor_agent == "codex" else 0


def _agent_timeout_sec(config: TaskConfig) -> float:
    """The agent timeout Harbor will enforce (see AGENT_TIMEOUT_SEC)."""
    return settings.agent_timeout_sec or config.agent.timeout_sec


def _run_cost_cap(db) -> tuple[float | None, str]:
    """The tightest dollar limit for one agent-trials run: the per-run budget
    and what's left of this month's budget."""
    caps: list[tuple[float, str]] = []
    if settings.agent_run_budget_usd > 0:
        caps.append((settings.agent_run_budget_usd, "AGENT_RUN_BUDGET_USD"))
    if settings.llm_budget_usd > 0:
        remaining = max(settings.llm_budget_usd - budget.spent_this_month(db), 0.0)
        caps.append((remaining, "what is left of LLM_BUDGET_USD this month"))
    if not caps:
        return None, ""
    return min(caps, key=lambda cap: cap[0])


def _agent_config_error() -> str | None:
    """A reason the configured agent can't run, or None."""
    if settings.harbor_agent == "codex":
        if not _agent_model().startswith("openai/"):
            return (
                f"Codex only works with OpenAI models, but the agent model is "
                f"'{_agent_model()}'. Set AGENT_MODEL=openai/<model> in backend/.env."
            )
        if not settings.openai_api_key:
            return "Codex needs OPENAI_API_KEY in backend/.env."
        if settings.harbor_network_mode not in ("", "public"):
            return (
                "Codex installs itself and calls the OpenAI API from inside the task "
                "container, so it needs HARBOR_NETWORK_MODE=public."
            )
    return None


def _agent_model() -> str:
    if settings.agent_model:
        return settings.agent_model
    prefix = _LITELLM_PREFIX.get(settings.llm_provider, settings.llm_provider)
    return f"{prefix}/{settings.llm_model}"


_STUCK_STATUSES = ("pending", "running")


def reset_stuck_rows() -> None:
    """On startup, any submission/run left in pending/running state has no
    in-process asyncio task that will ever finish it (the previous process
    is dead) — reset to failed with a clear note so the frontend doesn't
    show a permanently-spinning stage."""
    db = SessionLocal()
    try:
        stuck_submissions = (
            db.query(Submission).filter(Submission.build_status.in_(_STUCK_STATUSES)).all()
        )
        for s in stuck_submissions:
            s.build_status = "failed"
            s.build_logs = "interrupted by backend restart"

        stuck_runs = db.query(Run).filter(Run.status.in_(_STUCK_STATUSES)).all()
        for r in stuck_runs:
            r.status = "failed"
            r.reward = None
            r.logs = "interrupted by backend restart"
            r.finished_at = datetime.now(UTC)

        db.commit()
    finally:
        db.close()


@contextmanager
def _track_llm_spend(db, submission_id: str, stage: str):
    """Record the token usage of every LLM call made inside the block, even
    if the stage then fails. Rows are only added to the session here; the
    caller's next commit persists them."""
    calls = llm_usage.start()
    try:
        yield
    finally:
        budget.record_calls(db, submission_id, stage, calls)


def _get_or_create_run(db, submission_id: str, kind: str, run_index: int = 0) -> Run:
    run = (
        db.query(Run)
        .filter_by(submission_id=submission_id, kind=kind, run_index=run_index)
        .one_or_none()
    )
    if run is None:
        run = Run(submission_id=submission_id, kind=kind, run_index=run_index)
        db.add(run)
    return run


async def run_validate(submission_id: str) -> None:
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None:
            return

        submission.build_status = "running"
        db.commit()

        extract_dir = settings.storage_dir / "submissions" / submission_id / "extracted"
        zip_path = settings.storage_dir / "submissions" / submission_id / "raw.zip"

        result = validation_service.validate_zip(zip_path, extract_dir)

        if not result.ok:
            submission.build_status = "failed"
            submission.build_logs = "\n".join(result.errors)
            db.commit()
            return

        submission.extracted_path = str(result.task_root)
        submission.task_config_json = json.dumps(result.config.model_dump()) if result.config else None
        if result.task_name:
            submission.task_name = result.task_name
        db.commit()

        environment_dir = result.task_root / "environment"
        build_timeout_sec = (
            result.config.environment.build_timeout_sec
            if result.config
            else docker_orchestrator.DEFAULT_BUILD_TIMEOUT_SEC
        )
        try:
            image_tag, build_logs = await asyncio.to_thread(
                docker_orchestrator.build_image,
                environment_dir,
                submission_id,
                timeout_sec=build_timeout_sec,
            )
        except docker_orchestrator.DockerBuildError as e:
            submission.build_status = "failed"
            submission.build_logs = e.log_text
            db.commit()
            return

        submission.build_status = "passed"
        submission.image_tag = image_tag
        submission.build_logs = build_logs
        db.commit()
    except Exception:
        submission = db.get(Submission, submission_id)
        if submission is not None:
            submission.build_status = "failed"
            submission.build_logs = traceback.format_exc()
            db.commit()
    finally:
        db.close()


async def run_oracle(submission_id: str) -> None:
    await _run_harbor_gate(submission_id, kind="oracle")


async def run_nop(submission_id: str) -> None:
    await _run_harbor_gate(submission_id, kind="nop")


# Headroom over the task's own build + agent + verifier timeouts, which Harbor
# enforces itself; this outer limit only catches a hung harbor process.
_HARBOR_TIMEOUT_BUFFER_SEC = 600
# Per trial: agents that install themselves (Codex: apt, nvm, npm) before running.
_AGENT_SETUP_ALLOWANCE_SEC = 300


async def _run_harbor_gate(submission_id: str, kind: str) -> None:
    """Oracle/nop via `harbor run -a oracle|nop`, so these gates give the same
    verdict as the real grading pipeline. Harbor builds the image, runs the
    agent phase and the verifier in its own isolated containers."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return

        run = _get_or_create_run(db, submission_id, kind)
        run.status = "running"
        run.reward = None
        run.logs = None
        run.started_at = datetime.now(UTC)
        db.commit()

        config = _get_task_config(submission)
        timeout_sec = (
            config.environment.build_timeout_sec
            + _agent_timeout_sec(config)
            + config.verifier.timeout_sec
            + _HARBOR_TIMEOUT_BUFFER_SEC
        )

        jobs_dir = settings.storage_dir / "submissions" / submission_id / "runs" / kind / "harbor"
        if jobs_dir.exists():
            shutil.rmtree(jobs_dir)  # keep only the latest attempt's output

        result = await harbor_runner.run_single(
            Path(submission.extracted_path),
            kind,
            jobs_dir,
            f"{kind}-{uuid.uuid4().hex[:8]}",
            timeout_sec,
        )

        # The gate needs a perfect 1.0; any partial reward counts as 0.
        if result.reward is None:
            run.status = "failed"
            run.reward = None
        else:
            run.reward = 1 if result.reward >= 1.0 else 0
            run.status = "passed" if run.reward == 1 else "failed"
        run.logs = result.logs
        run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception:
        run = db.query(Run).filter_by(
            submission_id=submission_id, kind=kind, run_index=0
        ).one_or_none()
        if run is not None:
            run.status = "failed"
            run.reward = None
            run.logs = traceback.format_exc()
            run.finished_at = datetime.now(UTC)
            db.commit()
    finally:
        db.close()


async def run_agent_trials(submission_id: str, n: int, append: bool = False) -> None:
    """n trials of an LLM agent via one `harbor run` job. Harbor runs the
    agent inside the task container and only copies tests/ in afterwards to
    verify, so the agent never sees the tests. Each Run row is filled in as
    its trial finishes (completion order), with tokens and cost in its logs."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return

        runs = (
            db.query(Run)
            .filter_by(submission_id=submission_id, kind="agent", status="pending")
            .order_by(Run.run_index)
            .all()
        )  # the router pre-creates the n new rows as pending; earlier trials are left alone
        config_error = _agent_config_error()
        if config_error:
            for r in runs:
                r.status = "failed"
                r.reward = None
                r.logs = config_error
                r.finished_at = datetime.now(UTC)
            db.commit()
            return

        now = datetime.now(UTC)
        for r in runs:
            r.status = "running"
            r.reward = None
            r.logs = None
            r.started_at = now
        db.commit()

        config = _get_task_config(submission)
        concurrency = max(1, min(settings.agent_trials_concurrency, n))
        rounds = -(-n // concurrency)  # ceil
        timeout_sec = (
            config.environment.build_timeout_sec
            + rounds * (_agent_timeout_sec(config) + config.verifier.timeout_sec)
            + rounds * max(_AGENT_SETUP_ALLOWANCE_SEC, _agent_setup_timeout_sec())
            + _HARBOR_TIMEOUT_BUFFER_SEC
        )

        agent_kwargs = _agent_kwargs()

        jobs_dir = settings.storage_dir / "submissions" / submission_id / "runs" / "agent" / "harbor"
        if jobs_dir.exists() and not append:
            shutil.rmtree(jobs_dir)  # keep only the latest attempt's output

        pending = list(runs)
        model = _agent_model()

        async def on_trial(outcome: harbor_runner.TrialOutcome) -> str | None:
            if outcome.cost_usd is not None:
                budget.record(
                    db,
                    submission_id,
                    "agent_trials",
                    model,
                    outcome.cost_usd,
                    outcome.n_input_tokens,
                    outcome.n_cache_tokens,
                    outcome.n_output_tokens,
                )
            if pending:
                run = pending.pop(0)
                if outcome.reward is None:
                    run.status = "failed"
                    run.reward = None
                else:
                    run.reward = 1 if outcome.reward >= 1.0 else 0
                    run.status = "passed" if run.reward == 1 else "failed"
                run.logs = outcome.logs
                run.finished_at = datetime.now(UTC)
            db.commit()
            # Stop the rest of the job once the monthly cap is crossed.
            reason = budget.exceeded_message(db)
            return f"skipped: {reason}" if reason and pending else None

        cost_cap_usd, cost_cap_label = _run_cost_cap(db)
        result = await harbor_runner.run_job(
            Path(submission.extracted_path),
            settings.harbor_agent,
            jobs_dir,
            f"agent-{uuid.uuid4().hex[:8]}",
            timeout_sec,
            model=model,
            agent_kwargs=agent_kwargs,
            n_attempts=n,
            n_concurrent=concurrency,
            agent_setup_timeout_sec=_agent_setup_timeout_sec(),
            cost_cap_usd=cost_cap_usd,
            cost_cap_label=cost_cap_label,
            on_trial=on_trial,
        )

        for run in pending:  # trials that never produced a result
            run.status = "failed"
            run.reward = None
            run.logs = result.error or "harbor produced no result for this trial"
            run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception:
        runs = db.query(Run).filter_by(submission_id=submission_id, kind="agent").all()
        for r in runs:
            if r.status in _STUCK_STATUSES:
                r.status = "failed"
                r.reward = None
                r.logs = traceback.format_exc()
                r.finished_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()


async def run_sufficiency_check(submission_id: str) -> None:
    """Single-shot LLM-as-judge check: does the agent-visible material
    (instruction.md + environment/) contain or make derivable everything the
    hidden tests/ actually grade? No Docker involved — just file reads plus
    one non-agentic LLM call, so it only needs extracted_path to be set
    (i.e. validate ran far enough to extract the zip), independent of
    whether the Docker image itself built successfully."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return

        run = _get_or_create_run(db, submission_id, "sufficiency", 0)
        run.status = "running"
        run.reward = None
        run.logs = None
        run.started_at = datetime.now(UTC)
        db.commit()

        task_root = Path(submission.extracted_path)

        try:
            with _track_llm_spend(db, submission_id, "sufficiency"):
                verdict = await sufficiency_judge.run_sufficiency_judge(task_root)
        except Exception:
            run.status = "failed"
            run.reward = None
            run.logs = traceback.format_exc()
            run.finished_at = datetime.now(UTC)
            db.commit()
            return

        reward = 1 if verdict.get("passed") else 0
        gaps = verdict.get("gaps") or []
        logs_text = verdict.get("verdict", "")
        if gaps:
            logs_text += "\n\nGaps found:\n" + "\n".join(f"- {g}" for g in gaps)

        run.status = "passed" if reward == 1 else "failed"
        run.reward = reward
        run.logs = logs_text
        run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception:
        run = db.query(Run).filter_by(
            submission_id=submission_id, kind="sufficiency", run_index=0
        ).one_or_none()
        if run is not None:
            run.status = "failed"
            run.reward = None
            run.logs = traceback.format_exc()
            run.finished_at = datetime.now(UTC)
            db.commit()
    finally:
        db.close()


async def run_leakage_scan(submission_id: str) -> None:
    """Advisory-only regex scan for LLM-chat-leakage artifacts (assistant
    self-references, disclaimers, unfilled template placeholders) across
    every text file in the submission. No LLM call, no Docker — just file
    reads, so like sufficiency it only needs extracted_path to be set.
    A "failed" status here means artifacts were found, NOT that the
    submission is disqualified — never wire this into any pass/fail gate
    that blocks other stages; it's a flag for human review only."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return

        run = _get_or_create_run(db, submission_id, "leakage_scan", 0)
        run.status = "running"
        run.reward = None
        run.logs = None
        run.started_at = datetime.now(UTC)
        db.commit()

        task_root = Path(submission.extracted_path)

        try:
            result = leakage_scan.run_leakage_scan(task_root)
        except Exception:
            run.status = "failed"
            run.reward = None
            run.logs = traceback.format_exc()
            run.finished_at = datetime.now(UTC)
            db.commit()
            return

        reward = 1 if result.get("passed") else 0
        findings = result.get("findings") or []
        logs_text = result.get("verdict", "")
        if findings:
            logs_text += "\n\nFindings:\n" + "\n".join(f"- {f}" for f in findings)

        run.status = "passed" if reward == 1 else "failed"
        run.reward = reward
        run.logs = logs_text
        run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception:
        run = db.query(Run).filter_by(
            submission_id=submission_id, kind="leakage_scan", run_index=0
        ).one_or_none()
        if run is not None:
            run.status = "failed"
            run.reward = None
            run.logs = traceback.format_exc()
            run.finished_at = datetime.now(UTC)
            db.commit()
    finally:
        db.close()


async def run_code_smell_check(submission_id: str) -> None:
    """LLM judgment on whether the solution code reads as AI-generated and
    unedited — a fuzzier, lower-confidence cousin of leakage_scan. False
    positives are expected and accepted here; keep this stage's result
    clearly separate from leakage_scan's, never merged, since one is a
    verified fact and this one is a model's opinion. Only needs
    extracted_path, same minimal prerequisite as leakage_scan and
    sufficiency — runs independently of the rest of the pipeline."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return

        run = _get_or_create_run(db, submission_id, "code_smell", 0)
        run.status = "running"
        run.reward = None
        run.logs = None
        run.started_at = datetime.now(UTC)
        db.commit()

        task_root = Path(submission.extracted_path)

        try:
            with _track_llm_spend(db, submission_id, "code_smell"):
                verdict = await code_smell_judge.run_code_smell_judge(task_root)
        except Exception:
            run.status = "failed"
            run.reward = None
            run.logs = traceback.format_exc()
            run.finished_at = datetime.now(UTC)
            db.commit()
            return

        flagged = bool(verdict.get("likely_ai_generated"))
        reward = 0 if flagged else 1
        confidence = verdict.get("confidence", "low")
        reasoning = verdict.get("reasoning") or []
        logs_text = f"Likely AI-generated: {flagged} (confidence: {confidence})"
        if reasoning:
            logs_text += "\n\nReasoning:\n" + "\n".join(f"- {r}" for r in reasoning)

        run.status = "passed" if reward == 1 else "failed"
        run.reward = reward
        run.logs = logs_text
        run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception:
        run = db.query(Run).filter_by(
            submission_id=submission_id, kind="code_smell", run_index=0
        ).one_or_none()
        if run is not None:
            run.status = "failed"
            run.reward = None
            run.logs = traceback.format_exc()
            run.finished_at = datetime.now(UTC)
            db.commit()
    finally:
        db.close()


async def run_review_report(submission_id: str) -> None:
    """Final synthesis report: reads the already-completed platform report
    (Build/Oracle/Nop/Sufficiency/Agent Trials) plus mechanical static
    checks and an infra-marker scan, and asks the LLM to reason only over
    what's left — genuine vs. platform-noise failures, and what a candidate
    should change. Requires every prerequisite gate to be terminal
    (passed/failed); the router enforces this before enqueuing, this is a
    defensive second check in case the stage is ever invoked directly.
    "status: passed" here means the report was generated successfully — it
    is NOT a judgment on the submission itself; the actual verdict is in the
    report text (run.logs)."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return

        run = _get_or_create_run(db, submission_id, "review_report", 0)
        run.status = "running"
        run.reward = None
        run.logs = None
        run.started_at = datetime.now(UTC)
        db.commit()

        ready, missing = review_report.all_gates_terminal(submission)
        if not ready:
            run.status = "failed"
            run.reward = None
            run.logs = (
                "Cannot generate a review report yet — the following gates "
                f"haven't finished: {', '.join(missing)}."
            )
            run.finished_at = datetime.now(UTC)
            db.commit()
            return

        task_root = Path(submission.extracted_path)

        try:
            with _track_llm_spend(db, submission_id, "review_report"):
                result = await review_report.run_review_report(submission, task_root)
        except Exception:
            run.status = "failed"
            run.reward = None
            run.logs = traceback.format_exc()
            run.finished_at = datetime.now(UTC)
            db.commit()
            return

        run.status = "passed"
        run.reward = 1
        run.logs = result["report_markdown"]
        run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception:
        run = db.query(Run).filter_by(
            submission_id=submission_id, kind="review_report", run_index=0
        ).one_or_none()
        if run is not None:
            run.status = "failed"
            run.reward = None
            run.logs = traceback.format_exc()
            run.finished_at = datetime.now(UTC)
            db.commit()
    finally:
        db.close()
