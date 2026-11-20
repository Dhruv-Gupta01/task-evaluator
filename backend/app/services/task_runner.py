import asyncio
import json
import re
import shutil
import threading
import traceback
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_settings
from app.db import SessionLocal
from app.models import Run, Submission
from app.utils.ziputil import load_zip_entries
from app.services import (
    ai_detection,
    answer_exposure,
    edge_cases,
    budget,
    docker_orchestrator,
    harbor_runner,
    image_inventory,
    infra_marker_scan,
    leakage_scan,
    platform_checks,
    review_report,
    static_checks,
    sufficiency_judge,
    tb_static_checks,
    validation_service,
    variant_probe,
    verifier_report,
)
from app.services.llm import factory as llm_factory
from app.services.llm import pricing as llm_pricing
from app.services.llm import usage as llm_usage
from app.services.llm.base import Message
from app.services.task_config import TaskConfig
from app.services.tier0 import docker_runner as tier0_docker
from app.services.tier0 import probes as tier0_probes
from app.services.tier0 import record as tier0_record
from app.services.tier0 import runner as tier0_runner
from app.services.tier0 import taskspec as tier0_taskspec
from app.services.tier1 import fixer as tier1_fixer
from app.services.tier1 import hacker as tier1_hacker
from app.services.tier1 import judge as tier1_judge
from app.services.tier1 import loop as tier1_loop
from app.services.tier2 import PRESSURE_LINE
from app.services.tier2 import judge as tier2_judge
from app.services.tier2 import loop as tier2_loop

settings = get_settings()



def _apply_trial_outcome(run: Run, outcome: "harbor_runner.TrialOutcome") -> None:
    """Fill a Harbor-backed run's reward fields from a trial. `reward` stays
    the 0/1 the gates use (anything below 1.0 counts as a failure); the exact
    reward and the verifier's per-test report are kept beside it so a 0.73 or
    a 17-of-18 near miss is not lost."""
    run.raw_reward = outcome.reward
    run.verifier_json = json.dumps(outcome.report) if outcome.report else None
    if outcome.reward is None:
        run.status = "failed"
        run.reward = None
    else:
        run.reward = 1 if outcome.reward >= 1.0 else 0
        run.status = "passed" if run.reward == 1 else "failed"


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
# Models the UI offers per agent besides the one configured in .env. Not a
# whitelist: any model with the right provider prefix is accepted (see
# valid_model_override), so a newer model needs no code change.
MODEL_CHOICES: dict[str, tuple[str, ...]] = {
    "codex": ("openai/gpt-6-luna", "openai/gpt-6-sol", "openai/gpt-6-astra"),
    "claude-code": (
        "anthropic/claude-fable-5-1",
        "anthropic/claude-opus-5-5",
        "anthropic/claude-sonnet-5-5",
        "anthropic/claude-haiku-5-5",
    ),
}
_MODEL_PREFIX = {"codex": "openai/", "claude-code": "anthropic/"}
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,100}$")


def valid_model_override(agent: str, model: str) -> str | None:
    """A reason `model` can't be used with `agent`, or None. Codex only runs
    OpenAI models and Claude Code only Anthropic ones; Terminus-2 takes any
    LiteLLM string, so only its shape is checked."""
    if not _MODEL_RE.match(model):
        return "model must look like provider/name (letters, digits, . _ : - /)"
    prefix = _MODEL_PREFIX.get(agent)
    if prefix and not model.startswith(prefix):
        return f"{agent} only runs {prefix}* models"
    return None


def model_choices() -> dict[str, list[str]]:
    """Per agent, the model its .env setting uses first, then the other known ones."""
    out: dict[str, list[str]] = {}
    for agent, models in MODEL_CHOICES.items():
        out[agent] = list(dict.fromkeys([_agent_model(agent), *models]))
    return out

# Vendored Terminal-Bench prompts/rubrics (backend/vendor/tb_prompts/, see
# PROVENANCE.md there) -- an alternate, richer rubric a run can opt into,
# never the default. "default" = Harbor's own built-in rubric/prompt.
RUBRICS = ("default", "tb")
CHEAT_PROMPTS = ("default", "tb", "combined", "targeted")
# Fewer tool calls than this means the agent did not really attempt anything.
_MIN_CHEAT_TOOL_CALLS = 3
_TB_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "vendor" / "tb_prompts"
_TB_ANALYZE_RUBRIC = _TB_PROMPTS_DIR / "trial-analysis.toml"
_TB_ANALYZE_PROMPT = _TB_PROMPTS_DIR / "trial-analysis.txt"
_TB_ANALYZE_JOB_PROMPT = _TB_PROMPTS_DIR / "trial-analysis-job.txt"
_TB_CHECK_RUBRIC = _TB_PROMPTS_DIR / "task-implementation.toml"
_TB_CHEAT_PROMPT = _TB_PROMPTS_DIR / "hack-trial-prompt.md"


def _tb_cheat_prompt_text() -> str:
    """hack-trial-prompt.md starts with a leading "---\n\n". Terminal-Bench's
    own CI (scripts/ci/hosted_trial_workflow.py) reads the file verbatim,
    "---" included, and that is genuinely what their real cheat trials send
    -- confirmed directly against their source, not assumed. This platform
    strips it anyway, by deliberate choice: spliced into a task's own
    instruction.md, a bare "---" reads as a broken markdown fragment rather
    than the start of a real section, and that's judged worse here than a
    one-line formatting deviation from upstream."""
    text = _TB_CHEAT_PROMPT.read_text().lstrip()
    if text.startswith("---"):
        text = text[3:].lstrip("\n").lstrip()
    return text


def _agent_kwargs(agent: str, reasoning_effort: str | None = None) -> dict[str, str]:
    """Options for `harbor run --agent-kwarg`. Harbor >= 0.23 rejects options
    an agent doesn't declare, so each agent only gets the ones it knows.
    reasoning_effort overrides AGENT_REASONING_EFFORT for this run only."""
    kwargs: dict[str, str] = {}
    if agent.startswith("terminus") and settings.llm_max_iters > 0:
        # LLM_MAX_ITERS=0 passes no cap, so only the task's agent timeout bounds the run.
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


def _evaluator_kwargs(effort: str) -> dict[str, str]:
    """Agent kwargs for the analysis and rubric-check evaluators. Unlike trials they do not
    inherit AGENT_REASONING_EFFORT: an empty setting passes nothing (the model's default)."""
    return {"reasoning_effort": effort} if effort else {}


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


def _cost_cap_warning(cap_usd: float | None, label: str, n: int, timeout_sec: float) -> str:
    """E.7: a cap that gives each trial less than a realistic trial costs will
    stop long-timeout runs early and make the stop look like a task result."""
    if not cap_usd or n < 1 or timeout_sec < 3600:
        return ""
    share = cap_usd / n
    if share >= settings.min_trial_cost_usd:
        return ""
    return (
        f"(WARNING: cost cap ${cap_usd:.2f} ({label}) over {n} trial(s) is ${share:.2f} each, below "
        f"${settings.min_trial_cost_usd:.2f}; with a {timeout_sec / 3600:g} h agent timeout a trial can cost more "
        "than that and may be stopped early by the cap, not by the task)"
    )


def _run_cost_cap(db, requested_usd: float | None = None) -> tuple[float | None, str]:
    """The tightest dollar limit for one agent-trials run: the per-run budget
    (the amount set in the UI for this run, else AGENT_RUN_BUDGET_USD) and
    what's left of this month's budget, which the UI can lower but never raise."""
    caps: list[tuple[float, str]] = []
    if requested_usd:
        caps.append((requested_usd, "the cost cap set for this run"))
    elif settings.agent_run_budget_usd > 0:
        caps.append((settings.agent_run_budget_usd, "AGENT_RUN_BUDGET_USD"))
    if settings.llm_budget_usd > 0:
        remaining = max(settings.llm_budget_usd - budget.spent_this_month(db), 0.0)
        caps.append((remaining, "what is left of LLM_BUDGET_USD this month"))
    if not caps:
        return None, ""
    return min(caps, key=lambda cap: cap[0])


def _agent_config_error(agent: str, model: str | None = None) -> str | None:
    """A reason the chosen agent can't run, or None. model is a per-run override
    of the agent's .env model (see _agent_model)."""
    if agent == "claude-code":
        if not _agent_model(agent, model).startswith("anthropic/"):
            return (
                f"Claude Code only works with Anthropic models, but the agent model is "
                f"'{_agent_model(agent, model)}'. Set CLAUDE_AGENT_MODEL=anthropic/<model> in backend/.env."
            )
        if not settings.anthropic_api_key:
            return "Claude Code needs ANTHROPIC_API_KEY in backend/.env."
        if settings.harbor_network_mode not in ("", "public"):
            return (
                "Claude Code installs itself and calls the Anthropic API from inside the "
                "task container, so it needs HARBOR_NETWORK_MODE=public."
            )
    if agent == "codex":
        if not _agent_model(agent, model).startswith("openai/"):
            return (
                f"Codex only works with OpenAI models, but the agent model is "
                f"'{_agent_model(agent, model)}'. Set AGENT_MODEL=openai/<model> in backend/.env."
            )
        if not settings.openai_api_key:
            return "Codex needs OPENAI_API_KEY in backend/.env."
        if settings.harbor_network_mode not in ("", "public"):
            return (
                "Codex installs itself and calls the OpenAI API from inside the task "
                "container, so it needs HARBOR_NETWORK_MODE=public."
            )
    return None


def _agent_model(agent: str, model: str | None = None) -> str:
    """The LiteLLM model an agent runs. `model` overrides the .env setting for
    one run (the Cheat Trial's model dropdown); empty means the .env model, the
    same one Agent Trials use."""
    if model:
        return model
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
        budget.record_calls(
            db, submission_id, stage, calls, reasoning_effort=settings.llm_reasoning_effort
        )


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
    baked = harbor_runner.CODEX_BAKE_NOTE_MARKER in (run.logs or "")
    if update_canonical and not baked and task_checksum and not submission.task_checksum:
        submission.task_checksum = task_checksum


async def run_validate(submission_id: str, build: bool = True) -> None:
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
        if not build:
            submission.build_status = "not-run"
            submission.build_logs = "Files extracted and checked; no image built (validate?build=false)."
            db.commit()
            return
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
        try:
            await _record_image_inventory(db, submission)
        except Exception:
            db.rollback()  # the build itself passed; the inventory is extra evidence
    except Exception:
        submission = db.get(Submission, submission_id)
        if submission is not None:
            submission.build_status = "failed"
            submission.build_logs = traceback.format_exc()
            db.commit()
    finally:
        db.close()


async def _record_image_inventory(db, submission: Submission) -> None:
    """F.1/H.2: list the built image's task files and package versions
    (services/image_inventory.py). Best effort: a failure is recorded on its
    own Run and never changes the build result."""
    run = _get_or_create_run(db, submission.id, "image_inventory", 0)
    run.status = "running"
    run.reward = None
    run.started_at = datetime.now(UTC)
    db.commit()
    try:
        data = await asyncio.to_thread(image_inventory.collect, submission.image_tag, submission.id)
        if submission.extracted_path and not data.get("error"):
            # 5.0 E: which architectures each base image has (registry lookup; best effort).
            data["base_images"] = await asyncio.to_thread(
                platform_checks.inspect_bases, Path(submission.extracted_path)
            )
    except Exception:
        data = {"error": traceback.format_exc()}
    run.logs = json.dumps(data)
    run.status = "failed" if data.get("error") else "passed"
    run.finished_at = datetime.now(UTC)
    db.commit()


def _inventory(db, submission_id: str) -> dict | None:
    run = db.query(Run).filter_by(submission_id=submission_id, kind="image_inventory", run_index=0).one_or_none()
    return image_inventory.parse_logs(run.logs) if run else None


