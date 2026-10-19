import json
import math
import re
from collections import Counter

from app.config import get_settings
from app.models import LlmSpend, Run, Submission
from app.services import verifier_report
from app.services.harbor_runner import CODEX_BAKE_NOTE_MARKER
from app.services.task_config import TaskConfig
from app.schemas import (
    AgentTrialsResult,
    AnswerExposureResult,
    BuildResult,
    ChecksumInfo,
    CheatTrialResult,
    CheatTrialRun,
    FreshBuildResult,
    ImageInventoryResult,
    FailureAnalysisResult,
    RubricCheckResult,
    VariantProbeResult,
    WeakProbeResult,
    EdgeCasesResult,
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


def _verifier(run: Run, expected_tests: int | None = None) -> dict | None:
    """The stored verifier report as a dict (None for older rows). With
    expected_tests, a report listing a different number of tests gets a flag
    (A.2), computed here so runs recorded before it existed get it too."""
    if not run.verifier_json:
        return None
    try:
        data = json.loads(run.verifier_json)
    except ValueError:
        return None
    flag = verifier_report.count_flag(data, expected_tests)
    if flag:
        data["flags"] = [*(data.get("flags") or []), flag]
    return data


def _expected_tests(submission: Submission) -> int | None:
    """The number of tests the passing Oracle runs reported (the most common one)."""
    counts: Counter = Counter()
    for r in submission.runs:
        if r.kind == "oracle" and r.reward == 1:
            ctrf = (_verifier(r) or {}).get("ctrf") or {}
            if ctrf.get("tests"):
                counts[ctrf["tests"]] += 1
    return counts.most_common(1)[0][0] if counts else None


def _trial(r: Run, expected_tests: int | None) -> TrialResult:
    return TrialResult(
        index=r.run_index, reward=r.reward, raw_reward=r.raw_reward, verifier=_verifier(r, expected_tests),
        logs=r.logs, task_checksum=r.task_checksum,
    )


def _stage_result(run: Run | None) -> StageResult:
    if run is None:
        return StageResult(status="not-run", reward=None, logs=None)
    return StageResult(
        status=run.status, reward=run.reward, raw_reward=run.raw_reward, verifier=_verifier(run),  # type: ignore[arg-type]
        logs=run.logs, task_checksum=run.task_checksum,
    )


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


# Static-check findings that make a task's tests sensitive to load or timing.
_FRAGILE_CHECKS = ("test_timing_assertions", "test_live_directory_scan")
_SUGGESTED_ORACLE_RUNS = 5


def _fragile_findings(submission: Submission) -> list[str]:
    run = _get_run(submission, "static_checks")
    try:
        results = json.loads(run.logs)["results"] if run and run.logs else []
    except (ValueError, KeyError, TypeError):
        return []
    return [r["name"] for r in results if r.get("name") in _FRAGILE_CHECKS and r.get("severity") == "warn"]


def _oracle_runs_result(submission: Submission) -> OracleRunsResult:
    oracle_runs = sorted(
        (r for r in submission.runs if r.kind == "oracle"), key=lambda r: r.run_index
    )
    stress = sorted((r for r in submission.runs if r.kind == "oracle_stress"), key=lambda r: r.run_index)
    expected = _expected_tests(submission)
    stress_runs = [_trial(r, expected) for r in stress]
    stress_note = _stress_note(oracle_runs, stress)
    n = len(oracle_runs)
    if n == 0:
        return OracleRunsResult(
            status="not-run", n=0, runs=[], all_passed=None, stress_runs=stress_runs, stress_note=stress_note
        )

    statuses = {r.status for r in oracle_runs}
    all_terminal = statuses <= _TERMINAL

    all_passed = None
    if all_terminal:
        all_passed = all(r.reward == 1 for r in oracle_runs)
        status: StageStatus = "passed" if all_passed else "failed"
    else:
        status = "running"

    # G.1: a mix of 1s and 0s means the solution or the verifier is not deterministic.
    scored = [r.reward for r in oracle_runs if r.status in _TERMINAL and r.reward is not None]
    flaky = None if not scored else (0 in scored and 1 in scored)
    flaky_note = None
    if flaky:
        flaky_note = (
            f"{scored.count(0)} of {len(scored)} Oracle runs scored 0 while the others scored 1: the reference "
            "solution or the verifier is flaky, and an agent trial can fail for the same reason. Run "
            f"{_SUGGESTED_ORACLE_RUNS} or more Oracle runs and a stress run before trusting the gate."
        )
    elif scored and len(scored) < _SUGGESTED_ORACLE_RUNS and _fragile_findings(submission):
        flaky_note = (
            f"Only {len(scored)} Oracle run(s), and Static Checks found tests that time things or scan folders "
            f"the solution writes to ({', '.join(_fragile_findings(submission))}): run at least "
            f"{_SUGGESTED_ORACLE_RUNS}, plus a stress run, since such tests fail only some of the time."
        )
    return OracleRunsResult(
        status=status, n=n, runs=[_trial(r, expected) for r in oracle_runs], all_passed=all_passed,
        flaky=flaky, flaky_note=flaky_note, stress_runs=stress_runs, stress_note=stress_note,
        expected_tests=expected,
    )


def _stress_note(oracle_runs: list[Run], stress: list[Run]) -> str | None:
    done = [r for r in stress if r.status in _TERMINAL and r.reward is not None]
    if not done:
        return None
    failed = sum(1 for r in done if r.reward == 0)
    if not failed:
        return f"All {len(done)} stress run(s) passed under CPU load."
    quiet_passes = any(r.reward == 1 for r in oracle_runs)
    return (
        f"{failed} of {len(done)} stress run(s) failed under CPU load"
        + (" while the normal Oracle runs passed: the tests or the solution depend on a quiet machine "
           "(a timing assertion, a race, a tight timeout)" if quiet_passes else "")
        + "."
    )


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

    expected = _expected_tests(submission)
    trials = [_trial(r, expected) for r in agent_runs]
    test_summary = None
    if all_terminal:
        test_summary = verifier_report.cluster_failures(
            [(r.run_index, r.raw_reward, _verifier(r)) for r in agent_runs]
        )
    return AgentTrialsResult(
        status=status, n=n, trials=trials, pass_rate=pass_rate, pass_at_k=pass_at_k,
        test_summary=test_summary,
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


def _cheat_outcome(run: Run) -> str | None:
    """One word for what a finished cheat trial showed."""
    if run.status not in ("passed", "failed"):
        return None
    try:
        data = json.loads(run.logs or "")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    check = (data.get("cheat_check") or {}).get("outcome")
    if run.reward == 1:
        return {"pass": "solved honestly", "fail": "reward hacking found"}.get(check, "unresolved, read by hand")
    if data.get("inconclusive"):
        return "inconclusive (agent did not try)"
    return "trap held" if run.status == "passed" else "failed to run"


def _cheat_trial_result(submission: Submission) -> CheatTrialResult:
    rows = sorted((r for r in submission.runs if r.kind == "cheat_trial"), key=lambda r: r.run_index)
    if not rows:
        return CheatTrialResult(status="not-run", reward=None, logs=None)
    expected = _expected_tests(submission)
    runs = [
        CheatTrialRun(
            index=r.run_index, status=r.status, reward=r.reward, raw_reward=r.raw_reward,  # type: ignore[arg-type]
            verifier=_verifier(r, expected), logs=r.logs, task_checksum=r.task_checksum,
        )
        for r in rows
    ]
    statuses = {r.status for r in rows}
    if statuses & {"pending", "running"}:
        status: StageStatus = "running"
    else:
        status = "failed" if "failed" in statuses else "passed"
    counts = Counter(o for o in (_cheat_outcome(r) for r in rows) if o)
    summary = (
        f"{len(rows)} trial(s): " + ", ".join(f"{c} {o}" for o, c in counts.most_common()) if counts else None
    )
    first = runs[0]
    return CheatTrialResult(
        status=status, reward=first.reward, raw_reward=first.raw_reward, verifier=first.verifier,
        logs=first.logs, task_checksum=first.task_checksum, n=len(rows), runs=runs, summary=summary,
    )


def _checksum_info(submission: Submission) -> ChecksumInfo:
    """C1: did Build/Oracle/Nop/Agent Trials actually run against the same
    task version? Compares every Harbor-backed run's own task_checksum
    (oracle, nop, agent -- never cheat_trial, whose task copy always has an
    intentionally edited instruction.md) against the submission's canonical
    checksum (the first one any run reported). `consistent` stays null
    until at least one comparable run has reported a checksum."""
    canonical = submission.task_checksum
    mismatched: list[str] = []
    not_comparable: list[str] = []
    seen_any = False
    for r in submission.runs:
        if r.kind not in ("oracle", "oracle_stress", "nop", "agent") or not r.task_checksum:
            continue
        label = r.kind if r.kind == "nop" else f"{r.kind}#{r.run_index}"
        # A trial with Codex baked into its task copy ran a Dockerfile the
        # platform itself had extended (and date-stamped), so Harbor's checksum
        # differs from the other stages by design, not because the task changed.
        if CODEX_BAKE_NOTE_MARKER in (r.logs or ""):
            not_comparable.append(label)
            continue
        seen_any = True
        if canonical and r.task_checksum != canonical:
            mismatched.append(label)
    return ChecksumInfo(
        canonical=canonical,
        consistent=(len(mismatched) == 0) if seen_any else None,
        mismatched=mismatched,
        not_comparable=not_comparable,
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


def _variant_probe_result(run: Run | None) -> VariantProbeResult:
    if run is None:
        return VariantProbeResult(status="not-run", logs=None)
    return VariantProbeResult(status=run.status, logs=run.logs)  # type: ignore[arg-type]


def _weak_probe_result(run: Run | None) -> WeakProbeResult:
    if run is None:
        return WeakProbeResult(status="not-run", logs=None)
    return WeakProbeResult(status=run.status, logs=run.logs)  # type: ignore[arg-type]


def _edge_cases_result(run: Run | None) -> EdgeCasesResult:
    if run is None:
        return EdgeCasesResult(status="not-run", logs=None)
    return EdgeCasesResult(status=run.status, logs=run.logs)  # type: ignore[arg-type]


def _answer_exposure_result(run: Run | None) -> AnswerExposureResult:
    if run is None:
        return AnswerExposureResult(status="not-run", logs=None)
    return AnswerExposureResult(status=run.status, logs=run.logs)  # type: ignore[arg-type]


def _image_inventory_result(run: Run | None) -> ImageInventoryResult:
    if run is None:
        return ImageInventoryResult(status="not-run", logs=None)
    return ImageInventoryResult(status=run.status, logs=run.logs)  # type: ignore[arg-type]


def _fresh_build_result(run: Run | None) -> FreshBuildResult:
    if run is None:
        return FreshBuildResult(status="not-run", logs=None)
    return FreshBuildResult(status=run.status, logs=run.logs)  # type: ignore[arg-type]


def _reviewer_decisions(run: Run | None) -> dict:
    if run is None or not run.logs:
        return {}
    try:
        data = json.loads(run.logs)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _rubric_check_result(run: Run | None) -> RubricCheckResult:
    if run is None:
        return RubricCheckResult(status="not-run", logs=None)
    return RubricCheckResult(status=run.status, logs=run.logs)  # type: ignore[arg-type]


def _review_report_result(run: Run | None) -> ReviewReportResult:
    if run is None:
        return ReviewReportResult(status="not-run", passed=None, logs=None)
    return ReviewReportResult(status=run.status, passed=(run.reward == 1) if run.reward is not None else None, logs=run.logs)  # type: ignore[arg-type]


def to_schema(submission: Submission) -> SubmissionSchema:
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
        oracle=_oracle_runs_result(submission),
        nop=_nop_stage_result(nop_run),
        agent_trials=_agent_trials_result(submission),
        sufficiency=_sufficiency_result(sufficiency_run),
        leakage_scan=_leakage_scan_result(leakage_scan_run),
        review_report=_review_report_result(review_report_run),
        failure_analysis=_failure_analysis_result(_get_run(submission, "failure_analysis")),
        cheat_trial=_cheat_trial_result(submission),
        rubric_check=_rubric_check_result(_get_run(submission, "rubric_check")),
        variant_probe=_variant_probe_result(_get_run(submission, "variant_probe")),
        weak_probe=_weak_probe_result(_get_run(submission, "weak_probe")),
        edge_cases=_edge_cases_result(_get_run(submission, "edge_cases")),
        reviewer_decisions=_reviewer_decisions(_get_run(submission, "reviewer_decision")),
        static_checks=_static_checks_result(_get_run(submission, "static_checks")),
        tb_static_checks=_tb_static_checks_result(_get_run(submission, "tb_static_checks")),
        ai_detection=_ai_detection_result(_get_run(submission, "ai_detection")),
        answer_exposure=_answer_exposure_result(_get_run(submission, "answer_exposure")),
        image_inventory=_image_inventory_result(_get_run(submission, "image_inventory")),
        fresh_build=_fresh_build_result(_get_run(submission, "fresh_build")),
        checksum=_checksum_info(submission),
    )


def to_list_item(submission: Submission) -> SubmissionListItem:
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
        oracle_status=_oracle_runs_result(submission).status,
        nop_status=_nop_stage_result(nop_run).status,
        agent_status=agent_trials.status,
        sufficiency_status=(sufficiency_run.status if sufficiency_run else "not-run"),  # type: ignore[arg-type]
        leakage_scan_status=(leakage_scan_run.status if leakage_scan_run else "not-run"),  # type: ignore[arg-type]
        review_report_status=(review_report_run.status if review_report_run else "not-run"),  # type: ignore[arg-type]
    )


def _fmt_pct(x: float | None) -> str:
    return f"{x * 100:.1f}%" if x is not None else "not run yet"


def _fmt_status(status: str) -> str:
    return {"not-run": "not run yet", "pending": "queued", "running": "in progress"}.get(
        status, status
    )


def build_cost_report(rows: list[LlmSpend]) -> dict:
    """Every recorded spend row for a submission, plus totals grouped by stage
    and by model and by stage+model. Fields a row was recorded without (older
    rows, or evaluators where Harbor reports only a cost) are null, never
    guessed. Costs are USD; GPTZero is converted from its INR per-word price
    at the configured rate and carries its word count in unit/unit_count."""
    ordered = sorted(rows, key=lambda r: (r.created_at, r.id))

    def _group(field_names: tuple, key) -> list[dict]:
        groups: dict[tuple, dict] = {}
        for r in ordered:
            g = groups.setdefault(
                key(r),
                {"calls": 0, "cost_usd": 0.0, "input_tokens": 0, "cached_tokens": 0,
                 "cache_write_tokens": 0, "output_tokens": 0, "tokens_missing_for": 0},
            )
            g["calls"] += 1
            g["cost_usd"] += r.cost_usd
            if r.n_input_tokens is None and r.n_output_tokens is None:
                g["tokens_missing_for"] += 1
            g["input_tokens"] += r.n_input_tokens or 0
            g["cached_tokens"] += r.n_cache_tokens or 0
            g["cache_write_tokens"] += r.n_cache_write_tokens or 0
            g["output_tokens"] += r.n_output_tokens or 0
        return [
            {**dict(zip(field_names, k)), **g, "cost_usd": round(g["cost_usd"], 6)}
            for k, g in groups.items()
        ]

    by_stage = _group(("stage",), lambda r: (r.stage,))
    by_model = _group(("model",), lambda r: (r.model,))
    by_stage_model = _group(("stage", "model"), lambda r: (r.stage, r.model))

    return {
        "currency": "USD",
        "total_usd": round(sum(r.cost_usd for r in ordered), 6),
        "n_records": len(ordered),
        "by_stage": by_stage,
        "by_model": by_model,
        "by_stage_and_model": by_stage_model,
        "records": [
            {
                "recorded_at": r.created_at.isoformat(),
                "stage": r.stage,
                "detail": r.detail,
                "trial": r.trial_index,
                "model": r.model,
                "agent_or_harness": r.agent,
                "reasoning_effort": r.reasoning_effort,
                "input_tokens": r.n_input_tokens,
                "cached_tokens": r.n_cache_tokens,
                "cache_write_tokens": r.n_cache_write_tokens,
                "output_tokens": r.n_output_tokens,
                "unit": r.unit,
                "unit_count": r.unit_count,
                "cost_usd": round(r.cost_usd, 6),
            }
            for r in ordered
        ],
        "notes": [
            "Agent trials, the cheat trial and its check, Failure Analysis and Rubric Check "
            "report cost from Harbor; evaluator runs report no token counts, so those are null.",
            "Judge calls (Sufficiency, Review Report) are priced from token usage; a model not "
            "in the price table is listed with cost 0 and flagged in `detail`.",
            "A trial that crashed before Harbor reported a cost has no record, so it is absent.",
            "Rows recorded before this report existed have null agent/effort/trial/detail.",
        ],
    }


def _verifier_lines(schema: SubmissionSchema) -> list[str]:
    """Per-run flags from the verifier's own report (see verifier_report.py)
    and the failing-test summary, as plain bullets. Empty when nothing was
    recorded, so a submission from before this existed shows no section."""
    labelled: list[tuple[str, object]] = [(f"Oracle run {r.index + 1}", r) for r in schema.oracle.runs]
    labelled += [(f"Stress Oracle run {r.index + 1}", r) for r in schema.oracle.stress_runs]
    labelled.append(("Nop", schema.nop))
    labelled += [(f"Agent trial {t.index + 1}", t) for t in schema.agent_trials.trials]
    labelled += [(f"Cheat trial {t.index + 1}", t) for t in schema.cheat_trial.runs]
    lines: list[str] = []
    for label, r in labelled:
        v = getattr(r, "verifier", None)
        if v is None:
            continue
        for flag in v.flags:
            lines.append(f"- {label}: {flag}")
        if v.details:
            lines.append(f"- {label}: score details in `{v.details.file}`")
    ts = schema.agent_trials.test_summary
    if ts:
        c = ts.closest
        lines.append(f"- Closest failing trial: trial {c.trial + 1} passed {c.passed} of {c.tests} tests")
        for t in ts.failing_tests[:10]:
            lines.append(f"- Test `{t.name}` failed in {t.failed_in} of {ts.failing_trials} failing trials")
        lines += [f"- NOTE: {n}" for n in ts.notes]
    return lines


def _json_field(logs: str | None, key: str):
    try:
        return json.loads(logs or "").get(key)
    except (ValueError, AttributeError):
        return None


# Terminal-Bench 4.0's flat agent timeout.
POLICY_AGENT_TIMEOUT_SEC = 28800


def run_limits(submission: Submission, schema: SubmissionSchema) -> dict:
    """4.0 E.1: the limits trials ran with, as applied (AGENT_TIMEOUT_SEC
    overrides the task's own timeout), and how the latest recorded trial
    actually ran, with a warning when the agent timeout is below the 8 h policy."""
    settings = get_settings()
    config = TaskConfig.model_validate_json(submission.task_config_json) if submission.task_config_json else TaskConfig()
    applied = settings.agent_timeout_sec or config.agent.timeout_sec
    recorded = next(
        (
            t.verifier.facts
            for t in [*reversed(schema.agent_trials.trials), *reversed(schema.oracle.runs)]
            if t.verifier and t.verifier.facts
        ),
        None,
    )
    warnings = []
    if applied < POLICY_AGENT_TIMEOUT_SEC:
        warnings.append(
            f"agent timeout {applied:g}s is below Terminal-Bench 4.0's flat {POLICY_AGENT_TIMEOUT_SEC}s (8 h)"
            + ("" if settings.agent_timeout_sec else "; AGENT_TIMEOUT_SEC is not set, so the task's own value is used")
        )
    return {
        "agent_timeout_sec": applied,
        "agent_timeout_source": "AGENT_TIMEOUT_SEC" if settings.agent_timeout_sec else "task.toml",
        "verifier_timeout_sec": config.verifier.timeout_sec,
        "cpus": config.environment.cpus,
        "memory_mb": config.environment.memory_mb,
        "docker_platform": settings.docker_platform,
        "latest_recorded_trial": recorded,
        "warnings": warnings,
    }


def verifier_reports(schema: SubmissionSchema) -> list[dict]:
    """4.1 A.1/A.3: every run's per-test report, score-details file and flags,
    for the evidence download."""
    labelled: list[tuple[str, object]] = [(f"oracle#{r.index}", r) for r in schema.oracle.runs]
    labelled += [(f"oracle_stress#{r.index}", r) for r in schema.oracle.stress_runs]
    labelled.append(("nop", schema.nop))
    labelled += [(f"agent#{t.index}", t) for t in schema.agent_trials.trials]
    labelled += [(f"cheat_trial#{t.index}", t) for t in schema.cheat_trial.runs]
    return [
        {"run": label, "reward": r.reward, **r.verifier.model_dump()}  # type: ignore[attr-defined]
        for label, r in labelled
        if getattr(r, "verifier", None) is not None
    ]


def build_summary_markdown(
    schema: SubmissionSchema, total_cost_usd: float | None, cost_report: dict | None = None
) -> str:
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
        *([f"  - FLAKY: {schema.oracle.flaky_note}"] if schema.oracle.flaky else
          [f"  - NOTE: {schema.oracle.flaky_note}"] if schema.oracle.flaky_note else []),
        *([f"  - Under CPU load: {schema.oracle.stress_note}"] if schema.oracle.stress_note else []),
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

    verifier_lines = _verifier_lines(schema)
    if verifier_lines:
        lines += ["", "## Verifier reports (per-test results behind the rewards)", *verifier_lines]

    lines += [
        "",
        "## Advisory checks",
        f"- Leakage Scan: {_fmt_status(schema.leakage_scan.status)}",
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
        f"- Cheat Trial: {_fmt_status(schema.cheat_trial.status)}"
        + (f" ({schema.cheat_trial.summary})" if schema.cheat_trial.summary else ""),
        f"- Answer Exposure: {_fmt_status(schema.answer_exposure.status)}"
        + (f" ({_json_field(schema.answer_exposure.logs, 'warn_count')} warning(s))" if schema.answer_exposure.logs else ""),
        f"- Fresh build (no cache, Oracle): {_fmt_status(schema.fresh_build.status)}",
        f"- Failure Analysis: {_fmt_status(schema.failure_analysis.status)}",
        f"- Review Report: {_fmt_status(schema.review_report.status)}",
        "",
        "## Cost",
        f"- Total recorded LLM spend for this submission: "
        + (f"${total_cost_usd:.4f}" if total_cost_usd is not None else "not recorded"),
    ]
    if cost_report and cost_report["records"]:
        lines += ["", "| Stage | Model | Calls | Cost (USD) |", "|---|---|---|---|"]
        for g in sorted(cost_report["by_stage_and_model"], key=lambda g: g["stage"]):
            lines.append(f"| {g['stage']} | {g['model']} | {g['calls']} | ${g['cost_usd']:.4f} |")
        lines.append("- Full per-call breakdown: see `cost` in the evidence JSON.")
    return "\n".join(lines) + "\n"
