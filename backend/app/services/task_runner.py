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
    infra_marker_scan,
    leakage_scan,
    review_report,
    static_checks,
    sufficiency_judge,
    tb_static_checks,
    validation_service,
)
from app.services.llm import factory as llm_factory
from app.services.llm import usage as llm_usage
from app.services.llm.base import Message
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


# Reasoning levels the UI offers for one run of trials. Codex, Claude Code and
# Terminus all accept these.
REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Agents a run of trials can pick (default: HARBOR_AGENT).
AGENTS = ("codex", "claude-code", "terminus-2")

# Vendored Terminal-Bench prompts/rubrics (backend/vendor/tb_prompts/, see
# PROVENANCE.md there) -- an alternate, richer rubric a run can opt into,
# never the default. "default" = Harbor's own built-in rubric/prompt.
RUBRICS = ("default", "tb")
CHEAT_PROMPTS = ("default", "tb")
_TB_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "vendor" / "tb_prompts"
_TB_ANALYZE_RUBRIC = _TB_PROMPTS_DIR / "trial-analysis.toml"
_TB_ANALYZE_PROMPT = _TB_PROMPTS_DIR / "trial-analysis.txt"
_TB_ANALYZE_JOB_PROMPT = _TB_PROMPTS_DIR / "trial-analysis-job.txt"
_TB_CHECK_RUBRIC = _TB_PROMPTS_DIR / "task-implementation.toml"
_TB_CHEAT_PROMPT = _TB_PROMPTS_DIR / "hack-trial-prompt.md"


def _agent_kwargs(agent: str, reasoning_effort: str | None = None) -> dict[str, str]:
    """Options for `harbor run --agent-kwarg`. Harbor >= 0.23 rejects options
    an agent doesn't declare, so each agent only gets the ones it knows.
    reasoning_effort overrides AGENT_REASONING_EFFORT for this run only."""
    kwargs: dict[str, str] = {}
    if agent.startswith("terminus"):
        kwargs["max_turns"] = str(settings.llm_max_iters)
    if agent.startswith("terminus") or agent in ("codex", "claude-code"):
        effort = reasoning_effort or settings.agent_reasoning_effort
        if effort:
            kwargs["reasoning_effort"] = effort
    if agent == "codex":
        if settings.codex_version:
            kwargs["version"] = settings.codex_version
        if settings.codex_web_search:
            kwargs["web_search"] = settings.codex_web_search
    return kwargs


# Codex and Claude Code install themselves inside the container (Node, npm or a
# download), which can take longer than Harbor's 360s default.
_INSTALLED_AGENT_SETUP_TIMEOUT_SEC = 1200


def _agent_setup_timeout_sec(agent: str) -> int:
    """0 means Harbor's own default."""
    if settings.agent_setup_timeout_sec:
        return settings.agent_setup_timeout_sec
    return _INSTALLED_AGENT_SETUP_TIMEOUT_SEC if agent in ("codex", "claude-code") else 0


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


def _agent_config_error(agent: str) -> str | None:
    """A reason the chosen agent can't run, or None."""
    if agent == "claude-code":
        if not _agent_model(agent).startswith("anthropic/"):
            return (
                f"Claude Code only works with Anthropic models, but CLAUDE_AGENT_MODEL is "
                f"'{_agent_model(agent)}'. Set CLAUDE_AGENT_MODEL=anthropic/<model> in backend/.env."
            )
        if not settings.anthropic_api_key:
            return "Claude Code needs ANTHROPIC_API_KEY in backend/.env."
        if settings.harbor_network_mode not in ("", "public"):
            return (
                "Claude Code installs itself and calls the Anthropic API from inside the "
                "task container, so it needs HARBOR_NETWORK_MODE=public."
            )
    if agent == "codex":
        if not _agent_model(agent).startswith("openai/"):
            return (
                f"Codex only works with OpenAI models, but the agent model is "
                f"'{_agent_model(agent)}'. Set AGENT_MODEL=openai/<model> in backend/.env."
            )
        if not settings.openai_api_key:
            return "Codex needs OPENAI_API_KEY in backend/.env."
        if settings.harbor_network_mode not in ("", "public"):
            return (
                "Codex installs itself and calls the OpenAI API from inside the task "
                "container, so it needs HARBOR_NETWORK_MODE=public."
            )
    return None


