"""Runs tasks through the Harbor CLI (`harbor run`) — the same harness the
real grading pipeline uses — for the Oracle/Nop gates and for agent trials.
Harbor builds the environment image itself, runs the agent in the task
container, then copies tests/ in and runs the verifier in that same
container, so every stage matches the real pipeline's verdicts."""

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.services.llm import pricing
from app.services.llm.base import Usage

settings = get_settings()

MAX_LOG_CHARS = 200_000
# How long to let Harbor tear down its containers after SIGINT before killing it.
_SHUTDOWN_GRACE_SEC = 60
# How often to look for newly finished trials while a job is running.
_POLL_INTERVAL_SEC = 5
# Tail of a failed trial's agent transcript, where the agent left one.
MAX_AGENT_OUTPUT_CHARS = 4_000
# Harbor's built-in agent install limit, which --agent-setup-timeout-multiplier scales.
_HARBOR_DEFAULT_SETUP_TIMEOUT_SEC = 360
# How often the cost watchdog re-reads in-flight trials' running cost.
_COST_CHECK_INTERVAL_SEC = 10


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


_NETWORK_KEYS = ("allow_internet", "network_mode", "allowed_hosts")


def _rewrite_toml(text: str, edits: dict[str, tuple[tuple[str, ...], str]]) -> str:
    """Line-based task.toml edits (the stdlib can read TOML but not write it).
    `edits` maps a section header such as "[agent]" to (keys to drop from that
    section, a line to insert right after its header). A section that is
    missing is appended at the end."""
    out: list[str] = []
    section = None
    inserted: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped
        elif section in edits and any(
            stripped.startswith(k) and stripped[len(k) :].lstrip().startswith("=")
            for k in edits[section][0]
        ):
            continue
        out.append(line)
        if stripped == section and section in edits and section not in inserted:
            out.append(edits[section][1])
            inserted.add(section)
    for header, (_, new_line) in edits.items():
        if header not in inserted:
            out += ["", header, new_line]
    return "\n".join(out) + "\n"


_CODEX_BAKE_SCRIPT_NAME = ".platform-codex-bake.sh"
_NODE_VERSION = "22.11.0"
_CODEX_BAKE_SCRIPT = f"""#!/bin/sh
# Added by the platform: installs Node and the Codex CLI while the task image
# builds, so trials don't download them. Usage: sh script <codex version|latest>
set -eu
if command -v codex >/dev/null 2>&1; then exit 0; fi
if command -v apk >/dev/null 2>&1; then
  apk add --no-cache nodejs npm curl bash ripgrep
elif command -v apt-get >/dev/null 2>&1; then
  apt-get update
  apt-get install -y --no-install-recommends curl ca-certificates xz-utils ripgrep
  rm -rf /var/lib/apt/lists/*
  case "$(uname -m)" in
    x86_64) A=x64 ;;
    aarch64|arm64) A=arm64 ;;
    *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;;
  esac
  curl -fsSL "https://nodejs.org/dist/v{_NODE_VERSION}/node-v{_NODE_VERSION}-linux-$A.tar.xz" \\
    | tar -xJ -C /usr/local --strip-components=1 --no-same-owner
else
  echo "no supported package manager" >&2
  exit 1
fi
npm install -g "@openai/codex@$1"
codex --version
"""


