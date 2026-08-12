from app.models import Run, Submission
from app.schemas import (
    AgentTrialsResult,
    BuildResult,
    LeakageScanResult,
    ReviewReportResult,
    StageResult,
    StageStatus,
    SubmissionListItem,
    SubmissionSchema,
    SufficiencyResult,
    TrialResult,
)

_TERMINAL = {"passed", "failed", "not-run"}


def _get_run(submission: Submission, kind: str, run_index: int = 0) -> Run | None:
    for r in submission.runs:
        if r.kind == kind and r.run_index == run_index:
            return r
    return None


def _stage_result(run: Run | None) -> StageResult:
    if run is None:
        return StageResult(status="not-run", reward=None, logs=None)
    return StageResult(status=run.status, reward=run.reward, logs=run.logs)  # type: ignore[arg-type]


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
    return result.model_copy(update={"status": _invert_status(result.status)})


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
    if all_terminal:
        passed = sum(1 for r in agent_runs if r.reward == 1)
        pass_rate = passed / n

    trials = [TrialResult(index=r.run_index, reward=r.reward, logs=r.logs) for r in agent_runs]
    return AgentTrialsResult(status=status, n=n, trials=trials, pass_rate=pass_rate)


def _sufficiency_result(run: Run | None) -> SufficiencyResult:
    if run is None:
        return SufficiencyResult(status="not-run", passed=None, logs=None)
    return SufficiencyResult(status=run.status, passed=(run.reward == 1) if run.reward is not None else None, logs=run.logs)  # type: ignore[arg-type]


def _leakage_scan_result(run: Run | None) -> LeakageScanResult:
    if run is None:
        return LeakageScanResult(status="not-run", passed=None, logs=None)
    return LeakageScanResult(status=run.status, passed=(run.reward == 1) if run.reward is not None else None, logs=run.logs)  # type: ignore[arg-type]


def _review_report_result(run: Run | None) -> ReviewReportResult:
    if run is None:
        return ReviewReportResult(status="not-run", passed=None, logs=None)
    return ReviewReportResult(status=run.status, passed=(run.reward == 1) if run.reward is not None else None, logs=run.logs)  # type: ignore[arg-type]


def to_schema(submission: Submission) -> SubmissionSchema:
    oracle_run = _get_run(submission, "oracle")
    nop_run = _get_run(submission, "nop")
    sufficiency_run = _get_run(submission, "sufficiency")
    leakage_scan_run = _get_run(submission, "leakage_scan")
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
        oracle=_stage_result(oracle_run),
        nop=_nop_stage_result(nop_run),
        agent_trials=_agent_trials_result(submission),
        sufficiency=_sufficiency_result(sufficiency_run),
        leakage_scan=_leakage_scan_result(leakage_scan_run),
        review_report=_review_report_result(review_report_run),
    )


def to_list_item(submission: Submission) -> SubmissionListItem:
    oracle_run = _get_run(submission, "oracle")
    nop_run = _get_run(submission, "nop")
    sufficiency_run = _get_run(submission, "sufficiency")
    leakage_scan_run = _get_run(submission, "leakage_scan")
    review_report_run = _get_run(submission, "review_report")
    agent_trials = _agent_trials_result(submission)
    return SubmissionListItem(
        id=submission.id,
        task_name=submission.task_name,
        uploaded_at=submission.uploaded_at,
        build_status=submission.build_status,  # type: ignore[arg-type]
        oracle_status=(oracle_run.status if oracle_run else "not-run"),  # type: ignore[arg-type]
        nop_status=_nop_stage_result(nop_run).status,
        agent_status=agent_trials.status,
        sufficiency_status=(sufficiency_run.status if sufficiency_run else "not-run"),  # type: ignore[arg-type]
        leakage_scan_status=(leakage_scan_run.status if leakage_scan_run else "not-run"),  # type: ignore[arg-type]
        review_report_status=(review_report_run.status if review_report_run else "not-run"),  # type: ignore[arg-type]
    )
