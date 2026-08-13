import asyncio
import json
import shutil
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_settings
from app.db import SessionLocal
from app.models import Run, Submission
from app.services import (
    code_smell_judge,
    docker_orchestrator,
    leakage_scan,
    review_report,
    sufficiency_judge,
    validation_service,
)
from app.services.task_config import TaskConfig

settings = get_settings()


def _get_task_config(submission: Submission) -> TaskConfig:
    if submission.task_config_json:
        return TaskConfig.model_validate_json(submission.task_config_json)
    return TaskConfig()


def _llm_env_vars() -> dict[str, str]:
    env = {
        "LLM_PROVIDER": settings.llm_provider,
        "LLM_MODEL": settings.llm_model,
        "LLM_RPM": str(settings.llm_rpm),
        "LLM_MAX_ITERS": str(settings.llm_max_iters),
    }
    if settings.llm_tpm:
        env["LLM_TPM"] = str(settings.llm_tpm)

    key_map = {
        "anthropic": ("ANTHROPIC_API_KEY", settings.anthropic_api_key),
        "openai": ("OPENAI_API_KEY", settings.openai_api_key),
        "groq": ("GROQ_API_KEY", settings.groq_api_key),
        "gemini": ("GEMINI_API_KEY", settings.gemini_api_key),
        "fireworks": ("FIREWORKS_API_KEY", settings.fireworks_api_key),
    }
    key_name, key_value = key_map.get(settings.llm_provider, (None, None))
    if key_name and key_value:
        env[key_name] = key_value
    return env


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


def _resolve_reward(verify_exit: int, verifier_logs_dir: Path) -> int:
    """The canonical test.sh shape (mkdir /logs/verifier, run pytest, always
    `echo N > /logs/verifier/reward.txt` as the LAST command) makes the
    verify container's own exit code always 0 regardless of pass/fail — the
    real signal is the reward file's content. Prefer that; fall back to the
    process exit code only for scripts that don't write the file at all
    (e.g. a bare `exit 0`/`exit 1` test.sh)."""
    reward_file = verifier_logs_dir / "reward.txt"
    if reward_file.is_file():
        try:
            # Accept float-formatted rewards too (e.g. "1.0", "0.0") — a
            # natural, common way to write this that plain int() rejects
            # outright. Falling through to the exit-code fallback on that
            # ValueError silently produced a WRONG reward for any test.sh
            # following the documented "always exit 0, real signal is the
            # file" convention, since that convention makes verify_exit
            # always 0 regardless of pass/fail.
            return int(float(reward_file.read_text().strip()))
        except (ValueError, OSError):
            pass
    return 1 if verify_exit == 0 else 0


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
    await _run_solve_and_verify(submission_id, kind="oracle", run_index=0)


async def run_nop(submission_id: str) -> None:
    await _run_solve_and_verify(submission_id, kind="nop", run_index=0)