def _bake_codex_into_environment(env_dir: Path, version: str) -> bool:
    """Adds a Codex install layer to the environment/ of a task *copy*. The
    layer is best-effort: if it fails the build still succeeds and Harbor
    falls back to installing Codex at trial time. Returns False when the
    environment can't take it (no Dockerfile, or a compose-based one)."""
    dockerfile = env_dir / "Dockerfile"
    if not dockerfile.is_file() or any(
        (env_dir / name).exists()
        for name in ("docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml")
    ):
        return False
    text = dockerfile.read_text()
    users = re.findall(r"^\s*USER\s+(\S+)", text, flags=re.IGNORECASE | re.MULTILINE)
    lines = [
        "",
        "# --- added by the platform (CODEX_BAKE_INTO_IMAGE): Codex installed at build time ---",
        "USER root",
        f"COPY {_CODEX_BAKE_SCRIPT_NAME} /tmp/{_CODEX_BAKE_SCRIPT_NAME}",
        f"RUN sh /tmp/{_CODEX_BAKE_SCRIPT_NAME} {version} "
        '|| echo "NOTE: could not bake Codex into the image; Harbor will install it at trial time"',
    ]
    if users:
        lines.append(f"USER {users[-1]}")
    dockerfile.write_text(text.rstrip("\n") + "\n" + "\n".join(lines) + "\n")
    (env_dir / _CODEX_BAKE_SCRIPT_NAME).write_text(_CODEX_BAKE_SCRIPT)
    dockerignore = env_dir / ".dockerignore"
    if dockerignore.is_file():  # the script must not be excluded from the build context
        dockerignore.write_text(dockerignore.read_text().rstrip("\n") + f"\n!{_CODEX_BAKE_SCRIPT_NAME}\n")
    return True


def _prepare_task(
    task_root: Path, work_dir: Path, agent: str = ""
) -> tuple[Path, str | None]:
    """Returns (task path for Harbor, notes for the logs). When
    HARBOR_NETWORK_MODE or AGENT_TIMEOUT_SEC differ from the task file, or
    Codex is to be baked into the image, Harbor runs a temporary copy of the
    task with those changes; the extracted files are never edited."""
    mode = settings.harbor_network_mode
    timeout = settings.agent_timeout_sec
    toml_path = task_root / "task.toml"
    original = tomllib.loads(toml_path.read_text())
    env = original.get("environment", {})
    agent_section = original.get("agent", {})

    edits: dict[str, tuple[tuple[str, ...], str]] = {}
    notes: list[str] = []
    if mode and not (env.get("network_mode") == mode and "allow_internet" not in env):
        edits["[environment]"] = (_NETWORK_KEYS, f'network_mode = "{mode}"')
        was = {k: env[k] for k in _NETWORK_KEYS if k in env}
        notes.append(
            f"(network_mode forced to '{mode}' by the platform setting HARBOR_NETWORK_MODE; "
            f"the task file had {was or 'no network setting'})"
        )
    if timeout and agent_section.get("timeout_sec") != timeout:
        edits["[agent]"] = (("timeout_sec",), f"timeout_sec = {timeout}.0")
        notes.append(
            f"(agent timeout forced to {timeout}s by the platform setting AGENT_TIMEOUT_SEC; "
            f"the task file had {agent_section.get('timeout_sec', 'no timeout')})"
        )
    bake = (
        agent == "codex"
        and settings.codex_bake_into_image
        and (task_root / "environment" / "Dockerfile").is_file()
    )
    if not edits and not bake:
        return task_root, None

    if work_dir.exists():
        shutil.rmtree(work_dir)
    shutil.copytree(task_root, work_dir, symlinks=True)
    new_text = _rewrite_toml(toml_path.read_text(), edits) if edits else toml_path.read_text()
    (work_dir / "task.toml").write_text(new_text)
    new = tomllib.loads(new_text)
    new_env, new_agent = new.get("environment", {}), new.get("agent", {})
    if "[environment]" in edits and (
        new_env.get("network_mode") != mode
        or any(k in new_env for k in ("allow_internet", "allowed_hosts"))
    ):
        raise ValueError("could not rewrite task.toml network policy")
    if "[agent]" in edits and new_agent.get("timeout_sec") != timeout:
        raise ValueError("could not rewrite task.toml agent timeout")
    if bake:
        version = settings.codex_version or "latest"
        if _bake_codex_into_environment(work_dir / "environment", version):
            notes.append(
                f"(Codex {version} installed into the task image at build time by "
                "CODEX_BAKE_INTO_IMAGE, so the trial needs no download)"
            )
    return work_dir, "\n".join(notes)


