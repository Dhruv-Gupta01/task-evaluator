"""Runs tasks through the Harbor CLI (`harbor run`) — the same harness the
real grading pipeline uses — for the Oracle/Nop gates and for agent trials.
Harbor builds the environment image itself, runs the agent in the task
container, then copies tests/ in and runs the verifier in that same
container, so every stage matches the real pipeline's verdicts."""

import asyncio
import json
import os
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings

settings = get_settings()

MAX_LOG_CHARS = 200_000
# How long to let Harbor tear down its containers after SIGINT before killing it.
_SHUTDOWN_GRACE_SEC = 60
# How often to look for newly finished trials while a job is running.
_POLL_INTERVAL_SEC = 5


@dataclass
class TrialOutcome:
    reward: float | None
    logs: str
    n_input_tokens: int | None = None
    n_cache_tokens: int | None = None
    n_output_tokens: int | None = None
    cost_usd: float | None = None


@dataclass
class HarborJobResult:
    trials: list[TrialOutcome]
    # Set when the job as a whole failed (CLI missing, timeout, crash) —
    # trials that never produced a result.json are explained by this.
    error: str | None = None


def _truncate(text: str) -> str:
    if len(text) > MAX_LOG_CHARS:
        return text[-MAX_LOG_CHARS:]
    return text


def _read_text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _usage_line(outcome: TrialOutcome) -> str:
    if outcome.n_input_tokens is None and outcome.cost_usd is None:
        return "(no LLM usage recorded)"
    cost = f"${outcome.cost_usd:.4f}" if outcome.cost_usd is not None else "unknown"
    return (
        f"input tokens: {outcome.n_input_tokens} (cached: {outcome.n_cache_tokens}), "
        f"output tokens: {outcome.n_output_tokens}, cost: {cost}"
    )


def _parse_trial(result_path: Path) -> TrialOutcome:
    trial_dir = result_path.parent
    result = json.loads(result_path.read_text())

    reward = None
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    if rewards.get("reward") is not None:
        reward = float(rewards["reward"])

    agent_result = result.get("agent_result") or {}
    outcome = TrialOutcome(
        reward=reward,
        logs="",
        n_input_tokens=agent_result.get("n_input_tokens"),
        n_cache_tokens=agent_result.get("n_cache_tokens"),
        n_output_tokens=agent_result.get("n_output_tokens"),
        cost_usd=agent_result.get("cost_usd"),
    )

    # Most useful section last: review_report reads the tail of trial logs.
    sections = [
        f"(full Harbor trial output: {trial_dir})",
        f"=== REWARD ===\n{reward if reward is not None else '(none)'}",
        f"=== LLM USAGE ===\n{_usage_line(outcome)}",
    ]
    exception = result.get("exception_info")
    if exception:
        sections.append(
            f"=== EXCEPTION: {exception.get('exception_type')} ===\n"
            f"{exception.get('exception_message')}\n\n"
            f"{exception.get('exception_traceback', '')}"
        )
    trial_log = _read_text(trial_dir / "trial.log")
    if trial_log:
        sections.append(f"=== TRIAL LOG ===\n{trial_log}")
    test_stdout = _read_text(trial_dir / "verifier" / "test-stdout.txt")
    if test_stdout:
        sections.append(f"=== VERIFIER OUTPUT ===\n{test_stdout}")

    outcome.logs = _truncate("\n\n".join(sections))
    return outcome