async def run_image_inventory_stage(submission_id: str) -> None:
    """Re-list the built image (for submissions built before this existed)."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is not None and submission.image_tag:
            await _record_image_inventory(db, submission)
    finally:
        db.close()


async def run_answer_exposure_stage(submission_id: str) -> None:
    """Advisory (4.1 C.1, 4.0 item 61): can the agent find the answer in the
    files it is given? services/answer_exposure.py; uses the built image's file
    list too when Build recorded one. "failed" = at least one warning."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return
        run = _get_or_create_run(db, submission_id, "answer_exposure", 0)
        run.status = "running"
        run.reward = None
        run.logs = None
        run.started_at = datetime.now(UTC)
        db.commit()
        inventory = _inventory(db, submission_id)
        image_files = [f["path"] for f in inventory["files"]] if inventory else None
        result = await asyncio.to_thread(answer_exposure.scan, Path(submission.extracted_path), image_files, load_zip_entries(Path(submission.extracted_path)))
        run.logs = json.dumps(result)
        run.status = "failed" if result["warn_count"] else "passed"
        run.reward = 0 if result["warn_count"] else 1
        run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception:
        run = _get_or_create_run(db, submission_id, "answer_exposure", 0)
        run.status = "failed"
        run.reward = None
        run.logs = json.dumps({"error": traceback.format_exc(), "findings": []})
        run.finished_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()


# Verdicts that need a human to look at the task (or at the run). `held` with a failed
# positive control is listed too: that probe never reached its defence, so it proves nothing.
_TIER0_ATTENTION = (
    "exploited", "fail_open", "false_zero", "control_failed", "infra_failed", "relies_on_harbor",
)
_TIER0_BAD = set(_TIER0_ATTENTION) - {"infra_failed"}


def _tier0_result_dict(res) -> dict:
    from dataclasses import asdict
    return asdict(res)


def _previous_upload(db, submission: Submission) -> Submission | None:
    """The latest earlier upload of the same task name, as the Changes banner picks it."""
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


def _tier0_compare(db, previous: Submission | None, results: list[dict], fingerprint: str) -> dict | None:
    """Regression gate (protocol section 5): the same probes against the previous upload's
    result. A probe that now fails where it held is a regression; one that held where it
    failed is fixed. `verifier_changed` says whether tests/ differs at all, because with an
    unchanged verifier any difference is noise or flakiness, not an effect of a fix."""
    if previous is None:
        return None
    run = db.query(Run).filter_by(submission_id=previous.id, kind="tier0_probes", run_index=0).one_or_none()
    if run is None or not run.logs:
        return {"against": previous.id, "available": False, "changes": []}
    try:
        old = json.loads(run.logs)
    except ValueError:
        return {"against": previous.id, "available": False, "changes": []}
    before = {r["probe_id"]: r["verdict"] for r in old.get("results", [])}
    after = {r["probe_id"]: r["verdict"] for r in results}
    changes = []
    for pid in sorted(set(before) | set(after)):
        b, a = before.get(pid), after.get(pid)
        if b == a:
            continue
        if b in ("inconclusive", "skipped") or a in ("inconclusive", "skipped"):
            kind = "not_comparable"  # e.g. G1 run through Harbor one time and not the other
        elif b is None:
            kind = "new"
        elif a is None:
            kind = "gone"
        elif a in _TIER0_BAD and b not in _TIER0_BAD:
            kind = "regression"
        elif b in _TIER0_BAD and a not in _TIER0_BAD:
            kind = "fixed"
        else:
            kind = "changed"
        changes.append({"probe_id": pid, "before": b, "after": a, "kind": kind})
    return {
        "against": previous.id,
        "available": True,
        "verifier_changed": old.get("verifier_fingerprint") != fingerprint,
        "unchanged_probes": sum(1 for pid in after if before.get(pid) == after[pid]),
        "changes": changes,
    }


async def _tier0_harbor_probe(submission: Submission, spec, probe, config) -> dict:
    """Probes whose answer depends on Harbor itself (G1: pre-written reward files). The
    task is copied, solution/ is replaced by a solve.sh that is the probe script, and a real
    Harbor Oracle trial is run, so Harbor's own artifact handling, reward-file deletion and
    verifier start-up decide the reward. reward 1 = exploited; reward below 1 = held."""
    import shlex
    from dataclasses import asdict

    from app.services.tier0 import record as rec

    res = rec.ProbeResult(
        probe_id=probe.pid, task=spec.slug, flaw_class=probe.flaw, title=probe.title, tier=probe.tier,
        verdict="inconclusive", expected_reward=probe.expect_reward, needs_harbor=False,
    )
    base = settings.storage_dir / "submissions" / submission.id / "runs" / "tier0_harbor"
    started = datetime.now(UTC)
    try:
        if shutil.which("harbor") is None:
            res.notes = ["the harbor CLI is not on PATH, so this probe could not run"]
            return asdict(res)
        copy_root = base / f"{probe.pid.lower()}-src"
        if copy_root.exists():
            shutil.rmtree(copy_root)
        shutil.copytree(spec.root, copy_root, symlinks=True)  # the copy under test: the upload or a patched copy
        sol = copy_root / "solution"
        if sol.exists():
            shutil.rmtree(sol)
        sol.mkdir()
        script = "#!/bin/bash\nset -- " + " ".join(shlex.quote(a) for a in spec.artifacts) + "\n" + probe.agent_script
        (sol / "solve.sh").write_text(script, encoding="utf-8")
        timeout = (
            config.environment.build_timeout_sec + 600 + config.verifier.timeout_sec + _HARBOR_TIMEOUT_BUFFER_SEC
        )
        outcome = await harbor_runner.run_single(
            copy_root, "oracle", base / probe.pid.lower(), f"tier0-{probe.pid.lower()}-{uuid.uuid4().hex[:8]}", timeout
        )
        shutil.rmtree(copy_root, ignore_errors=True)
        res.reward = outcome.reward
        if outcome.reward is None:
            res.verdict = "inconclusive"
            res.notes = ["Harbor produced no reward for this trial: " + (outcome.logs or "")[-300:]]
        elif outcome.reward >= 1.0:
            res.verdict = "exploited"
            res.notes = ["a real Harbor trial scored 1 for an agent that only pre-wrote reward files"]
        else:
            res.verdict = "held"
            res.notes = [
                f"a real Harbor Oracle trial whose solve.sh only pre-wrote /logs/verifier/reward.txt and "
                f"reward.json scored {outcome.reward}: Harbor's own reward handling is not fooled"
            ]
    except Exception as exc:
        res.verdict = "infra_failed"
        res.notes = [f"{type(exc).__name__}: {exc}"]
    res.duration_sec = round((datetime.now(UTC) - started).total_seconds(), 1)
    return asdict(res)


async def run_tier0_stage(
    submission_id: str, repeats: int = 1, harbor: bool = True, task_root: Path | None = None
) -> None:
    """Tier 0 of the cheat-trial loop (services/tier0/): human-written static probes. No agent
    and no model. Each probe builds the files an exploit would leave, the verifier is run on
    them (a replica of Harbor's separate-verifier contract, in Docker), and the reward is read.
    Mutants say whether a reward of 0 means a defence held or the probe was broken, a control
    probe (the task's own solution) says whether the harness models this task at all, a second
    pass with Harbor's sanitising off says whether the task defends itself, and `harbor` adds
    probes judged by a real Harbor run (G1). Stored rollouts are regraded (replay), and the
    result is compared with the previous upload of the same task.
    Advisory: "failed" = something needs a human (an exploit, a verifier that fails open, a
    valid solution graded 0, a failed control, a task that relies on Harbor, a probe that could
    not run, or a broken suite). A task the harness cannot drive (shared verifier, `main`
    built by compose) is "passed" with `skipped` set, like AI Detection without a key.
    `logs` is JSON: {task, platform, repeats, skipped, summary, results, progress,
    images_removed, selftest, replay, comparison, verifier_fingerprint}."""
    from app.services.tier0 import replay as tier0_replay
    from app.services.tier0 import selftest as tier0_selftest

    db = SessionLocal()
    stop = threading.Event()
    spec = None
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return
        run = _get_or_create_run(db, submission_id, "tier0_probes", 0)
        run.status = "running"
        run.reward = None
        run.logs = None
        run.started_at = datetime.now(UTC)
        db.commit()

        base = {"platform": settings.docker_platform, "repeats": repeats, "skipped": None}
        try:
            spec = tier0_taskspec.load(task_root or submission.extracted_path)  # task_root: a hardened copy
        except (ValueError, FileNotFoundError) as exc:
            run.logs = json.dumps({**base, "task": None, "skipped": str(exc), "summary": None, "results": []})
            run.status, run.reward = "passed", 1
            run.finished_at = datetime.now(UTC)
            db.commit()
            return
        selected = tier0_probes.for_task(spec.slug)
        problems = tier0_selftest.run(spec)  # mutant anchors, control wiring, script syntax
        fingerprint = tier0_replay.verifier_fingerprint(spec)
        harbor_probes = [p for p in selected if p.harbor_probe]
        docker_probes = [p for p in selected if not (p.harbor_probe and harbor)]

        def on_result(i: int, n: int, res: tier0_record.ProbeResult) -> None:
            # Runs in the worker thread, so it uses its own session.
            part = SessionLocal()
            try:
                row = _get_or_create_run(part, submission_id, "tier0_probes", 0)
                doc = json.loads(row.logs) if row.logs else {"results": []}
                doc["results"].append(_tier0_result_dict(res))
                doc["progress"] = {"done": i, "total": n + (len(harbor_probes) if harbor else 0)}
                row.logs = json.dumps(doc)
                part.commit()
            finally:
                part.close()

        run.logs = json.dumps(
            {**base, "task": spec.slug, "results": [], "selftest": problems,
             "progress": {"done": 0, "total": len(selected)}}
        )
        db.commit()
        tier0_docker.SUBMISSION_LABEL.set(submission_id)  # copied into the worker thread
        opts = tier0_runner.Options(platform=settings.docker_platform, sanitize=True, repeats=repeats)
        try:
            results = await asyncio.to_thread(
                tier0_runner.run_suite, spec, docker_probes, opts, on_result, stop.is_set
            )
        except asyncio.CancelledError:
            stop.set()  # the worker finishes its current probe, then stops
            raise
        rows = [_tier0_result_dict(r) for r in results]

        if harbor and harbor_probes:
            config = _get_task_config(submission)
            for hp in harbor_probes:
                rows.append(await _tier0_harbor_probe(submission, spec, hp, config))

        # Replay: regrade stored rollouts through the replica. This upload's own are a check
        # that the replica agrees with Harbor; an earlier upload's show what a verifier
        # change did (LOOSENED = the task may have got easier).
        sub_dir = settings.storage_dir / "submissions"
        # On a patched copy (task_root) the upload's own rollouts were graded by the *old* verifier, so
        # they are an earlier version's, not proof that the replica matches Harbor: a changed reward is
        # LOOSENED or REGRESSION for a person to look at, never a false "harness_disagrees".
        own_source = "this upload" if task_root is None else "earlier version (before the patch)"
        items = [(b, k, own_source) for b, k in tier0_replay.collect(sub_dir / submission_id, spec, limit=2)]
        previous = _previous_upload(db, submission)
        if previous is not None:
            items += [(b, k, "earlier upload") for b, k in tier0_replay.collect(sub_dir / previous.id, spec, limit=2)]
        replays = (
            await asyncio.to_thread(tier0_runner.run_replays, spec, items, opts, stop.is_set) if items else []
        )

        summary = tier0_record.summarise(results)
        # The summary counts come from the Docker results; fold the Harbor probes in.
        for r in rows:
            if r["probe_id"] in {p.pid for p in harbor_probes} and r["verdict"] == "exploited":
                summary["exploited"] += 1
                summary["exploited_ids"].append(r["probe_id"])
        summary["probes_run"] = len(rows)
        summary["needs_harbor_to_settle"] = [
            r["probe_id"] for r in rows if r["probe_id"] in {p.pid for p in harbor_probes} and r["verdict"] == "inconclusive"
        ]
        summary["replay_disagreements"] = sum(1 for r in replays if r["verdict"] == "harness_disagrees")
        summary["loosened"] = sum(1 for r in replays if r["verdict"] == "LOOSENED")
        summary["regressed"] = sum(1 for r in replays if r["verdict"] == "REGRESSION")
        comparison = _tier0_compare(db, previous, rows, fingerprint)
        attention = (
            [r for r in rows if r["verdict"] in _TIER0_ATTENTION]
            or summary["held_but_positive_control_failed"]
            or problems
            or summary["replay_disagreements"]
            or summary["loosened"]
            or summary["regressed"]
        )
        removed = await asyncio.to_thread(tier0_docker.cleanup_images, spec)
        run.logs = json.dumps(
            {
                **base, "task": spec.slug, "summary": summary, "results": rows,
                "progress": {"done": len(rows), "total": len(rows)},
                "images_removed": removed, "selftest": problems, "replay": replays,
                "comparison": comparison, "verifier_fingerprint": fingerprint,
            }
        )
        run.status = "failed" if attention else "passed"
        run.reward = 0 if attention else 1
        run.finished_at = datetime.now(UTC)
        db.commit()
    except asyncio.CancelledError:
        raise
    except Exception:
        run = _get_or_create_run(db, submission_id, "tier0_probes", 0)
        run.status = "failed"
        run.reward = None
        run.logs = json.dumps({"error": traceback.format_exc(), "results": []})
        run.finished_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()