def _usage_line(outcome: TrialOutcome) -> str:
    if outcome.n_input_tokens is None and outcome.cost_usd is None:
        return "(no LLM usage recorded)"
    cost = f"${outcome.cost_usd:.4f}" if outcome.cost_usd is not None else "unknown"
    if outcome.n_input_tokens is None:
        return f"cost: {cost} (estimated from the agent's live usage; it reported no final tokens)"
    return (
        f"input tokens: {outcome.n_input_tokens} (cached: {outcome.n_cache_tokens}), "
        f"output tokens: {outcome.n_output_tokens}, cost: {cost}"
    )


def _cost_from_tokens(
    model: str | None,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
) -> float | None:
    if not model:
        return None
    return pricing.cost_usd(
        model.split("/", 1)[-1],
        Usage(
            input_tokens=input_tokens,
            cached_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
            output_tokens=output_tokens,
        ),
    )


def _live_cost_from_files(trial_dir: Path, model: str | None) -> float | None:
    """Terminus rewrites agent/trajectory.json after every turn, with running
    totals, so a trial's cost so far is readable from the host."""
    try:
        metrics = json.loads((trial_dir / "agent" / "trajectory.json").read_text()).get(
            "final_metrics"
        ) or {}
    except (OSError, json.JSONDecodeError):
        return None
    if metrics.get("total_cost_usd") is not None:
        return float(metrics["total_cost_usd"])
    if metrics.get("total_prompt_tokens") is not None:
        return _cost_from_tokens(
            model,
            metrics["total_prompt_tokens"],
            metrics.get("total_cached_tokens") or 0,
            metrics.get("total_completion_tokens") or 0,
        )
    return None


# Codex keeps its session file inside the container until the trial ends, so a
# running trial's usage can only be read with docker exec.
_CODEX_SESSION_SCRIPT = (
    "f=$(ls -t /tmp/codex-home/sessions/*/*/*/rollout-*.jsonl 2>/dev/null | head -1); "
    "[ -n \"$f\" ] && grep '\"total_token_usage\"' \"$f\" | tail -1"
)