async def _stop(proc: asyncio.subprocess.Process) -> None:
    """SIGINT first so Harbor can remove its containers, then kill."""
    if proc.returncode is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
        await asyncio.wait_for(proc.wait(), timeout=_SHUTDOWN_GRACE_SEC)
    except (TimeoutError, ProcessLookupError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


def _litellm_key_env() -> dict[str, str]:
    """Provider keys for agents that call a model from the host (Terminus-2
    runs on this machine and drives the container, so keys never enter it)."""
    keys = {
        "ANTHROPIC_API_KEY": settings.anthropic_api_key,
        "OPENAI_API_KEY": settings.openai_api_key,
        "GEMINI_API_KEY": settings.gemini_api_key,
        "GROQ_API_KEY": settings.groq_api_key,
        "FIREWORKS_API_KEY": settings.fireworks_api_key,
        "FIREWORKS_AI_API_KEY": settings.fireworks_api_key,
    }
    return {k: v for k, v in keys.items() if v}


async def run_job(
    task_root: Path,
    agent: str,
    jobs_dir: Path,
    job_name: str,
    timeout_sec: float,
    *,
    model: str | None = None,
    agent_kwargs: dict[str, str] | None = None,
    n_attempts: int = 1,
    n_concurrent: int = 1,
    on_trial: Callable[[TrialOutcome], Awaitable[str | None]] | None = None,
) -> HarborJobResult:
    """Run `harbor run` with `n_attempts` trials of `agent` against the task
    at `task_root`. `on_trial` is awaited as each trial finishes, in
    completion order; if it returns a reason string, the job is stopped and
    that reason becomes the job error. Never raises for task-level failures — a failed build,
    crashed trial, or timeout comes back as reward=None or a job error."""
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_dir = jobs_dir / job_name
    cli_output_path = jobs_dir / f"{job_name}.out"
    cmd = [
        settings.harbor_bin,
        "run",
        "--path", str(task_root),
        "--agent", agent,
        "--jobs-dir", str(jobs_dir),
        "--job-name", job_name,
        "--n-attempts", str(n_attempts),
        "--n-concurrent", str(n_concurrent),
        "--max-retries", "0",
        "--quiet",
        "--yes",
    ]
    if model:
        cmd += ["--model", model]
    for key, value in (agent_kwargs or {}).items():
        cmd += ["--agent-kwarg", f"{key}={value}"]

    # Harbor doesn't pin a platform; Docker honours this default for builds
    # and runs, keeping Harbor on the same architecture as our own stages.
    env = {
        **os.environ,
        **_litellm_key_env(),
        "DOCKER_DEFAULT_PLATFORM": settings.docker_platform,
    }

    trials: list[TrialOutcome] = []
    seen: set[Path] = set()
    stop_reason: str | None = None

    async def collect_finished() -> None:
        nonlocal stop_reason
        # result.json is written once, when a trial finishes.
        for result_path in sorted(job_dir.glob("*/result.json")):
            if result_path in seen:
                continue
            try:
                outcome = _parse_trial(result_path)
            except (json.JSONDecodeError, OSError):
                continue  # caught mid-write; pick it up on the next poll
            seen.add(result_path)
            if stop_reason is not None:
                # Finished only because we stopped the job; say why first.
                outcome.logs = f"{stop_reason}\n\n{outcome.logs}"
            trials.append(outcome)
            if on_trial is not None:
                reason = await on_trial(outcome)
                if reason and stop_reason is None:
                    stop_reason = reason

    with open(cli_output_path, "wb") as cli_output:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=jobs_dir,
                env=env,
                stdout=cli_output,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            return HarborJobResult(
                trials=[],
                error=(
                    f"harbor CLI not found ({settings.harbor_bin!r}). Install it with "
                    "`uv tool install harbor`, or set HARBOR_BIN in backend/.env."
                ),
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_sec
        try:
            while True:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=_POLL_INTERVAL_SEC)
                    break
                except TimeoutError:
                    await collect_finished()
                    if stop_reason is not None:
                        await _stop(proc)
                        await collect_finished()
                        return HarborJobResult(trials=trials, error=stop_reason)
                    if loop.time() > deadline:
                        await _stop(proc)
                        await collect_finished()
                        return HarborJobResult(
                            trials=trials,
                            error=f"harbor run timed out after {timeout_sec:.0f}s",
                        )
        except asyncio.CancelledError:
            await _stop(proc)
            raise

    await collect_finished()
    error = stop_reason
    if error is None and len(trials) < n_attempts:
        error = (
            f"harbor exited with code {proc.returncode} after {len(trials)} of "
            f"{n_attempts} trials finished\n\n=== HARBOR OUTPUT ===\n"
            f"{_truncate(_read_text(cli_output_path))}"
        )
    return HarborJobResult(trials=trials, error=error)


async def run_single(
    task_root: Path, agent: str, jobs_dir: Path, job_name: str, timeout_sec: float
) -> TrialOutcome:
    """One trial of a model-free agent (oracle/nop)."""
    result = await run_job(task_root, agent, jobs_dir, job_name, timeout_sec)
    if result.trials:
        return result.trials[0]
    return TrialOutcome(reward=None, logs=result.error or "harbor produced no trial result")
