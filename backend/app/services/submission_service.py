import json
import math
import re

from app.models import Run, Submission
from app.schemas import (
    AgentTrialsResult,
    BuildResult,
    ChecksumInfo,
    CodeSmellResult,
    CheatTrialResult,
    FailureAnalysisResult,
    RubricCheckResult,
    LeakageScanResult,
    OracleRunsResult,
    ReviewReportResult,
    StageResult,
    StageStatus,
    StaticChecksResult,
    TbStaticChecksResult,
    AiDetectionResult,
    SubmissionListItem,
    SubmissionSchema,
    SufficiencyResult,
    TrialResult,
)

_TERMINAL = {"passed", "failed", "not-run"}

# C10: build/trial logs and check reports embed absolute host paths
# (backend/storage/..., a candidate's own machine's home dir in task file
# content, etc.) -- fine for the platform's own UI, but a submission's
# username shouldn't leak into anything exported for outside reading. Only
# the username segment is redacted, not the whole path, so the rest (still
# useful for debugging) survives.
_HOST_PATH_RE = re.compile(r"(/(?:Users|home)/)([^/\s\"'\\]+)")


def redact_host_paths(value):
    """Recursively redacts `/Users/<user>/...` and `/home/<user>/...`
    segments in strings, dicts and lists -- used on export only (see
    routers.submissions.get_evidence_bundle), never on data stored or shown
    inside the platform itself."""
    if isinstance(value, str):
        return _HOST_PATH_RE.sub(lambda m: f"{m.group(1)}<redacted>", value)
    if isinstance(value, dict):
        return {k: redact_host_paths(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_host_paths(v) for v in value]
    return value


def _get_run(submission: Submission, kind: str, run_index: int = 0) -> Run | None:
    for r in submission.runs:
        if r.kind == kind and r.run_index == run_index:
            return r
    return None


def _stage_result(run: Run | None) -> StageResult:
    if run is None:
        return StageResult(status="not-run", reward=None, logs=None)
    return StageResult(status=run.status, reward=run.reward, logs=run.logs, task_checksum=run.task_checksum)  # type: ignore[arg-type]


def _invert_status(status: StageStatus) -> StageStatus:
    """nop's desired outcome is reward=0 (doing nothing shouldn't solve the
    task), so the displayed badge is the inverse of the literal reward-based
    status: reward=0 -> "passed" badge, reward=1 -> "failed" badge. Reward
    itself stays literal; only this presentation label flips."""
    if status == "passed":
        return "failed"
    if status == "failed":
        return "passed"
    return status


def _nop_stage_result(run: Run | None) -> StageResult:
    result = _stage_result(run)
    if result.status == "failed" and result.reward is None:
        # No reward at all means the run itself errored (build failure,
        # crash, timeout) — that must not display as a nop pass.
        return result
    return result.model_copy(update={"status": _invert_status(result.status)})


def _oracle_runs_result(submission: Submission) -> OracleRunsResult:
    oracle_runs = sorted(
        (r for r in submission.runs if r.kind == "oracle"), key=lambda r: r.run_index
    )
    n = len(oracle_runs)
    if n == 0:
        return OracleRunsResult(status="not-run", n=0, runs=[], all_passed=None)

    statuses = {r.status for r in oracle_runs}
    all_terminal = statuses <= _TERMINAL

    all_passed = None
    if all_terminal:
        all_passed = all(r.reward == 1 for r in oracle_runs)
        status: StageStatus = "passed" if all_passed else "failed"
    else:
        status = "running"

    runs = [
        TrialResult(index=r.run_index, reward=r.reward, logs=r.logs, task_checksum=r.task_checksum)
        for r in oracle_runs
    ]
    return OracleRunsResult(status=status, n=n, runs=runs, all_passed=all_passed)


def _pass_at_k(n: int, c: int, k: int) -> float:
    """The standard unbiased pass@k estimator (Codex/HumanEval):
    1 - C(n-c, k) / C(n, k) -- the probability that at least one of k
    trials sampled without replacement from the n actually run is a pass,
    given c of them passed. C6: unlike Harbor's own reporting (which only
    ever fills in powers-of-2 and multiples-of-5 k values -- pass@1 is never
    computed by Harbor for any job, by design), this is computed here for
    every k from 1 to n, so pass@1 and pass@3 are always available."""
    if k > n:
        raise ValueError(f"k={k} exceeds n={n}")
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def _pass_at_k_table(n: int, c: int) -> dict[str, float]:
    return {str(k): _pass_at_k(n, c, k) for k in range(1, n + 1)}


def _agent_trials_result(submission: Submission) -> AgentTrialsResult:
    agent_runs = sorted(
        (r for r in submission.runs if r.kind == "agent"), key=lambda r: r.run_index
    )
    n = len(agent_runs)
    if n == 0:
        return AgentTrialsResult(status="not-run", n=0, trials=[], pass_rate=None)

    statuses = {r.status for r in agent_runs}
    all_terminal = statuses <= _TERMINAL

    if not all_terminal:
        status: StageStatus = "running"
    else:
        status = "passed" if any(r.reward == 1 for r in agent_runs) else "failed"

    pass_rate = None
    pass_at_k = None
    if all_terminal:
        passed = sum(1 for r in agent_runs if r.reward == 1)
        pass_rate = passed / n
        pass_at_k = _pass_at_k_table(n, passed)

    trials = [
        TrialResult(index=r.run_index, reward=r.reward, logs=r.logs, task_checksum=r.task_checksum)
        for r in agent_runs
    ]
    return AgentTrialsResult(
        status=status, n=n, trials=trials, pass_rate=pass_rate, pass_at_k=pass_at_k
    )


def _sufficiency_result(run: Run | None) -> SufficiencyResult:
    if run is None:
        return SufficiencyResult(status="not-run", passed=None, logs=None)
    return SufficiencyResult(status=run.status, passed=(run.reward == 1) if run.reward is not None else None, logs=run.logs)  # type: ignore[arg-type]


def _leakage_scan_result(run: Run | None) -> LeakageScanResult:
    if run is None:
        return LeakageScanResult(status="not-run", passed=None, logs=None)
    return LeakageScanResult(status=run.status, passed=(run.reward == 1) if run.reward is not None else None, logs=run.logs)  # type: ignore[arg-type]


def _failure_analysis_result(run: Run | None) -> FailureAnalysisResult:
    if run is None:
        return FailureAnalysisResult(status="not-run", logs=None)
    return FailureAnalysisResult(status=run.status, logs=run.logs)  # type: ignore[arg-type]


def _cheat_trial_result(run: Run | None) -> CheatTrialResult:
    if run is None:
        return CheatTrialResult(status="not-run", reward=None, logs=None)
    return CheatTrialResult(status=run.status, reward=run.reward, logs=run.logs, task_checksum=run.task_checksum)  # type: ignore[arg-type]


def _checksum_info(submission: Submission) -> ChecksumInfo:
    """C1: did Build/Oracle/Nop/Agent Trials actually run against the same
    task version? Compares every Harbor-backed run's own task_checksum
    (oracle, nop, agent -- never cheat_trial, whose task copy always has an
    intentionally edited instruction.md) against the submission's canonical
    checksum (the first one any run reported). `consistent` stays null
    until at least one comparable run has reported a checksum."""
    canonical = submission.task_checksum
    mismatched: list[str] = []
    seen_any = False
    for r in submission.runs:
        if r.kind not in ("oracle", "nop", "agent") or not r.task_checksum:
            continue
        seen_any = True
        if canonical and r.task_checksum != canonical:
            label = r.kind if r.kind == "nop" else f"{r.kind}#{r.run_index}"
            mismatched.append(label)
    return ChecksumInfo(
        canonical=canonical,
        consistent=(len(mismatched) == 0) if seen_any else None,
        mismatched=mismatched,
    )


def _static_checks_result(run: Run | None) -> StaticChecksResult:
    if run is None:
        return StaticChecksResult(status="not-run", logs=None)
    return StaticChecksResult(status=run.status, logs=run.logs)  # type: ignore[arg-type]


def _tb_static_checks_result(run: Run | None) -> TbStaticChecksResult:
    if run is None:
        return TbStaticChecksResult(status="not-run", logs=None)
    return TbStaticChecksResult(status=run.status, logs=run.logs)  # type: ignore[arg-type]


def _ai_detection_result(run: Run | None) -> AiDetectionResult:
    if run is None:
        return AiDetectionResult(status="not-run", logs=None)
    return AiDetectionResult(status=run.status, logs=run.logs)  # type: ignore[arg-type]


def _rubric_check_result(run: Run | None) -> RubricCheckResult:
    if run is None:
        return RubricCheckResult(status="not-run", logs=None)
    return RubricCheckResult(status=run.status, logs=run.logs)  # type: ignore[arg-type]


def _review_report_result(run: Run | None) -> ReviewReportResult:
    if run is None:
        return ReviewReportResult(status="not-run", passed=None, logs=None)
    return ReviewReportResult(status=run.status, passed=(run.reward == 1) if run.reward is not None else None, logs=run.logs)  # type: ignore[arg-type]


def _code_smell_result(run: Run | None) -> CodeSmellResult:
    if run is None:
        return CodeSmellResult(status="not-run", passed=None, logs=None)
    return CodeSmellResult(status=run.status, passed=(run.reward == 1) if run.reward is not None else None, logs=run.logs)  # type: ignore[arg-type]


def to_schema(submission: Submission) -> SubmissionSchema:
    nop_run = _get_run(submission, "nop")
    sufficiency_run = _get_run(submission, "sufficiency")
    leakage_scan_run = _get_run(submission, "leakage_scan")
    code_smell_run = _get_run(submission, "code_smell")
    review_report_run = _get_run(submission, "review_report")
    return SubmissionSchema(
        id=submission.id,
        task_name=submission.task_name,
        uploaded_at=submission.uploaded_at,
        build=BuildResult(
            status=submission.build_status,  # type: ignore[arg-type]
            logs=submission.build_logs,
            image_tag=submission.image_tag,
        ),
        oracle=_oracle_runs_result(submission),
        nop=_nop_stage_result(nop_run),
        agent_trials=_agent_trials_result(submission),
        sufficiency=_sufficiency_result(sufficiency_run),
        leakage_scan=_leakage_scan_result(leakage_scan_run),
        code_smell=_code_smell_result(code_smell_run),
        review_report=_review_report_result(review_report_run),
        failure_analysis=_failure_analysis_result(_get_run(submission, "failure_analysis")),
        cheat_trial=_cheat_trial_result(_get_run(submission, "cheat_trial")),
        rubric_check=_rubric_check_result(_get_run(submission, "rubric_check")),
        static_checks=_static_checks_result(_get_run(submission, "static_checks")),
        tb_static_checks=_tb_static_checks_result(_get_run(submission, "tb_static_checks")),
        ai_detection=_ai_detection_result(_get_run(submission, "ai_detection")),
        checksum=_checksum_info(submission),
    )


def to_list_item(submission: Submission) -> SubmissionListItem:
    nop_run = _get_run(submission, "nop")
    sufficiency_run = _get_run(submission, "sufficiency")
    leakage_scan_run = _get_run(submission, "leakage_scan")
    code_smell_run = _get_run(submission, "code_smell")
    review_report_run = _get_run(submission, "review_report")
    agent_trials = _agent_trials_result(submission)
    return SubmissionListItem(
        id=submission.id,
        task_name=submission.task_name,
        uploaded_at=submission.uploaded_at,
        build_status=submission.build_status,  # type: ignore[arg-type]
        oracle_status=_oracle_runs_result(submission).status,
        nop_status=_nop_stage_result(nop_run).status,
        agent_status=agent_trials.status,
        sufficiency_status=(sufficiency_run.status if sufficiency_run else "not-run"),  # type: ignore[arg-type]
        leakage_scan_status=(leakage_scan_run.status if leakage_scan_run else "not-run"),  # type: ignore[arg-type]
        code_smell_status=(code_smell_run.status if code_smell_run else "not-run"),  # type: ignore[arg-type]
        review_report_status=(review_report_run.status if review_report_run else "not-run"),  # type: ignore[arg-type]
    )


def _fmt_pct(x: float | None) -> str:
    return f"{x * 100:.1f}%" if x is not None else "not run yet"


def _fmt_status(status: str) -> str:
    return {"not-run": "not run yet", "pending": "queued", "running": "in progress"}.get(
        status, status
    )


def build_summary_markdown(schema: SubmissionSchema, total_cost_usd: float | None) -> str:
    """C8: a README/summary rendered straight from already-verified run data
    -- no LLM call, so every field is a real value or an explicit "not run
    yet" / "not recorded" -- never a [fill] placeholder ship. Used by
    routers.submissions.get_evidence_bundle. Extend this, not a separate
    template file, when a deliverable needs another field: every value here
    traces back to SubmissionSchema, so it can't drift from what the
    platform actually recorded."""
    c = schema.checksum
    if c.consistent is True:
        checksum_line = f"Consistent across all runs (`{c.canonical}`)."
    elif c.consistent is False:
        checksum_line = (
            f"**MISMATCH** -- {', '.join(c.mismatched)} ran against a different task version "
            f"than the rest of this submission's runs (canonical: `{c.canonical}`). Every result "
            "below may describe more than one task version; re-run before trusting this summary."
        )
    else:
        checksum_line = "Not enough Harbor-backed runs yet to compare."

    lines = [
        f"# {schema.task_name or 'Untitled task'} — Evaluation Summary",
        "",
        f"Submission: `{schema.id}`",
        f"Task checksum: {checksum_line}",
        "",
        "## Gates",
        f"- Build: {_fmt_status(schema.build.status)}",
        (
            f"- Oracle ({schema.oracle.n}x): {_fmt_status(schema.oracle.status)}"
            + (
                f", all {schema.oracle.n} runs passed"
                if schema.oracle.all_passed
                else ", not all runs passed" if schema.oracle.all_passed is False else ""
            )
        ),
        f"- Nop: {_fmt_status(schema.nop.status)}"
        + (f" (reward={schema.nop.reward})" if schema.nop.reward is not None else ""),
        f"- Sufficiency: {_fmt_status(schema.sufficiency.status)}",
        "",
        "## Agent Trials",
        f"- N: {schema.agent_trials.n}",
        f"- Pass rate: {_fmt_pct(schema.agent_trials.pass_rate)}",
    ]
    if schema.agent_trials.pass_at_k:
        pass_k_line = ", ".join(
            f"pass@{k}: {_fmt_pct(v)}"
            for k, v in sorted(schema.agent_trials.pass_at_k.items(), key=lambda kv: int(kv[0]))
        )
        lines.append(f"- {pass_k_line}")
    else:
        lines.append("- pass@k: not run yet")

    lines += [
        "",
        "## Advisory checks",
        f"- Leakage Scan: {_fmt_status(schema.leakage_scan.status)}",
        f"- Code Smell: {_fmt_status(schema.code_smell.status)}",
        (
            f"- Static Checks: {_fmt_status(schema.static_checks.status)}"
            + (
                f" ({json.loads(schema.static_checks.logs)['fail_count']} fail, "
                f"{json.loads(schema.static_checks.logs)['warn_count']} warn)"
                if schema.static_checks.logs
                else ""
            )
        ),
        (
            f"- TB Static Checks: {_fmt_status(schema.tb_static_checks.status)}"
            + (
                f" ({json.loads(schema.tb_static_checks.logs)['pass_count']} pass, "
                f"{json.loads(schema.tb_static_checks.logs)['fail_count']} fail)"
                if schema.tb_static_checks.logs
                else ""
            )
        ),
        f"- Rubric Check: {_fmt_status(schema.rubric_check.status)}",
        f"- Cheat Trial: {_fmt_status(schema.cheat_trial.status)}",
        f"- Failure Analysis: {_fmt_status(schema.failure_analysis.status)}",
        f"- Review Report: {_fmt_status(schema.review_report.status)}",
        "",
        "## Cost",
        f"- Total recorded LLM spend for this submission: "
        + (f"${total_cost_usd:.4f}" if total_cost_usd is not None else "not recorded"),
    ]
    return "\n".join(lines) + "\n"