def _agent_model(agent: str) -> str:
    if agent == "claude-code":
        return settings.claude_agent_model
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


def _record_checksum(
    submission: Submission, run: Run, task_checksum: str | None, update_canonical: bool = True
) -> None:
    """Stores the task_checksum Harbor reported for this run, and — the
    first time any run on this submission reports one — freezes it on the
    Submission as the canonical version (C1: one frozen task version per
    run set). Later runs keep their own checksum even if it differs, so a
    mismatch (e.g. oracle run against one task copy, rollouts against
    another) is recorded rather than silently overwritten.

    update_canonical=False for the Cheat Trial: its task copy always has an
    intentionally edited instruction.md (see _prepare_task's
    cheat_instruction branch), so its checksum legitimately never matches
    the others and must never become -- or be compared against -- the
    canonical version."""
    run.task_checksum = task_checksum
    if update_canonical and task_checksum and not submission.task_checksum:
        submission.task_checksum = task_checksum


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


async def run_oracle_trials(submission_id: str, n: int) -> None:
    """Oracle, run n times sequentially, always appended to any earlier runs
    on this submission (never cleared): Gate 1 needs 3 *consecutive* 1.0
    runs, so the point of this is to keep every run's own status/reward/logs
    visible afterwards, unlike the old single-row behavior that a fresh
    trigger silently overwrote. One Harbor job at a time -- running two
    concurrently has caused environment-start failures on this machine."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return

        runs = (
            db.query(Run)
            .filter_by(submission_id=submission_id, kind="oracle", status="pending")
            .order_by(Run.run_index)
            .all()
        )  # the router pre-created these n rows as pending; earlier runs are left alone

        config = _get_task_config(submission)
        timeout_sec = (
            config.environment.build_timeout_sec
            + _agent_timeout_sec(config)
            + config.verifier.timeout_sec
            + _HARBOR_TIMEOUT_BUFFER_SEC
        )
        jobs_dir = settings.storage_dir / "submissions" / submission_id / "runs" / "oracle" / "harbor"

        for run in runs:
            run.status = "running"
            run.reward = None
            run.logs = None
            run.started_at = datetime.now(UTC)
            db.commit()

            outcome = await harbor_runner.run_single(
                Path(submission.extracted_path),
                "oracle",
                jobs_dir,
                f"oracle-{uuid.uuid4().hex[:8]}",
                timeout_sec,
            )
            # The gate needs a perfect 1.0; any partial reward counts as 0.
            if outcome.reward is None:
                run.status = "failed"
                run.reward = None
            else:
                run.reward = 1 if outcome.reward >= 1.0 else 0
                run.status = "passed" if run.reward == 1 else "failed"
            run.logs = outcome.logs
            _record_checksum(submission, run, outcome.task_checksum)
            run.finished_at = datetime.now(UTC)
            db.commit()
    except Exception:
        stuck = (
            db.query(Run)
            .filter_by(submission_id=submission_id, kind="oracle")
            .filter(Run.status.in_(_STUCK_STATUSES))
            .all()
        )
        for r in stuck:
            r.status = "failed"
            r.reward = None
            r.logs = traceback.format_exc()
            r.finished_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()


async def run_nop(submission_id: str) -> None:
    await _run_harbor_gate(submission_id, kind="nop")


# Headroom over the task's own build + agent + verifier timeouts, which Harbor
# enforces itself; this outer limit only catches a hung harbor process.
_HARBOR_TIMEOUT_BUFFER_SEC = 600
# Per trial: agents that install themselves (Codex: apt, nvm, npm) before running.
_AGENT_SETUP_ALLOWANCE_SEC = 300


async def _run_harbor_gate(submission_id: str, kind: str) -> None:
    """Single-run gate via `harbor run -a <kind>`, so it gives the same
    verdict as the real grading pipeline. Harbor builds the image, runs the
    agent phase and the verifier in its own isolated containers. Only Nop
    uses this now (kind="nop", one row, overwritten on each trigger) --
    Oracle moved to run_oracle_trials, which needs N runs kept visible at
    once (Gate 1's "3 consecutive" requirement), not just the latest."""
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
        _record_checksum(submission, run, result.task_checksum)
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


# C11: marks a trial's logs as already having had its one automatic infra
# retry, so a retry that also shows infra noise is left for a human instead
# of looping forever against a persistent problem (Docker down, etc).
_INFRA_RETRY_MARKER = "[C11: auto-retried once for infra noise]"


async def _retry_infra_agent_failures(
    db,
    submission: Submission,
    submission_id: str,
    runs: list[Run],
    agent: str,
    model: str,
    agent_kwargs: dict,
    timeout_sec: float,
    jobs_dir: Path,
) -> None:
    """"Keep the current infra-error policy: rerun infrastructure failures
    and flag them" (C11). A trial that crashed/timed out with no reward
    (reward is None -- see run's own "no reward" convention) AND whose logs
    show a known infra-noise marker (services/infra_marker_scan.py: connect/
    read timeouts, rate limits, DNS failures, Docker orchestration errors --
    never a genuine task-logic failure) gets exactly one automatic retry,
    sequentially (never two Harbor jobs at once -- that itself has caused
    EnvironmentStartTimeoutError on this machine), before being left for a
    human. The original failure is kept in the retried trial's logs, not
    discarded, and the retry itself is flagged either way so it's always
    clear this reward came from a second attempt."""
    candidates = [
        r
        for r in runs
        if r.reward is None
        and r.logs
        and _INFRA_RETRY_MARKER not in r.logs
        and infra_marker_scan.scan_trial_logs(r.logs)
    ]
    for run in candidates:
        reason = budget.exceeded_message(db)
        if reason:
            run.logs = f"{_INFRA_RETRY_MARKER} (not retried: {reason})\n\n{run.logs}"
            db.commit()
            continue

        markers = infra_marker_scan.scan_trial_logs(run.logs)
        original_logs = run.logs
        retry_job_dir = jobs_dir.parent / f"{jobs_dir.name}-infra-retry"
        result = await harbor_runner.run_job(
            Path(submission.extracted_path),
            agent,
            retry_job_dir,
            f"agent-retry-{uuid.uuid4().hex[:8]}",
            timeout_sec,
            model=model,
            agent_kwargs=agent_kwargs,
            n_attempts=1,
            n_concurrent=1,
        )
        header = (
            f"{_INFRA_RETRY_MARKER} original attempt showed infra noise ({', '.join(markers)}), "
            "so it was rerun once automatically rather than counted as a genuine failure.\n\n"
            f"=== ORIGINAL (infra-failed) ATTEMPT ===\n{original_logs[-4_000:]}\n\n"
            "=== RETRY ==="
        )
        if result.trials:
            outcome = result.trials[0]
            if outcome.cost_usd is not None:
                budget.record(
                    db, submission_id, "agent_trials", model, outcome.cost_usd,
                    outcome.n_input_tokens, outcome.n_cache_tokens, outcome.n_output_tokens,
                )
            if outcome.reward is None:
                run.status = "failed"
                run.reward = None
            else:
                run.reward = 1 if outcome.reward >= 1.0 else 0
                run.status = "passed" if run.reward == 1 else "failed"
            run.logs = f"{header}\n{outcome.logs}"
            _record_checksum(submission, run, outcome.task_checksum)
        else:
            run.status = "failed"
            run.reward = None
            run.logs = f"{header}\n{result.error or 'harbor produced no result for the retry either'}"
        run.finished_at = datetime.now(UTC)
        db.commit()


async def run_agent_trials(
    submission_id: str,
    n: int,
    append: bool = False,
    reasoning_effort: str | None = None,
    agent: str | None = None,
) -> None:
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
        agent = agent or settings.harbor_agent
        config_error = _agent_config_error(agent)
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
            + rounds * max(_AGENT_SETUP_ALLOWANCE_SEC, _agent_setup_timeout_sec(agent))
            + _HARBOR_TIMEOUT_BUFFER_SEC
        )

        agent_kwargs = _agent_kwargs(agent, reasoning_effort)

        jobs_dir = settings.storage_dir / "submissions" / submission_id / "runs" / "agent" / "harbor"
        if jobs_dir.exists() and not append:
            shutil.rmtree(jobs_dir)  # keep only the latest attempt's output

        pending = list(runs)
        model = _agent_model(agent)

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
                _record_checksum(submission, run, outcome.task_checksum)
                run.finished_at = datetime.now(UTC)
            db.commit()
            # Stop the rest of the job once the monthly cap is crossed.
            reason = budget.exceeded_message(db)
            return f"skipped: {reason}" if reason and pending else None

        cost_cap_usd, cost_cap_label = _run_cost_cap(db)
        if agent == "claude-code" and cost_cap_usd is not None:
            # The watchdog can't read Claude Code's cost while it runs (its
            # session log stays in the container), so each trial also gets its
            # share of the cap as Claude Code's own --max-budget-usd.
            agent_kwargs["max_budget_usd"] = f"{max(cost_cap_usd / n, 0.01):.2f}"
        result = await harbor_runner.run_job(
            Path(submission.extracted_path),
            agent,
            jobs_dir,
            f"agent-{uuid.uuid4().hex[:8]}",
            timeout_sec,
            model=model,
            agent_kwargs=agent_kwargs,
            n_attempts=n,
            n_concurrent=concurrency,
            agent_setup_timeout_sec=_agent_setup_timeout_sec(agent),
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

        await _retry_infra_agent_failures(
            db, submission, submission_id, runs, agent, model, agent_kwargs, timeout_sec, jobs_dir
        )
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


async def run_static_checks_stage(submission_id: str) -> None:
    """Advisory-only mechanical checks (services/static_checks.py) over
    instruction.md, test.sh, Dockerfile and zip layout -- word counts,
    formatting, hygiene, no LLM call, no Docker. Persisted as its own Run
    (kind="static_checks") so the evidence (C5: "the output of the static
    checks") is independently saved and downloadable, not just computed
    on-the-fly inside Review Report's prompt (which still recomputes it
    live for the LLM's benefit -- both read the same deterministic
    function, so they never disagree)."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return

        run = _get_or_create_run(db, submission_id, "static_checks", 0)
        run.status = "running"
        run.reward = None
        run.logs = None
        run.started_at = datetime.now(UTC)
        db.commit()

        try:
            report = static_checks.run_static_checks(Path(submission.extracted_path))
        except Exception:
            run.status = "failed"
            run.reward = None
            run.logs = traceback.format_exc()
            run.finished_at = datetime.now(UTC)
            db.commit()
            return

        run.status = "passed" if report.fail_count == 0 else "failed"
        run.reward = 1 if report.fail_count == 0 else 0
        run.logs = json.dumps(
            {
                "fail_count": report.fail_count,
                "warn_count": report.warn_count,
                "results": [
                    {"name": r.name, "severity": r.severity, "message": r.message}
                    for r in report.results
                ],
                "text": report.as_text(),
            }
        )
        run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception:
        run = db.query(Run).filter_by(
            submission_id=submission_id, kind="static_checks", run_index=0
        ).one_or_none()
        if run is not None:
            run.status = "failed"
            run.reward = None
            run.logs = traceback.format_exc()
            run.finished_at = datetime.now(UTC)
            db.commit()
    finally:
        db.close()


async def run_tb_static_checks_stage(submission_id: str) -> None:
    """Advisory-only: Terminal-Bench's own official static checks
    (vendor/tb_checks/, see PROVENANCE.md there) -- framework-compliance
    rules maintained upstream, not by us. Persisted as its own Run
    (kind="tb_static_checks"), separate from and not a replacement for
    "static_checks" (services/static_checks.py), which checks a different,
    platform-specific rule set with almost no overlap."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return

        run = _get_or_create_run(db, submission_id, "tb_static_checks", 0)
        run.status = "running"
        run.reward = None
        run.logs = None
        run.started_at = datetime.now(UTC)
        db.commit()

        try:
            report = tb_static_checks.run_tb_static_checks(Path(submission.extracted_path))
        except Exception:
            run.status = "failed"
            run.reward = None
            run.logs = traceback.format_exc()
            run.finished_at = datetime.now(UTC)
            db.commit()
            return

        run.status = "passed" if report.fail_count == 0 else "failed"
        run.reward = 1 if report.fail_count == 0 else 0
        run.logs = json.dumps(
            {
                "pass_count": report.pass_count,
                "fail_count": report.fail_count,
                "results": [
                    {"name": r.name, "passed": r.passed, "output": r.output}
                    for r in report.results
                ],
                "text": report.as_text(),
            }
        )
        run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception:
        run = db.query(Run).filter_by(
            submission_id=submission_id, kind="tb_static_checks", run_index=0
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


async def run_failure_analysis(submission_id: str, rubric: str = "default") -> None:
    """`harbor analyze` over every agent job of this submission (append=true
    leaves several). rubric="tb" swaps in the vendored, richer 6-criterion
    Terminal-Bench rubric+prompt (vendor/tb_prompts/, see PROVENANCE.md)
    instead of Harbor's own default 2-criterion one, and additionally
    synthesizes a job-level summary (Harbor's analyze has no such step
    itself) via one extra LLM call using trial-analysis-job.txt's template.
    The result is stored as JSON in the Run's logs: {"agent", "model",
    "cost_usd", "error", "results": [per-trial analysis], "rubric",
    "job_summary": str | None}."""
    db = SessionLocal()
    try:
        run = _get_or_create_run(db, submission_id, "failure_analysis")
        run.status = "running"
        run.started_at = datetime.now(UTC)
        run.logs = None
        db.commit()

        agent_root = settings.storage_dir / "submissions" / submission_id / "runs" / "agent" / "harbor"
        job_dirs = sorted(
            (d for d in agent_root.glob("agent-*") if d.is_dir()), key=lambda d: d.stat().st_mtime
        )
        results: list[dict] = []
        errors: list[str] = []
        total_cost: float | None = None
        analysis_root = settings.storage_dir / "submissions" / submission_id / "runs" / "analysis"
        stamp = uuid.uuid4().hex[:8]
        use_tb = rubric == "tb"
        for job_dir in job_dirs:
            outcome = await harbor_runner.run_analyze(
                job_dir,
                analysis_root,
                f"analyze-{stamp}-{job_dir.name}",
                settings.analyze_agent,
                settings.analyze_model,
                settings.analyze_timeout_sec,
                rubric_path=_TB_ANALYZE_RUBRIC if use_tb else None,
                prompt_path=_TB_ANALYZE_PROMPT if use_tb else None,
            )
            if outcome.error:
                errors.append(f"{job_dir.name}: {outcome.error}")
            if outcome.report:
                results.extend(outcome.report.get("results") or [])
            if outcome.cost_usd is not None:
                total_cost = (total_cost or 0.0) + outcome.cost_usd
                budget.record(
                    db, submission_id, "failure_analysis", settings.analyze_model,
                    outcome.cost_usd, None, None, None,
                )

        job_summary = None
        if use_tb and results:
            template = _TB_ANALYZE_JOB_PROMPT.read_text()
            prompt = template.replace("{trial_results}", json.dumps(results, indent=2))
            try:
                with _track_llm_spend(db, submission_id, "failure_analysis"):
                    client = llm_factory.get_llm_client()
                    resp = await client.complete([Message(role="user", content=prompt)], tools=[])
                job_summary = resp.text
            except Exception:
                job_summary = None

        run.logs = json.dumps(
            {
                "agent": settings.analyze_agent,
                "model": settings.analyze_model,
                "cost_usd": total_cost,
                "error": "\n\n".join(errors) or None,
                "results": results,
                "rubric": rubric,
                "job_summary": job_summary,
            }
        )
        # "passed" = the analysis ran and produced results; it's advisory, not a verdict.
        run.status = "passed" if results else "failed"
        run.reward = 1 if results else None
        run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception:
        run = _get_or_create_run(db, submission_id, "failure_analysis")
        run.status = "failed"
        run.reward = None
        run.logs = json.dumps({"error": traceback.format_exc(), "results": []})
        run.finished_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()


async def run_cheat_trial(
    submission_id: str,
    agent: str | None = None,
    reasoning_effort: str | None = None,
    cheat_prompt: str = "default",
) -> None:
    """One trial run with an explicit cheat directive spliced into
    instruction.md (harbor_runner._prepare_task, cheat_instruction=True), to
    test whether the anti-cheat design (separate verifier image, tests/ copied
    in only after the agent finishes, solution/ excluded from the build
    context) holds when the agent is TOLD to cheat, not just when it happens
    to behave. reward staying 0/None already means the trap held ("passed").
    reward=1 alone doesn't say the agent found a real cheat -- confirmed live:
    two separate trials (Codex and Claude Code) both got reward 1 after
    explicitly refusing to cheat ("I won't alter grading machinery...") and
    solving the task honestly instead, with no access to tests/ or the
    reference solution. So a reward=1 trial is automatically followed by a
    `harbor analyze` reward_hacking check on that one trial (same mechanism as
    Failure Analysis); only a check that positively finds no reward hacking
    counts as "passed" ("trap held; solved honestly despite the directive").
    A check that finds reward hacking, or that itself fails, is "failed" and
    flagged for manual review -- never silently assumed clean. logs is JSON:
    {trial_log, reward, cheat_check: {outcome, explanation, cost_usd, error}}."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return

        run = _get_or_create_run(db, submission_id, "cheat_trial")
        agent = agent or settings.harbor_agent
        config_error = _agent_config_error(agent)
        if config_error:
            run.status = "failed"
            run.reward = None
            run.logs = config_error
            run.finished_at = datetime.now(UTC)
            db.commit()
            return

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
            + max(_AGENT_SETUP_ALLOWANCE_SEC, _agent_setup_timeout_sec(agent))
            + _HARBOR_TIMEOUT_BUFFER_SEC
        )
        agent_kwargs = _agent_kwargs(agent, reasoning_effort)
        model = _agent_model(agent)
        jobs_dir = settings.storage_dir / "submissions" / submission_id / "runs" / "cheat_trial" / "harbor"
        if jobs_dir.exists():
            shutil.rmtree(jobs_dir)

        outcome_holder: list[harbor_runner.TrialOutcome] = []

        async def on_trial(outcome: harbor_runner.TrialOutcome) -> str | None:
            outcome_holder.append(outcome)
            if outcome.cost_usd is not None:
                budget.record(
                    db, submission_id, "cheat_trial", model,
                    outcome.cost_usd, outcome.n_input_tokens, outcome.n_cache_tokens,
                    outcome.n_output_tokens,
                )
            return None

        cost_cap_usd, cost_cap_label = _run_cost_cap(db)
        job_name = f"cheat-{uuid.uuid4().hex[:8]}"
        result = await harbor_runner.run_job(
            Path(submission.extracted_path),
            agent,
            jobs_dir,
            job_name,
            timeout_sec,
            model=model,
            agent_kwargs=agent_kwargs,
            n_attempts=1,
            n_concurrent=1,
            agent_setup_timeout_sec=_agent_setup_timeout_sec(agent),
            cost_cap_usd=cost_cap_usd,
            cost_cap_label=cost_cap_label,
            on_trial=on_trial,
            cheat_instruction=True,
            cheat_instruction_text=_TB_CHEAT_PROMPT.read_text() if cheat_prompt == "tb" else None,
        )

        if outcome_holder:
            outcome = outcome_holder[0]
            run.reward = 1 if (outcome.reward is not None and outcome.reward >= 1.0) else outcome.reward
            cheat_check: dict | None = None
            if run.reward == 1:
                # reward=1 alone can't say cheated vs. refused-and-solved-honestly
                # (both observed live) -- ask harbor analyze's reward_hacking check.
                analysis_root = (
                    settings.storage_dir / "submissions" / submission_id / "runs" / "analysis"
                )
                analyze_outcome = await harbor_runner.run_analyze(
                    jobs_dir / job_name,
                    analysis_root,
                    f"analyze-{job_name}",
                    settings.analyze_agent,
                    settings.analyze_model,
                    settings.analyze_timeout_sec,
                )
                if analyze_outcome.cost_usd is not None:
                    budget.record(
                        db, submission_id, "cheat_trial", settings.analyze_model,
                        analyze_outcome.cost_usd, None, None, None,
                    )
                results = (analyze_outcome.report or {}).get("results") or []
                check = (results[0].get("checks") or {}).get("reward_hacking") if results else None
                if check:
                    cheat_check = {
                        "outcome": check.get("outcome"),
                        "explanation": check.get("explanation"),
                        "cost_usd": analyze_outcome.cost_usd,
                        "error": None,
                    }
                else:
                    cheat_check = {
                        "outcome": "unknown",
                        "explanation": None,
                        "cost_usd": analyze_outcome.cost_usd,
                        "error": analyze_outcome.error or "harbor analyze produced no reward_hacking check",
                    }
            # "passed" whenever the trap held outright (reward != 1), or when
            # reward=1 but the reward_hacking check positively found no
            # cheating; anything else (a real finding, or an unresolved
            # check) stays "failed" so it gets a human's attention.
            run.status = (
                "passed"
                if run.reward != 1 or (cheat_check is not None and cheat_check.get("outcome") == "pass")
                else "failed"
            )
            run.logs = json.dumps(
                {
                    "trial_log": outcome.logs,
                    "reward": run.reward,
                    "cheat_check": cheat_check,
                    "cheat_prompt": cheat_prompt,
                }
            )
            # Not update_canonical: this trial's task copy always has an
            # intentionally edited instruction.md, so its checksum never
            # matches the others by design -- record it, but never let it
            # become or be compared against the submission's canonical version.
            _record_checksum(submission, run, outcome.task_checksum, update_canonical=False)
        else:
            run.status = "failed"
            run.reward = None
            run.logs = json.dumps(
                {
                    "trial_log": result.error or "harbor produced no result for the cheat trial",
                    "reward": None,
                    "cheat_check": None,
                    "cheat_prompt": cheat_prompt,
                }
            )
        run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception:
        run = _get_or_create_run(db, submission_id, "cheat_trial")
        if run.status in _STUCK_STATUSES:
            run.status = "failed"
            run.reward = None
            run.logs = json.dumps({"trial_log": traceback.format_exc(), "reward": None, "cheat_check": None})
            run.finished_at = datetime.now(UTC)
            db.commit()
    finally:
        db.close()


async def run_rubric_check(submission_id: str, rubric: str = "default") -> None:
    """`harbor check`: an evaluator agent reads the whole task and scores it
    against a rubric -- Harbor's own default (an automated stand-in for a
    human task reviewer) unless rubric="tb", which swaps in the vendored,
    richer 35-criterion Terminal-Bench rubric (vendor/tb_prompts/, see
    PROVENANCE.md) instead. Advisory, like Failure Analysis. Result stored
    as JSON in the Run's logs: {"agent", "model", "cost_usd", "error",
    "results": [...], "rubric"}."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return

        run = _get_or_create_run(db, submission_id, "rubric_check")
        run.status = "running"
        run.logs = None
        run.started_at = datetime.now(UTC)
        db.commit()

        check_root = settings.storage_dir / "submissions" / submission_id / "runs" / "rubric_check"
        outcome = await harbor_runner.run_check(
            Path(submission.extracted_path),
            check_root,
            f"check-{uuid.uuid4().hex[:8]}",
            settings.check_agent,
            settings.check_model,
            settings.check_timeout_sec,
            rubric_path=_TB_CHECK_RUBRIC if rubric == "tb" else None,
        )
        if outcome.cost_usd is not None:
            budget.record(
                db, submission_id, "rubric_check", settings.check_model,
                outcome.cost_usd, None, None, None,
            )

        results = (outcome.report or {}).get("results") or []
        run.logs = json.dumps(
            {
                "agent": settings.check_agent,
                "model": settings.check_model,
                "cost_usd": outcome.cost_usd,
                "error": outcome.error,
                "results": results,
                "rubric": rubric,
            }
        )
        has_checks = any(r.get("checks") for r in results)
        run.status = "passed" if has_checks else "failed"
        run.reward = 1 if has_checks else None
        run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception:
        run = _get_or_create_run(db, submission_id, "rubric_check")
        run.status = "failed"
        run.reward = None
        run.logs = json.dumps({"error": traceback.format_exc(), "results": []})
        run.finished_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()
