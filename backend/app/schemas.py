from datetime import datetime
from typing import Literal

from pydantic import BaseModel

StageStatus = Literal["pending", "running", "passed", "failed", "not-run"]


class StageResult(BaseModel):
    status: StageStatus
    reward: int | None = None  # 0 | 1 | null
    logs: str | None = None
    task_checksum: str | None = None


class BuildResult(BaseModel):
    status: StageStatus
    logs: str | None = None
    image_tag: str | None = None


class TrialResult(BaseModel):
    index: int
    reward: int | None = None
    logs: str | None = None
    task_checksum: str | None = None


class AgentTrialsResult(BaseModel):
    status: StageStatus
    n: int
    trials: list[TrialResult]
    pass_rate: float | None = None
    # C6: pass@k (the standard unbiased estimator) for every k from 1 to n,
    # keyed by k as a string. Unlike Harbor's own job-level reporting (which
    # only fills powers-of-2/multiples-of-5 -- pass@1 is never computed by
    # Harbor itself), every k is always present here once all trials are
    # terminal; null while any trial is still running/pending.
    pass_at_k: dict[str, float] | None = None


class OracleRunsResult(BaseModel):
    """Oracle can be run N times, always appended to the ones already there
    (Gate 1 needs 3 *consecutive* 1.0 runs, so history matters -- unlike Nop,
    which stays a single StageResult). "passed" only once every run in `runs`
    has reward=1; any run still pending/running keeps status "running".
    all_passed is None until every run is terminal."""
    status: StageStatus
    n: int
    runs: list[TrialResult]
    all_passed: bool | None = None


class SufficiencyResult(BaseModel):
    status: StageStatus
    passed: bool | None = None
    logs: str | None = None


class LeakageScanResult(BaseModel):
    """Advisory only — "passed": false means leakage artifacts were found,
    NOT that the submission is disqualified. Never treat this as a gate."""
    status: StageStatus
    passed: bool | None = None
    logs: str | None = None


class CodeSmellResult(BaseModel):
    """A model's judgment call, not a fact — unlike LeakageScanResult, false
    positives are expected and accepted. "passed": false means the judge
    thinks the code likely reads as AI-generated; treat as a lower-confidence
    signal worth a look, never as proof, and never merge with the leakage
    scan's verified findings."""
    status: StageStatus
    passed: bool | None = None
    logs: str | None = None


class ReviewReportResult(BaseModel):
    """"passed" means the report generated successfully — it is NOT a
    judgment on the submission. The actual verdict is markdown text in
    `logs`, synthesized from the already-completed platform report plus
    static checks. Advisory / draft-for-human-review, same as Sufficiency
    and Leakage Scan."""
    status: StageStatus
    passed: bool | None = None
    logs: str | None = None


class FailureAnalysisResult(BaseModel):
    """`harbor analyze` over the agent trials. Advisory: "passed" only means
    the analysis ran. `logs` is JSON: {agent, model, cost_usd, error, results},
    one entry per trial with a summary and rubric checks (reward_hacking,
    task_specification), each {outcome, explanation}."""
    status: StageStatus
    logs: str | None = None


class CheatTrialResult(BaseModel):
    """One trial run with an explicit directive to cheat spliced into the
    instruction. "passed" means the trap held: reward stayed 0/None, OR
    reward=1 but an automatic `harbor analyze` reward_hacking check (run
    whenever reward=1, since that alone can't tell a real cheat apart from
    the agent refusing and solving it honestly -- both observed live) found
    no reward hacking. "failed" means either a real finding (the check found
    reward hacking) or the check itself couldn't resolve it -- always surfaced
    for a human to read, never silently assumed clean either way. `logs` is
    JSON: {trial_log, reward, cheat_check: {outcome, explanation, cost_usd,
    error} | null}."""
    status: StageStatus
    reward: int | None = None
    logs: str | None = None
    # Recorded for audit, but never compared to the submission's canonical
    # checksum -- this trial's task copy always has an intentionally edited
    # instruction.md, so it legitimately never matches. See ChecksumInfo.
    task_checksum: str | None = None


class StaticChecksResult(BaseModel):
    """Mechanical, LLM-free checks (services/static_checks.py) over
    instruction.md, test.sh, Dockerfile and zip layout. Advisory: "failed"
    means at least one check came back `fail` (findings for a human to look
    at), not that the submission is disqualified. `logs` is JSON:
    {fail_count, warn_count, results: [{name, severity, message}], text}."""
    status: StageStatus
    logs: str | None = None


class TbStaticChecksResult(BaseModel):
    """Terminal-Bench's own official static checks (vendor/tb_checks/,
    maintained upstream, not by us) -- a different, mostly non-overlapping
    rule set from StaticChecksResult. Advisory. `logs` is JSON: {pass_count,
    fail_count, results: [{name, passed, output}], text}."""
    status: StageStatus
    logs: str | None = None


class RubricCheckResult(BaseModel):
    """`harbor check`: an evaluator agent scores the whole task against
    Harbor's quality rubric (not the agent trials -- see FailureAnalysisResult
    for that). Advisory. `logs` is JSON: {agent, model, cost_usd, error,
    results}, one entry per task with `checks` (per-criterion {outcome,
    explanation}) and a `summary`. A large environment/ (e.g. vendored
    dependencies) can make Harbor's own launcher fail with an OS
    argument-length error; that shows up in `error`, not as a real quality
    verdict."""
    status: StageStatus
    logs: str | None = None


class ChecksumInfo(BaseModel):
    """C1: verifies Build/Oracle/Nop/Agent Trials all ran against the same
    frozen task version instead of assuming it. `canonical` is the first
    Harbor-reported task_checksum seen for this submission (set once, never
    changed). `consistent` is null until at least one Harbor-backed run has
    reported a checksum; false means some run's own checksum (see each
    stage's `task_checksum` field) differs from canonical -- e.g. a
    submission stitched together from runs against different task copies.
    `mismatched` names those runs (e.g. "oracle#1", "agent#0"). The Cheat
    Trial is deliberately excluded: its task copy always has an edited
    instruction.md, so it never matches canonical by design -- see
    CheatTrialResult.task_checksum instead."""
    canonical: str | None = None
    consistent: bool | None = None
    mismatched: list[str] = []


class SubmissionSchema(BaseModel):
    id: str
    task_name: str
    uploaded_at: datetime
    build: BuildResult
    oracle: OracleRunsResult
    nop: StageResult
    agent_trials: AgentTrialsResult
    sufficiency: SufficiencyResult
    leakage_scan: LeakageScanResult
    code_smell: CodeSmellResult
    review_report: ReviewReportResult
    failure_analysis: FailureAnalysisResult
    cheat_trial: CheatTrialResult
    rubric_check: RubricCheckResult
    static_checks: StaticChecksResult
    tb_static_checks: TbStaticChecksResult
    checksum: ChecksumInfo


class SubmissionListItem(BaseModel):
    id: str
    task_name: str
    uploaded_at: datetime
    build_status: StageStatus
    oracle_status: StageStatus
    nop_status: StageStatus
    agent_status: StageStatus
    sufficiency_status: StageStatus
    leakage_scan_status: StageStatus
    code_smell_status: StageStatus
    review_report_status: StageStatus


class UploadResponse(BaseModel):
    id: str