async def _run_solve_and_verify(submission_id: str, kind: str, run_index: int) -> None:
    """Shared oracle/nop flow: a 'doing' phase (oracle solves, nop does
    nothing) followed by a SEPARATE verifier-phase container that mounts
    tests/ only now. tests/ and solution/ are never both visible in the same
    container — this is the isolation property the whole design depends on.
    """
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.image_tag is None:
            return

        run = _get_or_create_run(db, submission_id, kind, run_index)
        run.status = "running"
        run.reward = None
        run.logs = None
        run.started_at = datetime.now(UTC)
        db.commit()

        config = _get_task_config(submission)
        task_root = Path(submission.extracted_path)

        # attempt-unique suffix: guards against container-name collisions if a
        # cancelled prior attempt's blocking docker call is still unwinding in
        # its thread (asyncio.Task.cancel() does not stop a to_thread call in
        # flight, so the old container may still exist briefly)
        attempt = uuid.uuid4().hex[:8]

        workdir = (
            settings.storage_dir
            / "submissions"
            / submission_id
            / "runs"
            / kind
            / str(run_index)
            / "workdir"
        )
        if workdir.exists():
            shutil.rmtree(workdir)  # clean slate — no stale files from a prior attempt
        workdir.mkdir(parents=True, exist_ok=True)

        container_workdir_path = await asyncio.to_thread(
            docker_orchestrator.get_image_workdir, submission.image_tag
        )
        await asyncio.to_thread(
            docker_orchestrator.seed_workdir_from_image,
            submission.image_tag,
            workdir,
            container_workdir_path,
            submission_id,
        )

        if kind == "oracle":
            solution_dir = task_root / "solution"
            solve_exit, solve_logs = await asyncio.to_thread(
                docker_orchestrator.run_phase,
                submission.image_tag,
                {
                    str(workdir): (container_workdir_path, "rw"),
                    str(solution_dir): ("/solution", "ro"),
                },
                ["bash", "/solution/solve.sh"],
                not config.environment.allow_internet,
                config.agent.timeout_sec,
                f"taskeval-{submission_id}-{kind}-{run_index}-{attempt}-solve",
                submission_id,
                config.environment.memory_mb,
                int(config.environment.cpus * 1e9),
            )
        else:  # nop: no doing-phase container at all; workdir stays empty
            solve_exit, solve_logs = 0, "(nop: no action taken)"

        tests_dir = task_root / "tests"
        verifier_logs_dir = workdir.parent / "verifier_logs"
        if verifier_logs_dir.exists():
            shutil.rmtree(verifier_logs_dir)
        verifier_logs_dir.mkdir(parents=True, exist_ok=True)

        verify_exit, verify_logs = await asyncio.to_thread(
            docker_orchestrator.run_phase,
            submission.image_tag,
            {
                str(workdir): (container_workdir_path, "rw"),
                str(tests_dir): ("/tests", "ro"),
                str(verifier_logs_dir): ("/logs/verifier", "rw"),
            },
            ["bash", "/tests/test.sh"],
            True,  # verifier never needs network
            config.verifier.timeout_sec,
            f"taskeval-{submission_id}-{kind}-{run_index}-{attempt}-verify",
            submission_id,
            config.environment.memory_mb,
            int(config.environment.cpus * 1e9),
        )

        reward = _resolve_reward(verify_exit, verifier_logs_dir)
        run.status = "passed" if reward == 1 else "failed"
        run.reward = reward
        run.logs = (
            f"=== SOLVE PHASE (exit {solve_exit}) ===\n{solve_logs}\n\n"
            f"=== VERIFY PHASE (exit {verify_exit}) ===\n{verify_logs}"
        )
        run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception:
        run = db.query(Run).filter_by(
            submission_id=submission_id, kind=kind, run_index=run_index
        ).one_or_none()
        if run is not None:
            run.status = "failed"
            run.reward = None
            run.logs = traceback.format_exc()
            run.finished_at = datetime.now(UTC)
            db.commit()
    finally:
        db.close()