def _codex_cost_from_rollout_line(line: str, model: str | None) -> float | None:
    try:
        usage = json.loads(line)["payload"]["info"]["total_token_usage"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    return _cost_from_tokens(
        model,
        usage.get("input_tokens") or 0,
        usage.get("cached_input_tokens") or 0,
        usage.get("output_tokens") or 0,
        usage.get("cache_write_input_tokens") or 0,
    )


def _live_cost_from_container(trial_dir: Path, model: str | None) -> float | None:
    """Blocking (docker CLI calls); run it in a thread. Harbor names a
    trial's compose project after the lowercased trial directory."""
    project = trial_dir.name.lower()
    try:
        ids = subprocess.run(
            ["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={project}"],
            capture_output=True, text=True, timeout=20,
        ).stdout.split()
        for cid in ids:
            line = subprocess.run(
                ["docker", "exec", cid, "sh", "-c", _CODEX_SESSION_SCRIPT],
                capture_output=True, text=True, timeout=20,
            ).stdout.strip()
            if line:
                cost = _codex_cost_from_rollout_line(line, model)
                if cost is not None:
                    return cost
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _parse_trial(
    result_path: Path, model: str | None = None, live_costs: dict[Path, float] | None = None
) -> TrialOutcome:
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

    if outcome.cost_usd is None and outcome.n_input_tokens is not None and model:
        # Not every agent reports cost; price the tokens ourselves when we can.
        outcome.cost_usd = pricing.cost_usd(
            model.split("/", 1)[-1],
            Usage(
                input_tokens=outcome.n_input_tokens,
                cached_tokens=outcome.n_cache_tokens or 0,
                output_tokens=outcome.n_output_tokens or 0,
            ),
        )

    if outcome.cost_usd is None:
        # A trial cut short (timeout, cost cap) reports nothing itself; use
        # what its live usage showed.
        outcome.cost_usd = _live_cost_from_files(trial_dir, model)
    if outcome.cost_usd is None and live_costs:
        outcome.cost_usd = live_costs.get(trial_dir)

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
    if reward != 1.0:
        agent_output = _read_text(trial_dir / "agent" / "codex.txt")
        if agent_output:
            sections.append(
                f"=== AGENT OUTPUT (tail) ===\n{agent_output[-MAX_AGENT_OUTPUT_CHARS:]}"
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
    agent_setup_timeout_sec: int = 0,
    cost_cap_usd: float | None = None,
    cost_cap_label: str = "",
    on_trial: Callable[[TrialOutcome], Awaitable[str | None]] | None = None,
) -> HarborJobResult:
    """Run `harbor run` with `n_attempts` trials of `agent` against the task
    at `task_root`. If `cost_cap_usd` is set, the job is stopped once the cost
    of its finished plus in-flight trials reaches it. `on_trial` is awaited as each trial finishes, in
    completion order; if it returns a reason string, the job is stopped and
    that reason becomes the job error. Never raises for task-level failures — a failed build,
    crashed trial, or timeout comes back as reward=None or a job error."""
    jobs_dir.mkdir(parents=True, exist_ok=True)
    task_path, network_note = _prepare_task(
        task_root, jobs_dir.parent / f"{jobs_dir.name}-task", agent
    )
    notes = "\n".join(
        n
        for n in (
            f"(agent: {agent}, model: {model or 'n/a'}, options: {agent_kwargs or {}})",
            network_note,
        )
        if n
    )
    job_dir = jobs_dir / job_name
    cli_output_path = jobs_dir / f"{job_name}.out"
    cmd = [
        settings.harbor_bin,
        "run",
        "--path", str(task_path),
        "--agent", agent,
        "--jobs-dir", str(jobs_dir),
        "--job-name", job_name,
        "--n-attempts", str(n_attempts),
        "--n-concurrent", str(n_concurrent),
        "--max-retries", "0",
        "--quiet",
        "--yes",
    ]
    if agent_setup_timeout_sec > 0:
        # Harbor takes a multiplier of its own 360s default (trial.py).
        cmd += ["--agent-setup-timeout-multiplier", f"{agent_setup_timeout_sec / _HARBOR_DEFAULT_SETUP_TIMEOUT_SEC:.3f}"]
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
    live_costs: dict[Path, float] = {}
    last_cost_check = 0.0
    loop = asyncio.get_running_loop()

    async def collect_finished() -> None:
        nonlocal stop_reason
        # result.json is written once, when a trial finishes.
        for result_path in sorted(job_dir.glob("*/result.json")):
            if result_path in seen:
                continue
            try:
                outcome = _parse_trial(result_path, model, live_costs)
            except (json.JSONDecodeError, OSError):
                continue  # caught mid-write; pick it up on the next poll
            seen.add(result_path)
            if notes:
                outcome.logs = f"{notes}\n\n{outcome.logs}"
            if stop_reason is not None:
                # Finished only because we stopped the job; say why first.
                outcome.logs = f"{stop_reason}\n\n{outcome.logs}"
            trials.append(outcome)
            if on_trial is not None:
                reason = await on_trial(outcome)
                if reason and stop_reason is None:
                    stop_reason = reason

    async def check_cost_cap() -> None:
        nonlocal stop_reason, last_cost_check
        if not cost_cap_usd or stop_reason is not None or not job_dir.exists():
            return
        if loop.time() - last_cost_check < _COST_CHECK_INTERVAL_SEC:
            return
        last_cost_check = loop.time()
        finished = {p.parent for p in seen}
        total = sum(o.cost_usd or 0.0 for o in trials)
        for trial_dir in sorted(d for d in job_dir.iterdir() if d.is_dir()):
            if trial_dir in finished:
                continue
            cost = _live_cost_from_files(trial_dir, model)
            if cost is None and agent == "codex":
                cost = await asyncio.to_thread(_live_cost_from_container, trial_dir, model)
            if cost is not None:
                live_costs[trial_dir] = cost
                total += cost
        if total >= cost_cap_usd:
            stop_reason = (
                f"stopped: this run's cost reached about ${total:.2f}, over its "
                f"${cost_cap_usd:.2f} limit ({cost_cap_label})."
            )

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

        deadline = loop.time() + timeout_sec
        try:
            while True:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=_POLL_INTERVAL_SEC)
                    break
                except TimeoutError:
                    await collect_finished()
                    await check_cost_cap()
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