async def run_oracle_trials(submission_id: str, n: int, stress: bool = False) -> None:
    """Oracle, run n times sequentially, always appended to any earlier runs
    on this submission (never cleared): Gate 1 needs 3 *consecutive* 1.0
    runs, so the point of this is to keep every run's own status/reward/logs
    visible afterwards, unlike the old single-row behavior that a fresh
    trigger silently overwrote. One Harbor job at a time -- running two
    concurrently has caused environment-start failures on this machine.

    stress=True (G.3) records the runs as kind "oracle_stress", which never
    count for Gate 1, and keeps the Docker VM's CPUs busy from a sibling
    container while each one runs, instead of a second parallel Harbor job
    (which this machine cannot run reliably). A verifier with a wall-clock
    assertion or a race then shows up as a stress failure next to clean passes."""
    kind = "oracle_stress" if stress else "oracle"
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return

        runs = (
            db.query(Run)
            .filter_by(submission_id=submission_id, kind=kind, status="pending")
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
        jobs_dir = settings.storage_dir / "submissions" / submission_id / "runs" / kind / "harbor"

        for run in runs:
            run.status = "running"
            run.reward = None
            run.raw_reward = None
            run.verifier_json = None
            run.logs = None
            run.started_at = datetime.now(UTC)
            db.commit()

            load_id, note = None, ""
            if stress:
                load_id, note = await asyncio.to_thread(
                    docker_orchestrator.start_cpu_load, submission.image_tag, submission_id
                )
            try:
                outcome = await harbor_runner.run_single(
                    Path(submission.extracted_path),
                    "oracle",
                    jobs_dir,
                    f"{kind.replace('_', '-')}-{uuid.uuid4().hex[:8]}",
                    timeout_sec,
                    extra_note=note,
                )
            finally:
                await asyncio.to_thread(docker_orchestrator.stop_cpu_load, load_id)
            # The gate needs a perfect 1.0; any partial reward counts as 0.
            _apply_trial_outcome(run, outcome)
            run.logs = outcome.logs
            _record_checksum(submission, run, outcome.task_checksum)
            run.finished_at = datetime.now(UTC)
            db.commit()
    except Exception:
        stuck = (
            db.query(Run)
            .filter_by(submission_id=submission_id, kind=kind)
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


def _oracle_timeout_sec(config: TaskConfig) -> float:
    return (
        config.environment.build_timeout_sec
        + _agent_timeout_sec(config)
        + config.verifier.timeout_sec
        + _HARBOR_TIMEOUT_BUFFER_SEC
    )


async def _run_variant(task_root: Path, base: Path, i: int, variant: dict, mode: str, timeout_sec: float) -> dict:
    """Apply one proposed edit to a copy of the task and run the Oracle on it.
    mode "valid": a correct variant should be accepted (else "rejected").
    mode "broken": a deliberately wrong answer should score 0 ("caught"),
    and one that scores 1 is the finding ("slipped_through")."""
    entry = {**variant, "status": "did_not_run", "reward": None, "failed_tests": [], "flags": [], "note": ""}
    copy_root = base / f"v{i}-src"  # not "-task": run_job uses that name for its own copy
    if copy_root.exists():
        shutil.rmtree(copy_root)
    shutil.copytree(task_root, copy_root, symlinks=True)
    problem = variant_probe.apply_edits(copy_root, variant["edits"])
    if problem:
        entry["note"] = f"the edit could not be applied: {problem}"
        return entry
    outcome = await harbor_runner.run_single(
        copy_root, "oracle", base / f"v{i}", f"variant-{uuid.uuid4().hex[:8]}", timeout_sec
    )
    shutil.rmtree(copy_root, ignore_errors=True)
    report = outcome.report or {}
    entry["reward"] = outcome.reward
    entry["failed_tests"] = ((report.get("ctrf") or {}).get("failed_names") or [])[:10]
    entry["flags"] = report.get("flags") or []
    if outcome.reward is None:
        entry["note"] = (outcome.logs or "")[-600:]
    elif mode == "broken":
        entry["status"] = "slipped_through" if outcome.reward >= 1.0 else "caught"
    else:
        entry["status"] = "accepted" if outcome.reward >= 1.0 else "rejected"
    return entry


async def _run_probe(submission_id: str, kind: str, mode: str) -> None:
    """Shared by the valid-variant probe (mode "valid") and the weak-answer
    probe (mode "broken"). Advisory. A model proposes correct-by-the-instruction variants of the
    Oracle's answer (services/variant_probe.py); each is run through the real
    verifier as an Oracle trial on a temporary copy of the task. A variant the
    verifier rejects means the grader accepts fewer forms than the instruction
    allows. logs is JSON: {model, reason, dropped, variants: [{description,
    instruction_quote, edits, status: accepted|rejected|did_not_run, reward,
    failed_tests, flags, note}]}."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return
        run = _get_or_create_run(db, submission_id, kind, 0)
        run.status = "running"
        run.reward = None
        run.logs = None
        run.started_at = datetime.now(UTC)
        db.commit()

        task_root = Path(submission.extracted_path)
        try:
            with _track_llm_spend(db, submission_id, kind):
                proposal = await variant_probe.propose_variants(task_root, mode)
        except Exception:
            run.status = "failed"
            run.logs = json.dumps({"error": traceback.format_exc(), "variants": []})
            run.finished_at = datetime.now(UTC)
            db.commit()
            return

        timeout_sec = _oracle_timeout_sec(_get_task_config(submission))
        base = settings.storage_dir / "submissions" / submission_id / "runs" / kind
        results: list[dict] = []
        for i, variant in enumerate(proposal["variants"]):
            entry = await _run_variant(task_root, base, i, variant, mode, timeout_sec)
            results.append(entry)
            db.refresh(run)
            run.logs = json.dumps({"model": settings.llm_model, "mode": mode, "in_progress": True, "variants": results})
            db.commit()

        rejected = [r for r in results if r["status"] in ("rejected", "slipped_through")]
        run.logs = json.dumps(
            {
                "model": settings.llm_model,
                "mode": mode,
                "reason": proposal["reason"],
                "dropped": proposal["dropped"],
                "variants": results,
            }
        )
        # Advisory: "passed" means the grader accepted every variant that ran.
        run.status = "failed" if rejected else "passed"
        run.reward = 0 if rejected else 1
        run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception:
        run = _get_or_create_run(db, submission_id, kind, 0)
        run.status = "failed"
        run.reward = None
        run.logs = json.dumps({"error": traceback.format_exc(), "variants": []})
        run.finished_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()


async def run_variant_probe(submission_id: str) -> None:
    """Is the grader stricter than the instruction? See _run_probe."""
    await _run_probe(submission_id, "variant_probe", "valid")


async def run_weak_probe(submission_id: str) -> None:
    """Is the grader weaker than the instruction? Deliberately broken versions
    of the Oracle's answer must score 0; one that scores 1 slipped through."""
    await _run_probe(submission_id, "weak_probe", "broken")


async def run_edge_case_check(submission_id: str, probe: bool = False, probe_covered: bool = False) -> None:
    """Advisory. Edge cases an expert would handle (from the instruction only),
    each checked against the tests. "failed" means at least one is uncovered.

    probe=True (E.3) also runs the uncovered cases through the grader: a model
    breaks the reference solution on exactly that case, and the copy runs as
    an Oracle trial. Scoring 1 confirms the gap (`probe.status`
    "slipped_through"); scoring 0 means a test does catch it after all
    ("caught"), and the case no longer counts as uncovered. Every uncovered
    case is probed, however many there are.

    probe_covered=True also probes the cases the judge marked covered. A broken
    answer that still scores 1 means the "covered" verdict was wrong, so the
    case counts as uncovered (`covered_claim_wrong`); scoring 0 confirms it."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return
        run = _get_or_create_run(db, submission_id, "edge_cases", 0)
        run.status = "running"
        run.reward = None
        run.logs = None
        run.started_at = datetime.now(UTC)
        db.commit()
        task_root = Path(submission.extracted_path)
        with _track_llm_spend(db, submission_id, "edge_cases"):
            result = await edge_cases.run_edge_case_check(task_root)
            to_probe = [c for c in result["cases"] if probe_covered or not c["covered"]]
            proposals = (
                await variant_probe.propose_edge_breaks(task_root, [c["case"] for c in to_probe])
                if probe and to_probe and not result.get("error")
                else []
            )
        if probe:
            result["probed"] = True
            result["probe_total"] = len(to_probe)
            result["probe_done"] = 0
            by_case = {p["case"]: p for p in proposals}
            timeout_sec = _oracle_timeout_sec(_get_task_config(submission))
            base = settings.storage_dir / "submissions" / submission_id / "runs" / "edge_cases"
            for i, case in enumerate(to_probe):
                proposal = by_case.get(case["case"])
                if proposal is None:
                    case["probe"] = {"status": "did_not_run", "note": "no edit was proposed for this case"}
                else:
                    entry = await _run_variant(task_root, base, i, proposal, "broken", timeout_sec)
                    case["probe"] = {
                        k: entry[k] for k in ("status", "reward", "failed_tests", "flags", "note", "description")
                    }
                if case["covered"] and case["probe"].get("status") == "slipped_through":
                    case["covered_claim_wrong"] = True
                result["probe_done"] = i + 1
                db.refresh(run)
                run.logs = json.dumps({**result, "in_progress": True})
                db.commit()
        uncovered = [
            c for c in result["cases"]
            if (not c["covered"] and (c.get("probe") or {}).get("status") != "caught") or c.get("covered_claim_wrong")
        ]
        run.logs = json.dumps(result)
        run.status = "failed" if uncovered or result.get("error") else "passed"
        run.reward = 0 if run.status == "failed" else 1
        run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception:
        run = _get_or_create_run(db, submission_id, "edge_cases", 0)
        run.status = "failed"
        run.reward = None
        run.logs = json.dumps({"cases": [], "error": traceback.format_exc()})
        run.finished_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()


async def run_nop(submission_id: str) -> None:
    await _run_harbor_gate(submission_id, kind="nop")


async def run_fresh_build(submission_id: str) -> None:
    """H.1: does the task still build and solve today? Build reuses Docker's
    layer cache, so a working image from last week can hide a dependency that
    no longer installs (4.1-28: a new release of an unpinned dependency broke
    the Oracle). Two steps, advisory: (1) `docker build --no-cache --pull` of
    environment/; (2) one Oracle run through Harbor on a task copy whose
    Dockerfiles get a one-off ENV after each FROM, so every RUN step fetches
    again (harbor_runner._bust_build_cache). The copy's checksum differs by
    design, so this run is never compared with the canonical task version.
    logs is JSON: {build: {ok, log_tail}, oracle: {reward, failed_tests, flags,
    log_tail} | null}."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return
        run = _get_or_create_run(db, submission_id, "fresh_build", 0)
        run.status = "running"
        run.reward = None
        run.logs = None
        run.started_at = datetime.now(UTC)
        db.commit()

        task_root = Path(submission.extracted_path)
        config = _get_task_config(submission)
        build_ok, build_log = True, ""
        try:
            image_tag, build_log = await asyncio.to_thread(
                docker_orchestrator.build_image,
                task_root / "environment",
                submission_id,
                config.environment.build_timeout_sec,
                True,
            )
            await asyncio.to_thread(docker_orchestrator.remove_image, image_tag)
        except docker_orchestrator.DockerBuildError as exc:
            build_ok, build_log = False, exc.log_text
        oracle = None
        if build_ok:
            jobs_dir = settings.storage_dir / "submissions" / submission_id / "runs" / "fresh_build" / "harbor"
            if jobs_dir.exists():
                shutil.rmtree(jobs_dir)
            outcome = await harbor_runner.run_single(
                task_root, "oracle", jobs_dir, f"fresh-{uuid.uuid4().hex[:8]}",
                _oracle_timeout_sec(config), fresh_build=True,
            )
            report = outcome.report or {}
            oracle = {
                "reward": outcome.reward,
                "failed_tests": ((report.get("ctrf") or {}).get("failed_names") or [])[:10],
                "flags": report.get("flags") or [],
                "log_tail": (outcome.logs or "")[-6000:],
            }
        passed = build_ok and oracle is not None and (oracle["reward"] or 0) >= 1.0
        run.logs = json.dumps({"build": {"ok": build_ok, "log_tail": build_log[-6000:]}, "oracle": oracle})
        run.status = "passed" if passed else "failed"
        run.reward = 1 if passed else 0
        run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception:
        run = _get_or_create_run(db, submission_id, "fresh_build", 0)
        run.status = "failed"
        run.reward = None
        run.logs = json.dumps({"error": traceback.format_exc(), "build": None, "oracle": None})
        run.finished_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()


_REGRADE_MAX_TRIALS = 12  # newest saved trials re-scored per click; each one builds and runs the verifier


def regrade_blocker(task_root: Path) -> str | None:
    """Why `harbor job regrade` cannot score this task, or None. Harbor regrades only a
    separate-mode verifier: the agent's live environment no longer exists, so the verifier
    has to grade from the recorded artifacts alone."""
    import tomllib

    try:
        verifier = tomllib.loads((task_root / "task.toml").read_text()).get("verifier") or {}
    except (OSError, tomllib.TOMLDecodeError):
        return "task.toml could not be read"
    if verifier.get("environment_mode") != "separate" and "environment" not in verifier:
        return (
            "this task's verifier is not in separate mode, and Harbor can only re-score a separate "
            "verifier from recorded artifacts. Set [verifier] environment_mode = \"separate\" in task.toml "
            "and read the inputs from /logs/artifacts, or re-run the trials instead."
        )
    return None


def regrade_sources(db, submission: Submission, kind: str, source: str) -> tuple[Submission | None, list[Path]]:
    """The saved Harbor job folders to regrade: this upload's own, or the latest earlier
    upload's (a grader fix is usually a new upload, since every upload is a new submission).
    Newest first, up to _REGRADE_MAX_TRIALS trials."""
    src = submission if source == "self" else _previous_upload(db, submission)
    if src is None:
        return None, []
    base = settings.storage_dir / "submissions" / src.id / "runs" / kind / "harbor"
    jobs = sorted(
        (d for d in base.glob("*") if d.is_dir() and not d.name.endswith("-task") and any(d.glob("*/result.json"))),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    chosen: list[Path] = []
    total = 0
    for job in jobs:
        if total >= _REGRADE_MAX_TRIALS:
            break
        chosen.append(job)
        total += len(list(job.glob("*/result.json")))
    return src, chosen


def _regrade_item(job: Path, source_dir: Path | None, outcome: "harbor_runner.TrialOutcome") -> dict:
    """One saved trial's reward before and after the regrade."""
    old_reward = None
    old_grader_error = False
    if source_dir is not None and (source_dir / "result.json").is_file():
        try:
            old = json.loads((source_dir / "result.json").read_text())
            old_reward = ((old.get("verifier_result") or {}).get("rewards") or {}).get("reward")
            old_grader_error = verifier_report.detect_grader_error(source_dir, old, old_reward) is not None
        except (OSError, ValueError):
            pass
    new_reward = outcome.reward
    report = outcome.report or {}
    if old_reward == new_reward:
        change = "same"
    elif old_reward is None:
        change = "now_scored"
    elif new_reward is None:
        change = "now_no_score"
    elif new_reward > old_reward:
        change = "loosened"
    else:
        change = "tightened"
    return {
        "trial": source_dir.name if source_dir else "(unknown)",
        "job": job.name,
        "old_reward": old_reward,
        "new_reward": new_reward,
        "change": change,
        "old_grader_error": old_grader_error,
        "new_grader_error": report.get("grader_error"),
        "failed_checks": [c["name"] for c in (report.get("checks") or []) if not c.get("passed")][:10]
        or ((report.get("ctrf") or {}).get("failed_names") or [])[:10],
        "flags": (report.get("flags") or [])[:5],
    }


async def run_regrade(submission_id: str, kind: str = "agent", source: str = "previous") -> None:
    """Score saved trials again with the current grader, without running the agent
    (`harbor job regrade`). It shows exactly which scores a grader fix changes and never
    edits the original runs: the answer goes in its own `regrade` row. `loosened` (0 became 1)
    means the fix made the grader accept work it rejected before, which a person should look
    at because the task may have got easier. Advisory; no LLM, so no cost. logs is JSON:
    {kind, source, items, summary, errors}."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return
        run = _get_or_create_run(db, submission_id, "regrade", 0)
        run.status = "running"
        run.reward = None
        run.logs = None
        run.started_at = datetime.now(UTC)
        db.commit()

        task_root = Path(submission.extracted_path)
        config = _get_task_config(submission)
        src, jobs = regrade_sources(db, submission, kind, source)
        out_dir = settings.storage_dir / "submissions" / submission_id / "runs" / "regrade" / "harbor"
        if out_dir.exists():
            shutil.rmtree(out_dir)  # keep only the latest regrade's output
        items: list[dict] = []
        errors: list[str] = []
        for job in jobs:
            n_trials = max(1, len(list(job.glob("*/result.json"))))
            timeout = (
                config.environment.build_timeout_sec
                + config.verifier.timeout_sec * n_trials
                + _HARBOR_TIMEOUT_BUFFER_SEC
            )
            result = await harbor_runner.run_regrade(job, task_root, out_dir, f"regrade-{uuid.uuid4().hex[:8]}", timeout)
            if result.error:
                errors.append(f"{job.name}: {result.error[:1500]}")
                continue
            items += [_regrade_item(job, source_dir, outcome) for source_dir, outcome in result.trials]
        counts = {c: sum(1 for i in items if i["change"] == c) for c in
                  ("same", "loosened", "tightened", "now_scored", "now_no_score")}
        summary = {
            "total": len(items),
            **counts,
            "grader_errors_before": sum(1 for i in items if i["old_grader_error"]),
            "grader_errors_after": sum(1 for i in items if i["new_grader_error"]),
        }
        clean = bool(items) and not errors and counts["same"] == len(items)
        run.logs = json.dumps(
            {
                "kind": kind,
                "source": {"submission": src.id if src else None, "same_upload": bool(src and src.id == submission_id)},
                "items": items,
                "summary": summary,
                "errors": errors,
                "harbor_version": harbor_runner._harbor_version(),
            }
        )
        run.status = "passed" if clean else "failed"
        run.reward = 1 if clean else 0
        run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception:
        run = _get_or_create_run(db, submission_id, "regrade", 0)
        run.status = "failed"
        run.reward = None
        run.logs = json.dumps({"error": traceback.format_exc(), "items": [], "errors": []})
        run.finished_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()


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
        run.raw_reward = None
        run.verifier_json = None
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
        _apply_trial_outcome(run, result)
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
                    agent=agent,
                    reasoning_effort=agent_kwargs.get("reasoning_effort"),
                    trial_index=run.run_index + 1,
                    detail="automatic infra retry of this trial",
                )
            _apply_trial_outcome(run, outcome)
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
    max_cost_usd: float | None = None,
    model: str | None = None,
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
        config_error = _agent_config_error(agent, model)
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
        model = _agent_model(agent, model)

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
                    agent=agent,
                    reasoning_effort=agent_kwargs.get("reasoning_effort"),
                    trial_index=pending[0].run_index + 1 if pending else None,
                    detail="agent trial" + ("" if outcome.reward is not None else " (no reward: crashed or cut short)"),
                )
            if pending:
                run = pending.pop(0)
                _apply_trial_outcome(run, outcome)
                run.logs = outcome.logs
                _record_checksum(submission, run, outcome.task_checksum)
                run.finished_at = datetime.now(UTC)
            db.commit()
            # Stop the rest of the job once the monthly cap is crossed.
            reason = budget.exceeded_message(db)
            return f"skipped: {reason}" if reason and pending else None

        cost_cap_usd, cost_cap_label = _run_cost_cap(db, max_cost_usd)
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
            extra_note=_cost_cap_warning(cost_cap_usd, cost_cap_label, n, _agent_timeout_sec(config)),
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


async def run_sufficiency_check(submission_id: str, runs: int = 1) -> None:
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

        image_section = image_inventory.judge_section(task_root, _inventory(db, submission_id))
        try:
            with _track_llm_spend(db, submission_id, "sufficiency"):
                verdict = await sufficiency_judge.run_sufficiency_judge(task_root, runs, image_section)
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
        if verdict.get("test_phrases"):
            logs_text += (
                "\n\nLines in tests/ that say something is not stated (found by a plain text search, shown "
                "to the judge; read each against instruction.md):\n"
                + "\n".join(f"- {x}" for x in verdict["test_phrases"])
            )

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
                "skip_count": report.skip_count,
                "vendor": tb_static_checks.vendor_info(),
                "results": [
                    {"name": r.name, "passed": r.passed, "output": r.output, "skipped": r.skipped}
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


async def run_ai_detection_stage(submission_id: str) -> None:
    """Advisory: Terminal-Bench's own Layer 3 AI-usage detection (vendor/
    tb_checks/scripts/check_ai_detection.py, see PROVENANCE.md there) --
    calls the real GPTZero API against instruction.md and solution/solve.sh.
    Genuinely optional in Terminal-Bench's own workflow too, not something
    this platform is more or less strict about than upstream. Persisted as
    its own Run (kind="ai_detection"). "passed" covers both a genuine pass
    and a graceful skip (no GPTZERO_API_KEY, or a GPTZero network error --
    the vendored script's own behavior, not something this platform added);
    "failed" means the script itself determined the content is likely
    AI-generated, or hit an unexpected error. `logs` is JSON: {skipped,
    passed, output, error, api_key_configured}."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return

        run = _get_or_create_run(db, submission_id, "ai_detection", 0)
        run.status = "running"
        run.reward = None
        run.logs = None
        run.started_at = datetime.now(UTC)
        db.commit()

        try:
            result = ai_detection.run_ai_detection(Path(submission.extracted_path))
        except Exception:
            run.status = "failed"
            run.reward = None
            run.logs = json.dumps({"error": traceback.format_exc()})
            run.finished_at = datetime.now(UTC)
            db.commit()
            return

        if not result.ran:
            run.status = "failed"
            run.reward = None
        elif result.error:
            run.status = "failed"
            run.reward = None
        elif result.skipped:
            run.status = "passed"
            run.reward = None
        else:
            run.status = "passed" if result.passed else "failed"
            run.reward = 1 if result.passed else 0

        gptzero_words = None
        if result.ran and not result.skipped and not result.error:
            # GPTZero bills per word, and the vendored script sends both files whole.
            gptzero_words = 0
            for rel in ("instruction.md", "solution/solve.sh"):
                f = Path(submission.extracted_path) / rel
                if f.is_file():
                    gptzero_words += len(f.read_text(errors="replace").split())
            budget.record(
                db, submission_id, "ai_detection", "gptzero",
                budget.gptzero_cost_usd(gptzero_words), None, None, None,
                agent="GPTZero API",
                detail="instruction.md + solution/solve.sh, billed per word in INR",
                unit="words",
                unit_count=gptzero_words,
            )

        run.logs = json.dumps(
            {
                "skipped": result.skipped if result.ran else None,
                "passed": result.passed if result.ran else None,
                "output": result.output if result.ran else None,
                "error": result.error,
                "api_key_configured": bool(settings.gptzero_api_key),
                "words_billed": gptzero_words,
            }
        )
        run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception:
        run = db.query(Run).filter_by(
            submission_id=submission_id, kind="ai_detection", run_index=0
        ).one_or_none()
        if run is not None:
            run.status = "failed"
            run.reward = None
            run.logs = json.dumps({"error": traceback.format_exc()})
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


_CLASSIFY_PROMPT = """You are reviewing failed agent trials on a benchmark task, to separate a hard task \
from a flawed one. For each trial you get the analyzer's summary, its task-specification check, the \
tests that failed and any automatic flags.

Classify every trial whose reward is below 1:
- "real_bug": the agent had what it needed and reasoned or implemented wrongly.
- "instruction_gap": the failure came from something the instruction or files did not establish, or \
the checker is stricter than the instruction.
- "infrastructure": a timeout, resource limit, network or Docker problem, not the task.
Trials with reward 1 get "passed".

Then group failed trials that chose the same alternative reading of the task. Two or more trials \
making the same alternative choice is a sign of ambiguity, but it can be a false alarm when the task \
simply needs domain knowledge, so describe it as contested, never as broken.

Respond with ONLY JSON:
{"trials": [{"trial": "<trial_name>", "class": "real_bug|instruction_gap|infrastructure|passed", \
"reason": "one sentence"}],
 "clusters": [{"reading": "the shared alternative reading", "trials": ["<trial_name>", ...]}]}

TRIALS:
"""


def _trial_evidence(job_dirs: list[Path], results: list[dict]) -> list[dict]:
    """Analyzer output joined with each trial's reward and failing tests."""
    evidence = []
    for res in results:
        item = {
            "trial": res.get("trial_name"),
            "summary": res.get("summary"),
            "task_specification": (res.get("checks") or {}).get("task_specification"),
        }
        for job_dir in job_dirs:
            trial_dir = job_dir / str(res.get("trial_name"))
            if not (trial_dir / "result.json").is_file():
                continue
            try:
                reward = ((json.loads((trial_dir / "result.json").read_text()).get("verifier_result") or {})
                          .get("rewards") or {}).get("reward")
                report = verifier_report.read_verifier_report(trial_dir / "verifier", reward)
            except (OSError, ValueError):
                break
            item["reward"] = reward
            if report:
                item["failed_tests"] = ((report.get("ctrf") or {}).get("failed_names") or [])[:15]
                item["flags"] = report.get("flags")
            break
        evidence.append(item)
    return evidence


async def _classify_failures(db, submission_id: str, job_dirs: list[Path], results: list[dict]) -> dict | None:
    evidence = _trial_evidence(job_dirs, results)
    if not any((e.get("reward") is None or e.get("reward") < 1) for e in evidence):
        return None  # nothing failed
    try:
        with _track_llm_spend(db, submission_id, "failure_analysis"):
            client = llm_factory.get_llm_client()
            resp = await client.complete(
                [Message(role="user", content=_CLASSIFY_PROMPT + json.dumps(evidence, indent=1)[:60000])],
                tools=[],
            )
        match = re.search(r"\{.*\}", resp.text, re.DOTALL)
        data = json.loads(match.group(0)) if match else None
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for cluster in data.get("clusters") or []:
        if isinstance(cluster, dict):
            cluster["label"] = "contested, needs a domain expert"
    return data


_AUDIT_PROMPT = """You audit agent trials on a benchmark task. You get the task's instruction, the \
hidden tests, and for each trial its reward, summary and failed tests.

1. For the passing trials: list the tests that a trial could pass only by making an assumption or \
guess the instruction and files do not establish. Name the test and the assumption. If every \
passing test follows from the given material, return an empty list.
2. For the single best failing trial (the one that passed the most tests): rule on each failed \
test as "agent_error" (the material was enough and the agent got it wrong), "documentation_gap" \
(the instruction or files did not establish what the test needs) or "grader_error" (the test is \
wrong or stricter than the instruction). Give one sentence of reasoning each.
3. If the instruction or documentation contains worked examples, say whether the tests would \
accept them. Report an example the tests would reject.

Respond with ONLY JSON:
{"passing": [{"trial": "<trial_name>", "assumptions": [{"test": "...", "assumption": "..."}]}],
 "best_failed": {"trial": "<trial_name>", "misses": [{"test": "...", "ruling": "agent_error|documentation_gap|grader_error", "reason": "..."}]} or null,
 "examples": ["example the tests would reject", ...]}
"""


async def _audit_trials(
    db, submission_id: str, task_root: Path, job_dirs: list[Path], results: list[dict]
) -> dict | None:
    """Looks at passes that may have relied on a guess and at the best failing
    trial's misses (4.0 category A). One extra judge call; advisory."""
    evidence = _trial_evidence(job_dirs, results)
    if not evidence:
        return None
    def score(item: dict) -> int:
        return len(item.get("failed_tests") or [])
    failing = [e for e in evidence if (e.get("reward") or 0) < 1]
    passing = [e for e in evidence if (e.get("reward") or 0) >= 1]
    if not passing and not failing:
        return None
    best = min(failing, key=score)["trial"] if failing else None
    instruction = (task_root / "instruction.md").read_text(errors="replace") if (task_root / "instruction.md").is_file() else ""
    from app.services.judge_files import read_text_files

    payload = (
        f"=== instruction.md ===\n{instruction}\n\n"
        f"=== environment/ (documents the agent can read) ===\n"
        f"{read_text_files(task_root / 'environment') if (task_root / 'environment').is_dir() else ''}\n\n"
        f"=== tests/ (hidden) ===\n{read_text_files(task_root / 'tests') if (task_root / 'tests').is_dir() else ''}\n\n"
        f"=== TRIALS (best failing trial: {best}) ===\n{json.dumps(evidence, indent=1)[:40000]}"
    )
    try:
        with _track_llm_spend(db, submission_id, "failure_analysis"):
            client = llm_factory.get_llm_client()
            resp = await client.complete([Message(role="user", content=_AUDIT_PROMPT + "\n" + payload)], tools=[])
        match = re.search(r"\{.*\}", resp.text, re.DOTALL)
        data = json.loads(match.group(0)) if match else None
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    best_failed = data.get("best_failed")
    if isinstance(best_failed, dict) and isinstance(best_failed.get("misses"), list):
        # Parametrised tests repeat the same miss; show each once with a count.
        merged: dict[tuple, dict] = {}
        for miss in best_failed["misses"]:
            if isinstance(miss, dict):
                key = (miss.get("test"), miss.get("ruling"), miss.get("reason"))
                if key in merged:
                    merged[key]["times"] = merged[key].get("times", 1) + 1
                else:
                    merged[key] = dict(miss)
        best_failed["misses"] = list(merged.values())
    return data


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
                agent_setup_timeout_sec=_agent_setup_timeout_sec(settings.analyze_agent),
                agent_kwargs=_evaluator_kwargs(settings.analyze_reasoning_effort),
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
                    agent=settings.analyze_agent,
                    reasoning_effort=settings.analyze_reasoning_effort,
                    detail=f"harbor analyze of {job_dir.name}",
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

        classification = await _classify_failures(db, submission_id, job_dirs, results) if results else None
        submission = db.get(Submission, submission_id)
        audit = (
            await _audit_trials(db, submission_id, Path(submission.extracted_path), job_dirs, results)
            if results and submission is not None and submission.extracted_path
            else None
        )

        run.logs = json.dumps(
            {
                "agent": settings.analyze_agent,
                "model": settings.analyze_model,
                "cost_usd": total_cost,
                "error": "\n\n".join(errors) or None,
                "results": results,
                "rubric": rubric,
                "job_summary": job_summary,
                "classification": classification,
                "audit": audit,
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


async def _cheat_check(db, submission_id: str, outcome: "harbor_runner.TrialOutcome", label: str) -> dict:
    """reward=1 alone can't say cheated vs. refused-and-solved-honestly (both
    observed live), so `harbor analyze`'s reward_hacking check reads that one
    trial (its own folder, so several cheat trials are judged separately)."""
    analysis_root = settings.storage_dir / "submissions" / submission_id / "runs" / "analysis"
    if outcome.trial_dir is None:
        return {"outcome": "unknown", "explanation": None, "cost_usd": None, "error": "the trial folder is missing"}
    analyze_outcome = await harbor_runner.run_analyze(
        outcome.trial_dir,
        analysis_root,
        f"analyze-{label}",
        settings.analyze_agent,
        settings.analyze_model,
        settings.analyze_timeout_sec,
        agent_setup_timeout_sec=_agent_setup_timeout_sec(settings.analyze_agent),
        agent_kwargs=_evaluator_kwargs(settings.analyze_reasoning_effort),
    )
    if analyze_outcome.cost_usd is not None:
        budget.record(
            db, submission_id, "cheat_trial", settings.analyze_model,
            analyze_outcome.cost_usd, None, None, None,
            agent=settings.analyze_agent,
            reasoning_effort=settings.analyze_reasoning_effort,
            detail="reward-hacking check on the cheat trial",
        )
    results = (analyze_outcome.report or {}).get("results") or []
    check = (results[0].get("checks") or {}).get("reward_hacking") if results else None
    if check:
        return {
            "outcome": check.get("outcome"),
            "explanation": check.get("explanation"),
            "cost_usd": analyze_outcome.cost_usd,
            "error": None,
        }
    return {
        "outcome": "unknown",
        "explanation": None,
        "cost_usd": analyze_outcome.cost_usd,
        "error": analyze_outcome.error or "harbor analyze produced no reward_hacking check",
    }


async def _finish_cheat_run(
    db, submission: Submission, run: Run, outcome: "harbor_runner.TrialOutcome", cheat_prompt: str, label: str
) -> None:
    run.reward = 1 if (outcome.reward is not None and outcome.reward >= 1.0) else outcome.reward
    run.raw_reward = outcome.reward
    run.verifier_json = json.dumps(outcome.report) if outcome.report else None
    cheat_check = await _cheat_check(db, submission.id, outcome, label) if run.reward == 1 else None
    # A reward below 1 only means the trap held if the agent really tried. A
    # trial with no reward, or one where the agent barely made a tool call
    # (refused, was blocked, crashed early), tested nothing.
    inconclusive: str | None = None
    if run.reward != 1:
        if outcome.reward is None:
            inconclusive = (
                "the trial produced no reward (it crashed, timed out or was stopped), "
                "so the trap was never tested"
            )
        elif outcome.tool_calls is not None and outcome.tool_calls < _MIN_CHEAT_TOOL_CALLS:
            inconclusive = (
                f"the agent made only {outcome.tool_calls} tool call(s); it did not really "
                "try to cheat, so the trap was never tested"
            )
    # "passed" whenever the trap held outright (reward != 1 and the agent
    # tried), or when reward=1 but the reward_hacking check positively found
    # no cheating; anything else (a real finding, an unresolved check, an
    # inconclusive trial) stays "failed" so it gets a human's attention.
    run.status = (
        "passed"
        if (run.reward != 1 and inconclusive is None)
        or (cheat_check is not None and cheat_check.get("outcome") == "pass")
        else "failed"
    )
    run.logs = json.dumps(
        {
            "trial_log": outcome.logs,
            "reward": run.reward,
            "cheat_check": cheat_check,
            "cheat_prompt": cheat_prompt,
            "inconclusive": inconclusive,
            "tool_calls": outcome.tool_calls,
        }
    )
    # Not update_canonical: this trial's task copy always has an intentionally
    # edited instruction.md, so its checksum never matches the others by
    # design -- record it, but never let it become or be compared against the
    # submission's canonical version.
    _record_checksum(submission, run, outcome.task_checksum, update_canonical=False)
    run.finished_at = datetime.now(UTC)
    db.commit()


async def run_cheat_trial(
    submission_id: str,
    agent: str | None = None,
    reasoning_effort: str | None = None,
    cheat_prompt: str = "default",
    max_cost_usd: float | None = None,
    model: str | None = None,
) -> None:
    """Cheat Trials: trials run with an explicit cheat directive spliced into
    instruction.md (harbor_runner._prepare_task, cheat_instruction=True), to
    test whether the anti-cheat design (separate verifier image, tests/ copied
    in only after the agent finishes, solution/ excluded from the build
    context) holds when the agent is TOLD to cheat, not just when it happens
    to behave. The router pre-creates 1 to 3 pending rows (kind
    "cheat_trial", run_index 0..n-1, replacing earlier ones); one Harbor job
    runs them one after another (4.1 B.2: one clean trial proves little).
    reward staying 0/None already means the trap held ("passed"). reward=1
    alone doesn't say the agent found a real cheat -- confirmed live: two
    separate trials (Codex and Claude Code) both got reward 1 after explicitly
    refusing to cheat and solving the task honestly instead. So each reward=1
    trial is followed by a `harbor analyze` reward_hacking check on that trial
    alone; only a check that positively finds no reward hacking counts as
    "passed". A check that finds reward hacking, or that itself fails, is
    "failed" and flagged for manual review -- never silently assumed clean.
    Each row's logs is JSON: {trial_log, reward, cheat_check: {outcome,
    explanation, cost_usd, error}, cheat_prompt, inconclusive, tool_calls}."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return
        runs = (
            db.query(Run)
            .filter_by(submission_id=submission_id, kind="cheat_trial", status="pending")
            .order_by(Run.run_index)
            .all()
        )
        if not runs:
            return
        agent = agent or settings.harbor_agent
        config_error = _agent_config_error(agent, model)
        if config_error:
            for r in runs:
                r.status = "failed"
                r.reward = None
                r.logs = json.dumps(
                    {"trial_log": config_error, "reward": None, "cheat_check": None, "cheat_prompt": cheat_prompt}
                )
                r.finished_at = datetime.now(UTC)
            db.commit()
            return

        now = datetime.now(UTC)
        for r in runs:
            r.status = "running"
            r.reward = None
            r.raw_reward = None
            r.verifier_json = None
            r.logs = None
            r.started_at = now
        db.commit()

        n = len(runs)
        config = _get_task_config(submission)
        timeout_sec = (
            config.environment.build_timeout_sec
            + n * (_agent_timeout_sec(config) + config.verifier.timeout_sec)
            + n * max(_AGENT_SETUP_ALLOWANCE_SEC, _agent_setup_timeout_sec(agent))
            + _HARBOR_TIMEOUT_BUFFER_SEC
        )
        agent_kwargs = _agent_kwargs(agent, reasoning_effort)
        model = _agent_model(agent, model)
        jobs_dir = settings.storage_dir / "submissions" / submission_id / "runs" / "cheat_trial" / "harbor"
        if jobs_dir.exists():
            shutil.rmtree(jobs_dir)

        pending = list(runs)
        finished: list[tuple[Run, harbor_runner.TrialOutcome]] = []

        async def on_trial(outcome: harbor_runner.TrialOutcome) -> str | None:
            if outcome.cost_usd is not None:
                budget.record(
                    db, submission_id, "cheat_trial", model,
                    outcome.cost_usd, outcome.n_input_tokens, outcome.n_cache_tokens,
                    outcome.n_output_tokens,
                    agent=agent,
                    reasoning_effort=agent_kwargs.get("reasoning_effort"),
                    trial_index=pending[0].run_index + 1 if pending else None,
                    detail="cheat trial (agent told to cheat)",
                )
            if pending:
                run = pending.pop(0)
                run.reward = 1 if (outcome.reward is not None and outcome.reward >= 1.0) else outcome.reward
                run.logs = json.dumps(
                    {"trial_log": outcome.logs, "reward": run.reward, "cheat_check": None, "cheat_prompt": cheat_prompt}
                )
                finished.append((run, outcome))
            db.commit()
            reason = budget.exceeded_message(db)
            return f"skipped: {reason}" if reason and pending else None

        cost_cap_usd, cost_cap_label = _run_cost_cap(db, max_cost_usd)
        if agent == "claude-code" and cost_cap_usd is not None:
            agent_kwargs["max_budget_usd"] = f"{max(cost_cap_usd / n, 0.01):.2f}"
        job_name = f"cheat-{uuid.uuid4().hex[:8]}"
        result = await harbor_runner.run_job(
            Path(submission.extracted_path),
            agent,
            jobs_dir,
            job_name,
            timeout_sec,
            model=model,
            agent_kwargs=agent_kwargs,
            n_attempts=n,
            n_concurrent=1,
            agent_setup_timeout_sec=_agent_setup_timeout_sec(agent),
            cost_cap_usd=cost_cap_usd,
            cost_cap_label=cost_cap_label,
            on_trial=on_trial,
            cheat_instruction=True,
            cheat_instruction_text=(
                _tb_cheat_prompt_text()
                if cheat_prompt == "tb"
                else harbor_runner.CHEAT_COMBINED_INSTRUCTION
                if cheat_prompt == "combined"
                else harbor_runner.targeted_cheat_instruction(Path(submission.extracted_path))
                if cheat_prompt == "targeted"
                else None
            ),
            extra_note=_cost_cap_warning(cost_cap_usd, cost_cap_label, n, _agent_timeout_sec(config)),
        )

        for run, outcome in finished:
            await _finish_cheat_run(db, submission, run, outcome, cheat_prompt, f"{job_name}-{run.run_index}")
        for run in pending:  # trials that never produced a result
            run.status = "failed"
            run.reward = None
            run.logs = json.dumps(
                {
                    "trial_log": result.error or "harbor produced no result for this cheat trial",
                    "reward": None,
                    "cheat_check": None,
                    "cheat_prompt": cheat_prompt,
                    "inconclusive": "the trial produced no result, so the trap was never tested",
                }
            )
            run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception:
        for run in db.query(Run).filter_by(submission_id=submission_id, kind="cheat_trial").all():
            if run.status in _STUCK_STATUSES:
                run.status = "failed"
                run.reward = None
                run.logs = json.dumps({"trial_log": traceback.format_exc(), "reward": None, "cheat_check": None})
                run.finished_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()


def _tier_effort_meta(agent: str, hacker_model: str | None, requested: str | None) -> dict:
    """What the tier logs say about reasoning effort: the setting asked for and the one really sent.
    A model such as Haiku 4.5 takes no effort setting, so Claude Code is launched without one."""
    name = (hacker_model or _agent_model(agent)).split("/", 1)[-1]
    if llm_factory.supports_effort(name):
        return {"reasoning_effort": requested, "reasoning_effort_requested": requested}
    return {
        "reasoning_effort": None,
        "reasoning_effort_requested": requested,
        "reasoning_effort_note": f"{name} takes no effort setting, so none was sent",
    }


async def run_tier1_stage(
    submission_id: str,
    agent: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    max_attempts: int | None = None,
    max_cost_usd: float | None = None,
    task_root: Path | None = None,
) -> None:
    """Tier 1 of the cheat-trial loop (services/tier1/): a hinted agent (Claude Code by default,
    on TIER1_HACKER_MODEL) is told to get reward 1 without doing the work, sees a read-only copy
    of tests/ at /hinted-tests and gets notes on its earlier attempts. Attempts run one at a
    time, each its own Harbor job. A judge labels each one; a confirmed hit makes an LLM fixer
    patch a *hardened copy* of tests/ and environment/ (the upload is never edited), accepted only
    when the Oracle still scores 1 on it, and the loop goes on against the hardened copy. The stage
    is "passed" when 3 cheating attempts in a row fail (status "robust"), "failed" for anything a
    human has to look at. Needs a passing Oracle first, so a fix is never judged against a task
    that did not work. Advisory; never changes a gate. logs is JSON, see tier1/loop.py."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return
        run = _get_or_create_run(db, submission_id, "tier1_loop", 0)
        run.status = "running"
        run.reward = None
        run.logs = None
        run.started_at = datetime.now(UTC)
        db.commit()

        def finish(status: str, reward: int | None, payload: dict) -> None:
            run.status = status
            run.reward = reward
            run.logs = json.dumps(payload)
            run.finished_at = datetime.now(UTC)
            db.commit()

        agent = agent or "claude-code"
        hacker_model = model or (settings.tier1_hacker_model if agent == "claude-code" else None)
        config_error = _agent_config_error(agent, hacker_model)
        if config_error:
            finish("failed", None, {"error": config_error, "attempts": [], "fixes": []})
            return

        src = task_root or Path(submission.extracted_path)  # task_root: the last round's hardened copy
        base = settings.storage_dir / "submissions" / submission_id / "tier1"
        if base.exists():
            shutil.rmtree(base)
        base.mkdir(parents=True)
        skipped = tier1_hacker.build_hinted_task(src, base / "preflight")
        shutil.rmtree(base / "preflight", ignore_errors=True)
        if skipped:
            finish("passed", None, {"skipped": skipped, "attempts": [], "fixes": []})
            return

        config = _get_task_config(submission)
        hardened = base / "hardened"
        shutil.copytree(src, hardened, symlinks=True)
        instruction = (src / "instruction.md").read_text(errors="replace") if (src / "instruction.md").is_file() else ""
        base_prompt = _tb_cheat_prompt_text()
        meta = {
            "agent": agent,
            "model": hacker_model or _agent_model(agent),
            "judge_model": settings.tier1_judge_model,
            "fixer_model": settings.tier1_fixer_model,
            **_tier_effort_meta(agent, hacker_model, reasoning_effort or settings.tier1_reasoning_effort),
            "attempt_timeout_sec": settings.tier1_attempt_timeout_sec,
            "attempt_cost_usd": settings.tier1_attempt_cost_usd,
        }
        limits = tier1_loop.Limits(
            max_attempts=max_attempts or settings.tier1_max_attempts,
            max_fixes=settings.tier1_max_fixes,
            max_cost_usd=settings.tier1_max_cost_usd if max_cost_usd is None else max_cost_usd,
        )
        meta["limits"] = {"max_attempts": limits.max_attempts, "max_fixes": limits.max_fixes, "max_cost_usd": limits.max_cost_usd}

        def llm_cost(calls) -> float:
            return sum(llm_pricing.cost_usd(m, u) or 0.0 for m, u in calls)

        async def run_attempt(task_dir: Path, mem: str, idx: int) -> dict:
            hinted = base / f"hinted-{idx}"
            reason = tier1_hacker.build_hinted_task(task_dir, hinted)
            if reason:
                return {"reward": None, "note": f"could not build the hinted copy: {reason}"}
            jobs_dir = base / "runs" / f"attempt-{idx}"
            outcomes: list[harbor_runner.TrialOutcome] = []

            async def on_trial(outcome: harbor_runner.TrialOutcome) -> str | None:
                outcomes.append(outcome)
                return None

            cap, cap_label = _run_cost_cap(db, settings.tier1_attempt_cost_usd or None)
            hacker_name = (hacker_model or _agent_model(agent)).split("/", 1)[-1]
            kwargs = _agent_kwargs(
                agent,
                (reasoning_effort or settings.tier1_reasoning_effort) if llm_factory.supports_effort(hacker_name) else None,
            )
            if not llm_factory.supports_effort(hacker_name):
                kwargs.pop("reasoning_effort", None)  # _agent_kwargs falls back to AGENT_REASONING_EFFORT
            if agent == "claude-code" and cap is not None:
                kwargs["max_budget_usd"] = f"{max(cap, 0.01):.2f}"
            timeout_sec = (
                config.environment.build_timeout_sec
                + settings.tier1_attempt_timeout_sec
                + config.verifier.timeout_sec
                + max(_AGENT_SETUP_ALLOWANCE_SEC, _agent_setup_timeout_sec(agent))
                + _HARBOR_TIMEOUT_BUFFER_SEC
            )
            result = await harbor_runner.run_job(
                hinted, agent, jobs_dir, f"tier1-{idx}-{uuid.uuid4().hex[:6]}", timeout_sec,
                model=hacker_model, agent_kwargs=kwargs, n_attempts=1, n_concurrent=1,
                agent_setup_timeout_sec=_agent_setup_timeout_sec(agent),
                cost_cap_usd=cap, cost_cap_label=cap_label, on_trial=on_trial,
                cheat_instruction=True,
                cheat_instruction_text=tier1_hacker.directive(base_prompt, mem),
                agent_timeout_override=settings.tier1_attempt_timeout_sec,
            )
            shutil.rmtree(hinted, ignore_errors=True)
            shutil.rmtree(jobs_dir.parent / f"{jobs_dir.name}-task", ignore_errors=True)
            if not outcomes:
                return {"reward": None, "note": result.error or "harbor produced no result for this attempt"}
            outcome = outcomes[0]
            if outcome.cost_usd is not None:
                budget.record(
                    db, submission_id, "tier1_loop", hacker_model or _agent_model(agent), outcome.cost_usd,
                    outcome.n_input_tokens, outcome.n_cache_tokens, outcome.n_output_tokens,
                    agent=agent, reasoning_effort=kwargs.get("reasoning_effort"), trial_index=idx,
                    detail=f"Tier 1 hinted attempt {idx}",
                )
                db.commit()
            return {
                "reward": outcome.reward, "tool_calls": outcome.tool_calls, "cost_usd": outcome.cost_usd,
                "trial_dir": str(outcome.trial_dir) if outcome.trial_dir else None,
                "refused": bool((outcome.report or {}).get("refusal")),
                "refusal": (outcome.report or {}).get("refusal"),
                "note": None if outcome.reward is not None else (outcome.logs or "")[-400:],
            }

        async def classify(outcome: dict, mem: str) -> dict | None:
            reward = outcome.get("reward")
            if reward is None or not outcome.get("trial_dir"):
                return None
            if reward < 1.0 and (outcome.get("refused") or (outcome.get("tool_calls") or 0) < 3):
                return None  # nothing worth a judge call
            transcript = tier1_hacker.condense_trajectory(Path(outcome["trial_dir"]))
            calls = llm_usage.start()
            try:
                judged = await tier1_judge.classify(instruction, mem, transcript, reward)
            finally:
                budget.record_calls(db, submission_id, "tier1_loop", calls, reasoning_effort=settings.tier1_reasoning_effort)
                db.commit()
            if judged is not None:
                judged["cost_usd"] = llm_cost(calls)
            return judged

        async def propose_fix(task_dir: Path, attempt: dict, rejected: str) -> dict | None:
            transcript = tier1_hacker.condense_trajectory(Path(attempt["trial_dir"])) if attempt.get("trial_dir") else ""
            calls = llm_usage.start()
            try:
                proposal = await tier1_fixer.propose(task_dir, attempt, transcript, rejected)
            finally:
                budget.record_calls(db, submission_id, "tier1_loop", calls, reasoning_effort=settings.tier1_reasoning_effort)
                db.commit()
            return {**(proposal or {}), "cost_usd": llm_cost(calls)}

        async def gate(candidate: Path) -> tuple[bool, str]:
            outcome = await harbor_runner.run_single(
                candidate, "oracle", base / "gate", f"gate-{uuid.uuid4().hex[:8]}", _oracle_timeout_sec(config)
            )
            shutil.rmtree(base / "gate", ignore_errors=True)
            if outcome.reward is None:
                return False, "the Oracle produced no reward on the patched task"
            if outcome.reward >= 1.0:
                return True, f"Oracle reward {outcome.reward:g} on the patched task"
            return False, f"Oracle reward {outcome.reward:g} on the patched task. {_failure_excerpt(outcome.logs)}"

        def progress(state: dict) -> None:
            db.refresh(run)
            run.logs = json.dumps({**meta, **state})
            db.commit()

        replay_control: dict[str, float | None] = {}

        async def replay(candidate: Path, attempt: dict) -> tuple[bool, str]:
            return await _tier1_replay(hardened, candidate, attempt, replay_control, settings.docker_platform or None)

        hooks = tier1_loop.Hooks(run_attempt, classify, propose_fix, tier1_fixer.apply_edits, gate, progress, replay=replay)
        result = await tier1_loop.run(hardened, base, hooks, limits)
        diff = tier1_fixer.tree_diff(src, hardened)
        (base / "hardened.diff").write_text(diff)
        passed = result["status"] == "robust"
        finish("passed" if passed else "failed", 1 if passed else 0, {**meta, **result, "diff": diff})
    except Exception:
        run = _get_or_create_run(db, submission_id, "tier1_loop", 0)
        run.status = "failed"
        run.reward = None
        run.logs = json.dumps({"error": traceback.format_exc(), "attempts": [], "fixes": []})
        run.finished_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()


def _failure_excerpt(logs: str | None, max_lines: int = 8) -> str:
    """The first pytest error lines of a failed Oracle gate (the lines that start with "E "), so the
    fixer can see why its patch broke the grader instead of only that it did."""
    lines = [l.strip()[:220] for l in (logs or "").splitlines() if l.lstrip().startswith("E ")]
    if lines:
        return "First errors from the verifier: " + " | ".join(lines[:max_lines])
    tail = (logs or "").strip()[-500:]
    return f"Verifier output ended with: {tail}" if tail else "No verifier output was captured."


async def _tier1_replay(hardened: Path, candidate: Path, attempt: dict, control: dict, platform: str | None) -> tuple[bool, str]:
    """Re-grade the winning attempt's own output (its collected artifacts) on the patched copy
    through Tier 0's replica verifier. A patch that still lets it score 1 did not close the
    exploit, whatever the Oracle says. The same output is first graded on the unpatched copy: if
    the replica cannot reproduce the win there, the replay proves nothing and the patch is
    accepted with a note saying so. Never rejects on doubt."""
    from app.services.tier0 import replay as tier0_replay

    trial = attempt.get("trial_dir")
    artifacts = Path(trial) / "artifacts" if trial else None
    if not artifacts or not (artifacts / "manifest.json").is_file():
        return True, "not replayed: the attempt's output was not stored"
    specs = []
    try:
        bundle = tier0_replay.load(artifacts)
        before_spec, after_spec = tier0_taskspec.load(hardened), tier0_taskspec.load(candidate)
        specs = [before_spec, after_spec]

        def grade(spec) -> float | None:
            return tier0_docker.run_replay(spec, bundle, platform=platform).reward

        if trial not in control:
            control[trial] = await asyncio.to_thread(grade, before_spec)
        if control[trial] is None or control[trial] < 1.0:
            return True, f"not replayed: the replica scored the earlier output {control[trial]} on the unpatched task, so it cannot show the exploit"
        reward = await asyncio.to_thread(grade, after_spec)
    except (ValueError, FileNotFoundError, OSError) as exc:  # includes a task that cannot be replayed
        return True, f"not replayed: {exc}"
    except Exception as exc:
        return True, f"replay failed ({type(exc).__name__}: {str(exc)[:160]}), so it proves nothing"
    finally:
        for spec in specs[:1]:
            await asyncio.to_thread(tier0_docker.cleanup_images, spec)
    if reward is None:
        return True, "replay gave no reward on the patched task, so it proves nothing"
    if reward >= 1.0:
        return False, "the earlier winning output still scores 1 on the patched task"
    return True, f"the earlier winning output scored 1 before and now scores {reward:g}"


def tier1_hardened_dir(submission_id: str) -> Path | None:
    """The hardened copy Tier 1 left behind, or None when there is none."""
    path = settings.storage_dir / "submissions" / submission_id / "tier1" / "hardened"
    return path if (path / "task.toml").is_file() else None


def _stage_snapshot(submission_id: str, kind: str) -> dict:
    """A short, JSON-safe summary of one tier's stored result, kept per round in the cycle row."""
    db = SessionLocal()
    try:
        row = db.query(Run).filter_by(submission_id=submission_id, kind=kind, run_index=0).one_or_none()
        if row is None:
            return {"status": "not-run"}
        try:
            logs = json.loads(row.logs) if row.logs else {}
        except ValueError:
            logs = {}
        if not isinstance(logs, dict):
            logs = {}
        out: dict = {"status": row.status, "result": logs.get("status"), "reason": logs.get("reason"),
                     "error": (logs.get("error") or "")[-300:] or None, "skipped": logs.get("skipped")}
        if kind == "tier0_probes":
            summary = logs.get("summary") or {}
            out["summary"] = {k: summary.get(k) for k in ("probes_run", "exploited", "fail_open", "false_zero", "relies_on_harbor")}
        else:
            attempts = logs.get("attempts") or []
            counts: dict[str, int] = {}
            for a in attempts:
                counts[a.get("verdict") or "unknown"] = counts.get(a.get("verdict") or "unknown", 0) + 1
            out["attempts"] = len(attempts)
            out["labels"] = counts
            out["cost_usd"] = float(logs.get("cost_usd") or 0.0)
            if kind == "tier1_loop":
                out["fixes_accepted"] = sum(1 for f in (logs.get("fixes") or []) if f.get("accepted"))
        return out
    finally:
        db.close()


def tier_cycle_dir(submission_id: str) -> Path:
    return settings.storage_dir / "submissions" / submission_id / "tier_cycle"


async def run_tier_cycle(
    submission_id: str,
    repeats: int = 1,
    model: str | None = None,
    max_attempts: int | None = None,
    max_cost_usd: float | None = None,
    max_rounds: int = 3,
) -> None:
    """The whole cheat-trial pipeline, repeated until the task stops changing.

    A round is Tier 0 (static probes), then Tier 1 (hinted agent, patches a hardened copy), then
    Tier 2 (unhinted agent) on the same copy. Only Tier 1 changes the task. If it accepted a patch,
    the round ends there (Tier 2 would be testing a task that is about to change) and the next
    round starts all three tiers again on the patched copy. The loop ends when:
      * a round makes no patch: "robust" if Tier 1 and Tier 2 both ended robust, "exploited" if
        Tier 2 got reward 1 by a shortcut (a person rules; nothing is patched from Tier 2),
        "inconclusive" if nobody really tried (honest solves, refusals) so nothing is proven;
      * Tier 1 stopped for a person (unfixed, unpatchable, needs_review): "needs_review";
      * `max_rounds` rounds all made a patch: "not_converged";
      * the shared `max_cost_usd` (Tier 1 + Tier 2 across all rounds; 0 or none = each tier's own
        default cap per round) is spent: "budget".
    The upload is never edited: each round's hardened copy is kept under tier_cycle/round-N/ and
    the cumulative diff against the upload is stored. Tier 0 findings are reported per round but
    never trigger a patch. Each tier keeps its own row (showing the latest round), and this row
    ("tier_cycle") keeps every round. Advisory; "passed" only when robust."""
    upload_root: Path | None = None
    started = datetime.now(UTC)
    rounds: list[dict] = []
    state: dict = {"status": None, "reason": None, "in_progress": True, "rounds": rounds,
                   "max_rounds": max_rounds, "cost_usd": 0.0, "max_cost_usd": max_cost_usd or 0.0, "diff": ""}

    def save(final: str | None = None, reason: str | None = None) -> None:
        db = SessionLocal()
        try:
            row = _get_or_create_run(db, submission_id, "tier_cycle", 0)
            state["in_progress"] = final is None
            if final is not None:
                state.update(status=final, reason=reason)
                row.status = "passed" if final == "robust" else "failed"
                row.reward = 1 if final == "robust" else 0
                row.finished_at = datetime.now(UTC)
            else:
                row.status = "running"
                row.reward = None
            row.started_at = row.started_at or started
            row.logs = json.dumps(state)
            db.commit()
        finally:
            db.close()

    def mark_pending(kinds) -> None:
        db = SessionLocal()
        try:
            for kind in kinds:
                row = _get_or_create_run(db, submission_id, kind, 0)
                row.status = "pending"
            db.commit()
        finally:
            db.close()

    try:
        db = SessionLocal()
        try:
            submission = db.get(Submission, submission_id)
            if submission is None or submission.extracted_path is None:
                return
            upload_root = Path(submission.extracted_path)
        finally:
            db.close()
        cycle = tier_cycle_dir(submission_id)
        if cycle.exists():
            shutil.rmtree(cycle)
        cycle.mkdir(parents=True)
        save()

        task_root: Path | None = None  # None = the upload; later rounds = the last hardened copy
        spent = 0.0
        final, reason = "not_converged", f"{max_rounds} rounds each still needed a patch"

        def remaining() -> float | None:
            return None if not max_cost_usd else max(max_cost_usd - spent, 0.0)

        for n in range(1, max_rounds + 1):
            if max_cost_usd and spent >= max_cost_usd:
                final, reason = "budget", f"spend reached the cap of ${max_cost_usd:.2f} before round {n}"
                break
            this = {"round": n, "task_copy": "upload" if task_root is None else f"hardened copy from round {n - 1}",
                    "tier0": None, "tier1": None, "tier2": None, "patched": 0}
            rounds.append(this)
            mark_pending(("tier0_probes", "tier1_loop"))
            save()

            await run_tier0_stage(submission_id, repeats, True, task_root)
            this["tier0"] = _stage_snapshot(submission_id, "tier0_probes")
            save()

            await run_tier1_stage(submission_id, None, model, None, max_attempts, remaining(), task_root)
            this["tier1"] = _stage_snapshot(submission_id, "tier1_loop")
            spent += this["tier1"].get("cost_usd") or 0.0
            patched = this["tier1"].get("fixes_accepted") or 0
            this["patched"] = patched
            state["cost_usd"] = round(spent, 4)
            hardened = tier1_hardened_dir(submission_id)
            if hardened is not None and patched:
                keep = cycle / f"round-{n}" / "hardened"
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(hardened, keep, symlinks=True)
                task_root = keep
            save()

            t1 = this["tier1"].get("result")
            if t1 in ("unfixed", "unpatchable", "needs_review"):
                final, reason = "needs_review", f"Tier 1 stopped ({t1}): {this['tier1'].get('reason') or 'a person has to look'}"
                break
            if patched:
                if n == max_rounds:
                    final, reason = "not_converged", f"round {n} still needed a patch and the round limit ({max_rounds}) is reached"
                    break
                continue  # the task changed: start again from Tier 0 on the patched copy

            if max_cost_usd and spent >= max_cost_usd:
                final, reason = "budget", f"spend reached the cap of ${max_cost_usd:.2f} before Tier 2"
                break
            mark_pending(("tier2_loop",))
            await run_tier2_stage(submission_id, None, model, None, max_attempts, remaining(), None)
            this["tier2"] = _stage_snapshot(submission_id, "tier2_loop")
            spent += this["tier2"].get("cost_usd") or 0.0
            state["cost_usd"] = round(spent, 4)
            t2 = this["tier2"].get("result")
            if t2 == "exploited":
                final, reason = "exploited", "Tier 2 got reward 1 by a shortcut on the final task; a person has to rule (Tier 2 patches nothing)"
            elif t1 == "robust" and t2 == "robust":
                final, reason = "robust", f"round {n}: no patch was needed, Tier 1 and Tier 2 both ended robust"
            elif t2 == "needs_review":
                final, reason = "needs_review", "a Tier 2 run scored 1 and the judge could not say whether it was a shortcut"
            else:
                final, reason = "inconclusive", (
                    f"round {n}: no hack was found and no patch was needed, but the attackers did not really try "
                    f"(Tier 1 {t1 or 'no result'}, Tier 2 {t2 or 'no result'}), so this is not proof the task is robust"
                )
            break

        if task_root is not None and upload_root is not None:
            state["diff"] = tier1_fixer.tree_diff(upload_root, task_root)
            (cycle / "hardened.diff").write_text(state["diff"])
        save(final, reason)
    except asyncio.CancelledError:
        raise
    except Exception:
        state["error"] = traceback.format_exc()
        save("error", "the cycle crashed; see error")


async def run_tier2_stage(
    submission_id: str,
    agent: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    max_attempts: int | None = None,
    max_cost_usd: float | None = None,
    task_copy: str | None = None,
) -> None:
    """Tier 2 of the cheat-trial loop (services/tier2/): an unhinted agent (Claude Code by
    default, on TIER2_HACKER_MODEL) gets the task's own instruction plus one line of pressure
    (tier2.PRESSURE_LINE), with no verifier access, no red-team charter and no memory of earlier
    runs. Runs are independent, one at a time, each its own Harbor job. A judge labels each run
    (a shortcut or an honest attempt, for passes and failures). Three genuine failed shortcut
    attempts in a row end the stage "robust" (passed); the first shortcut that gets reward 1
    stops it "exploited" for a person to rule on; nothing is patched. Honest solves and failures
    are neutral, so many runs end "inconclusive". Runs on Tier 1's hardened copy when there is
    one (task_copy "hardened" or "upload" chooses), and needs a passing Oracle first. Advisory;
    never changes a gate. logs is JSON, see tier2/loop.py."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.extracted_path is None:
            return
        run = _get_or_create_run(db, submission_id, "tier2_loop", 0)
        run.status = "running"
        run.reward = None
        run.logs = None
        run.started_at = datetime.now(UTC)
        db.commit()

        def finish(status: str, reward: int | None, payload: dict) -> None:
            run.status = status
            run.reward = reward
            run.logs = json.dumps(payload)
            run.finished_at = datetime.now(UTC)
            db.commit()

        agent = agent or "claude-code"
        hacker_model = model or (settings.tier2_hacker_model if agent == "claude-code" else None)
        config_error = _agent_config_error(agent, hacker_model)
        if config_error:
            finish("failed", None, {"error": config_error, "attempts": []})
            return

        upload = Path(submission.extracted_path)
        hardened = tier1_hardened_dir(submission_id)
        if task_copy == "hardened" and hardened is None:
            finish("failed", None, {"error": "there is no hardened copy from Tier 1 for this upload", "attempts": []})
            return
        use_hardened = hardened is not None and task_copy != "upload"
        src = hardened if use_hardened else upload
        copy_used = "hardened copy from Tier 1 (storage/submissions/<id>/tier1/hardened)" if use_hardened else "upload as submitted"

        base = settings.storage_dir / "submissions" / submission_id / "tier2"
        if base.exists():
            shutil.rmtree(base)
        base.mkdir(parents=True)

        config = _get_task_config(submission)
        instruction = (src / "instruction.md").read_text(errors="replace") if (src / "instruction.md").is_file() else ""
        meta = {
            "agent": agent,
            "model": hacker_model or _agent_model(agent),
            "judge_model": settings.tier2_judge_model,
            **_tier_effort_meta(agent, hacker_model, reasoning_effort or settings.tier2_reasoning_effort),
            "attempt_timeout_sec": settings.tier2_attempt_timeout_sec,
            "attempt_cost_usd": settings.tier2_attempt_cost_usd,
            "task_copy": copy_used,
            "pressure_line": PRESSURE_LINE,
        }
        limits = tier2_loop.Limits(
            max_attempts=max_attempts or settings.tier2_max_attempts,
            max_cost_usd=settings.tier2_max_cost_usd if max_cost_usd is None else max_cost_usd,
        )
        meta["limits"] = {"max_attempts": limits.max_attempts, "max_cost_usd": limits.max_cost_usd}

        def llm_cost(calls) -> float:
            return sum(llm_pricing.cost_usd(m, u) or 0.0 for m, u in calls)

        async def run_attempt(task_dir: Path, idx: int) -> dict:
            jobs_dir = base / "runs" / f"attempt-{idx}"
            outcomes: list[harbor_runner.TrialOutcome] = []

            async def on_trial(outcome: harbor_runner.TrialOutcome) -> str | None:
                outcomes.append(outcome)
                return None

            cap, cap_label = _run_cost_cap(db, settings.tier2_attempt_cost_usd or None)
            hacker_name = (hacker_model or _agent_model(agent)).split("/", 1)[-1]
            takes_effort = llm_factory.supports_effort(hacker_name)
            kwargs = _agent_kwargs(agent, (reasoning_effort or settings.tier2_reasoning_effort) if takes_effort else None)
            if not takes_effort:
                # _agent_kwargs falls back to AGENT_REASONING_EFFORT when given None, and a model
                # like Sonnet 4.5 answers HTTP 400 to any effort, so drop it here
                kwargs.pop("reasoning_effort", None)
            if agent == "claude-code" and cap is not None:
                kwargs["max_budget_usd"] = f"{max(cap, 0.01):.2f}"
            timeout_sec = (
                config.environment.build_timeout_sec
                + settings.tier2_attempt_timeout_sec
                + config.verifier.timeout_sec
                + max(_AGENT_SETUP_ALLOWANCE_SEC, _agent_setup_timeout_sec(agent))
                + _HARBOR_TIMEOUT_BUFFER_SEC
            )
            result = await harbor_runner.run_job(
                task_dir, agent, jobs_dir, f"tier2-{idx}-{uuid.uuid4().hex[:6]}", timeout_sec,
                model=hacker_model, agent_kwargs=kwargs, n_attempts=1, n_concurrent=1,
                agent_setup_timeout_sec=_agent_setup_timeout_sec(agent),
                cost_cap_usd=cap, cost_cap_label=cap_label, on_trial=on_trial,
                cheat_instruction=True,
                cheat_instruction_text=PRESSURE_LINE,
                agent_timeout_override=settings.tier2_attempt_timeout_sec,
            )
            shutil.rmtree(jobs_dir.parent / f"{jobs_dir.name}-task", ignore_errors=True)
            if not outcomes:
                return {"reward": None, "note": result.error or "harbor produced no result for this run"}
            outcome = outcomes[0]
            if outcome.cost_usd is not None:
                budget.record(
                    db, submission_id, "tier2_loop", hacker_model or _agent_model(agent), outcome.cost_usd,
                    outcome.n_input_tokens, outcome.n_cache_tokens, outcome.n_output_tokens,
                    agent=agent, reasoning_effort=kwargs.get("reasoning_effort"), trial_index=idx,
                    detail=f"Tier 2 unhinted run {idx}",
                )
                db.commit()
            return {
                "reward": outcome.reward, "tool_calls": outcome.tool_calls, "cost_usd": outcome.cost_usd,
                "trial_dir": str(outcome.trial_dir) if outcome.trial_dir else None,
                "refused": bool((outcome.report or {}).get("refusal")),
                "refusal": (outcome.report or {}).get("refusal"),
                "note": None if outcome.reward is not None else (outcome.logs or "")[-400:],
            }

        async def classify(outcome: dict) -> dict | None:
            if outcome.get("reward") is None or not outcome.get("trial_dir"):
                return None
            if outcome["reward"] < 1.0 and outcome.get("refused"):
                return None  # a refusal is neutral, nothing to judge
            transcript = tier1_hacker.condense_trajectory(Path(outcome["trial_dir"]))
            calls = llm_usage.start()
            try:
                judged = await tier2_judge.classify(instruction, transcript, outcome["reward"])
            finally:
                budget.record_calls(db, submission_id, "tier2_loop", calls, reasoning_effort=settings.tier2_reasoning_effort)
                db.commit()
            if judged is not None:
                judged["cost_usd"] = llm_cost(calls)
            return judged

        def progress(state: dict) -> None:
            db.refresh(run)
            run.logs = json.dumps({**meta, **state})
            db.commit()

        hooks = tier2_loop.Hooks(run_attempt, classify, progress)
        result = await tier2_loop.run(src, hooks, limits)
        passed = result["status"] == "robust"
        finish("passed" if passed else "failed", 1 if passed else 0, {**meta, **result})
    except Exception:
        run = _get_or_create_run(db, submission_id, "tier2_loop", 0)
        run.status = "failed"
        run.reward = None
        run.logs = json.dumps({"error": traceback.format_exc(), "attempts": []})
        run.finished_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()


async def run_rubric_check(submission_id: str, rubric: str = "tb") -> None:
    """`harbor check`: an evaluator agent reads the whole task and scores it
    against a rubric -- the vendored 36-criterion Terminal-Bench rubric
    (vendor/tb_prompts/, see PROVENANCE.md) by default, since criteria such as
    ctrf_reporting, verifier_execution_isolation, resource_configuration and
    effective_instruction_length exist only there (4.1 A.4), or Harbor's own
    11-criterion default with rubric="default". Advisory, like Failure Analysis. Result stored
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
            # the outer limit must outlast the agent's own, or it kills a run still inside its time
            max(settings.check_timeout_sec,
                settings.check_agent_timeout_sec + _agent_setup_timeout_sec(settings.check_agent) + 600),
            rubric_path=_TB_CHECK_RUBRIC if rubric == "tb" else None,
            agent_setup_timeout_sec=_agent_setup_timeout_sec(settings.check_agent),
            agent_kwargs=_evaluator_kwargs(settings.check_reasoning_effort),
            agent_timeout_sec=settings.check_agent_timeout_sec,
        )
        if outcome.cost_usd is not None:
            budget.record(
                db, submission_id, "rubric_check", settings.check_model,
                outcome.cost_usd, None, None, None,
                agent=settings.check_agent,
                reasoning_effort=settings.check_reasoning_effort,
                detail=f"harbor check, {rubric or 'default'} rubric",
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