async def run_agent_trials(submission_id: str, n: int) -> None:
    """Build the agent wrapper image once (reused across all N trials), then
    run trials SEQUENTIALLY (not parallel) — easier on rate limits and matches
    the isolation contract: the agent's own container never has tests/ or
    solution/ present, only afterward does a separate verifier-phase
    container (from the task's original image, not the wrapper) mount
    tests/ against the resulting workdir."""
    db = SessionLocal()
    try:
        submission = db.get(Submission, submission_id)
        if submission is None or submission.image_tag is None:
            return

        if submission.agent_wrapper_image_tag is None:
            try:
                wrapper_tag, _wrapper_logs = await asyncio.to_thread(
                    docker_orchestrator.build_agent_wrapper_image,
                    submission.image_tag,
                    submission_id,
                )
            except docker_orchestrator.DockerBuildError as e:
                runs = db.query(Run).filter_by(submission_id=submission_id, kind="agent").all()
                for r in runs:
                    r.status = "failed"
                    r.reward = None
                    r.logs = f"agent wrapper image build failed:\n{e.log_text}"
                    r.finished_at = datetime.now(UTC)
                db.commit()
                return
            submission.agent_wrapper_image_tag = wrapper_tag
            db.commit()

        config = _get_task_config(submission)
        task_root = Path(submission.extracted_path)
        instruction_path = task_root / "instruction.md"
        tests_dir = task_root / "tests"
        env_vars = _llm_env_vars()

        container_workdir_path = await asyncio.to_thread(
            docker_orchestrator.get_image_workdir, submission.image_tag
        )

        for run_index in range(n):
            attempt = uuid.uuid4().hex[:8]
            run = (
                db.query(Run)
                .filter_by(submission_id=submission_id, kind="agent", run_index=run_index)
                .one_or_none()
            )
            if run is None:
                continue  # router should have pre-created all n rows

            run.status = "running"
            run.reward = None
            run.logs = None
            run.started_at = datetime.now(UTC)
            db.commit()

            try:
                workdir = (
                    settings.storage_dir
                    / "submissions"
                    / submission_id
                    / "runs"
                    / "agent"
                    / str(run_index)
                    / "workdir"
                )
                if workdir.exists():
                    shutil.rmtree(workdir)
                workdir.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(
                    docker_orchestrator.seed_workdir_from_image,
                    submission.image_tag,
                    workdir,
                    container_workdir_path,
                    submission_id,
                )

                agent_env = dict(env_vars)
                agent_env["AGENT_WORKDIR"] = container_workdir_path
                agent_env["INSTRUCTION_PATH"] = "/task/instruction.md"
                agent_env["AGENT_TIMEOUT_SEC"] = str(config.agent.timeout_sec)

                solve_exit, solve_logs = await asyncio.to_thread(
                    docker_orchestrator.run_phase,
                    submission.agent_wrapper_image_tag,
                    {
                        str(workdir): (container_workdir_path, "rw"),
                        str(instruction_path): ("/task/instruction.md", "ro"),
                    },
                    None,  # rely on the wrapper image's ENTRYPOINT
                    False,  # agent needs real internet to reach the LLM API (documented MVP limitation)
                    config.agent.timeout_sec + 30,  # buffer over the loop's own internal timeout
                    f"taskeval-{submission_id}-agent-{run_index}-{attempt}-solve",
                    submission_id,
                    config.environment.memory_mb,
                    int(config.environment.cpus * 1e9),
                    agent_env,
                )

                agent_result_path = workdir / ".agent_result.json"
                agent_result_summary = (
                    agent_result_path.read_text()[:5000]
                    if agent_result_path.is_file()
                    else "(no .agent_result.json produced)"
                )

                verifier_logs_dir = workdir.parent / "verifier_logs"
                if verifier_logs_dir.exists():
                    shutil.rmtree(verifier_logs_dir)
                verifier_logs_dir.mkdir(parents=True, exist_ok=True)

                verify_exit, verify_logs = await asyncio.to_thread(
                    docker_orchestrator.run_phase,
                    submission.image_tag,
                    {
                        str(workdir): (container_workdir_path, "rw"),
                        str(tests_dir): ("/tests", "ro"),
                        str(verifier_logs_dir): ("/logs/verifier", "rw"),
                    },
                    ["bash", "/tests/test.sh"],
                    True,
                    config.verifier.timeout_sec,
                    f"taskeval-{submission_id}-agent-{run_index}-{attempt}-verify",
                    submission_id,
                    config.environment.memory_mb,
                    int(config.environment.cpus * 1e9),
                )

                reward = _resolve_reward(verify_exit, verifier_logs_dir)
                run.status = "passed" if reward == 1 else "failed"
                run.reward = reward
                run.logs = (
                    f"=== AGENT SOLVE PHASE (exit {solve_exit}) ===\n{solve_logs}\n\n"
                    f"=== AGENT RESULT ===\n{agent_result_summary}\n\n"
                    f"=== VERIFY PHASE (exit {verify_exit}) ===\n{verify_logs}"
                )
                run.finished_at = datetime.now(UTC)
                db.commit()
            except Exception:
                run.status = "failed"
                run.reward = None
                run.logs = traceback.format_exc()
                run.finished_at = datetime.now(UTC)
                db.commit()
                # continue to the next trial rather than aborting the whole batch
    except Exception:
        traceback.print_exc()
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
