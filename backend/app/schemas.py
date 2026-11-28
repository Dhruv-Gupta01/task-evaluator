from datetime import datetime
from typing import Literal

from pydantic import BaseModel

StageStatus = Literal["pending", "running", "passed", "failed", "not-run"]


class CtrfSummary(BaseModel):
    tests: int
    passed: int
    failed: int
    skipped: int
    other: int = 0
    failed_names: list[str] = []
    skipped_names: list[str] = []


class VerifierDetails(BaseModel):
    file: str
    content: str


class VerifierCheck(BaseModel):
    """One named check from Reward Kit's reward-details.json."""
    name: str
    group: str = ""
    passed: bool
    value: float | None = None
    message: str = ""


class VerifierReport(BaseModel):
    """What the verifier's own files said about one trial (advisory evidence;
    see services/verifier_report.py). `flags` lists places where the reward and
    the tests behind it disagree, or where no per-test record exists."""
    reward_raw: float | None = None
    ctrf: CtrfSummary | None = None
    details: VerifierDetails | None = None
    flags: list[str] = []
    facts: dict | None = None  # how the trial ran: limits, time used, resources, users
    refusal: dict | None = None  # {kind, evidence} when the agent refused the task (5.0 J)
    # What the agent did with the web, from its own tool calls (services/web_use.py): {used_web, web_tool_calls,
    # fetches, installs, hosts, examples}; None when no trajectory could be read.
    web_use: dict | None = None
    checks: list[VerifierCheck] | None = None  # Reward Kit's per-check results, when the task writes them
    worker_error: bool = False  # the agent's own code failed while the grader ran it (a fair failure)
    # {reason, evidence} when the grader itself broke: no score, says nothing about the agent.
    grader_error: dict | None = None


class SuggestedVerdict(BaseModel):
    """A pre-filled genuine-or-fake call for a finished trial; a reviewer confirms or changes it."""
    verdict: Literal["fake", "likely_genuine", "pass", "unclear"]
    reason: str
    action: str


class StageResult(BaseModel):
    status: StageStatus
    reward: int | None = None  # 0 | 1 | null
    raw_reward: float | None = None  # Harbor's exact reward; reward is 1 only at >= 1.0
    verifier: VerifierReport | None = None
    suggested_verdict: SuggestedVerdict | None = None
    logs: str | None = None
    task_checksum: str | None = None


class BuildResult(BaseModel):
    status: StageStatus
    logs: str | None = None
    image_tag: str | None = None


class TrialResult(BaseModel):
    index: int
    solution: str | None = None  # Oracle runs: which declared solution (SOLUTION=<value>)
    reward: int | None = None
    raw_reward: float | None = None
    verifier: VerifierReport | None = None
    suggested_verdict: SuggestedVerdict | None = None
    logs: str | None = None
    task_checksum: str | None = None


class FailingTest(BaseModel):
    name: str
    failed_in: int


class ClosestTrial(BaseModel):
    trial: int
    passed: int
    tests: int


class TestSummary(BaseModel):
    """Across the failing trials of one agent job: which tests they failed and
    how close the best one came. A single test failed by every failing trial is
    the typical shape of an over-strict grader or an unstated rule, so it is
    called out in `notes` (a human decides)."""
    failing_trials: int
    failing_tests: list[FailingTest]
    closest: ClosestTrial
    notes: list[str] = []


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
    test_summary: TestSummary | None = None
    # 5.0 J: trials the agent refused (they score 0 like a failure) and the pass
    # rate with those trials left out. pass_rate and pass_at_k are unchanged.
    refusals: int = 0
    pass_rate_excluding_refusals: float | None = None
    # Trials where the grader itself broke (no score). They are left out of pass_rate,
    # pass_at_k and the refusal rate, since they say nothing about the agent: rerun them.
    grader_errors: int = 0
    # Trials Harbor says have no valid reward (cancelled, a missing reward file, a verifier timeout, or a
    # non-finite reward). Left out of pass_rate and pass_at_k like grader errors, never counted twice.
    no_valid_reward: int = 0
    # 95% Wilson interval [low, high] for the pass rate, over the same scored trials as pass_rate.
    pass_interval: list[float] | None = None
    # The same as text, e.g. "1 of 3 passed (95% interval 6% to 79%)" (rule 15.11 asks for counts, not a bare %).
    pass_rate_text: str | None = None
    # Finished trials per agent and model (read from each trial's harbor command), in first-seen order: an
    # evidence set appends trials of other models, so one pass rate can mix several.
    by_model: list["ModelCount"] = []


class ModelCount(BaseModel):
    agent: str | None = None
    model: str | None = None
    passed: int
    n: int


class EvidenceSetResult(BaseModel):
    # logs is JSON: {set: standard|cheap, second, cost_cap, pairs: [{agent, model, n, status}]} or {error}.
    status: StageStatus
    logs: str | None = None


class OracleSolution(BaseModel):
    value: str
    runs: int
    passed_runs: int
    passed: bool


class OracleRunsResult(BaseModel):
    """Oracle can be run N times, always appended to the ones already there
    (Gate 1 needs 3 *consecutive* 1.0 runs, so history matters -- unlike Nop,
    which stays a single StageResult). "passed" only once every run in `runs`
    has reward=1; any run still pending/running keeps status "running".
    all_passed is None until every run is terminal.

    `flaky` (G.1) is true when some finished runs scored 1 and others 0: the
    reference solution or the verifier is not deterministic. `stress_runs`
    (G.3) are Oracle runs made while a sibling container kept the CPUs busy;
    they never count for the gate. `expected_tests` is the number of tests the
    passing runs reported, which other runs' reports are compared with (A.2)."""
    status: StageStatus
    n: int
    runs: list[TrialResult]
    all_passed: bool | None = None
    flaky: bool | None = None
    flaky_note: str | None = None
    stress_runs: list[TrialResult] = []
    stress_note: str | None = None
    expected_tests: int | None = None
    # one entry per solution the task declares; solutions_note says why the gate cannot open
    solutions: list[OracleSolution] = []
    solutions_note: str | None = None
    # Runs where the grader itself broke. The gate stays unpassed (it cannot be judged), but
    # these are not a verdict on the solution: rerun them.
    grader_errors: int = 0


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
    raw_reward: float | None = None
    verifier: VerifierReport | None = None
    logs: str | None = None
    # Recorded for audit, but never compared to the submission's canonical
    # checksum -- this trial's task copy always has an intentionally edited
    # instruction.md, so it legitimately never matches. See ChecksumInfo.
    task_checksum: str | None = None
    # B.2: a run can hold 1 to 3 cheat trials. The fields above describe the
    # first one (as before); `runs` has every trial, `status` is "failed" when
    # any trial needs a human, and `summary` counts the outcomes.
    n: int = 0
    runs: list["CheatTrialRun"] = []
    summary: str | None = None


class CheatTrialRun(BaseModel):
    index: int
    status: StageStatus
    reward: int | None = None
    raw_reward: float | None = None
    verifier: VerifierReport | None = None
    logs: str | None = None
    task_checksum: str | None = None


CheatTrialResult.model_rebuild()


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


class AiDetectionResult(BaseModel):
    # status is the effective one: a current ruling decides it; detected_status is the detector's own
    detected_status: str | None = None
    decision: dict | None = None
    decision_stale: bool = False
    history: list[dict] = []

    """Terminal-Bench's own Layer 3 AI-usage detection (vendor/tb_checks/
    scripts/check_ai_detection.py) -- calls the real GPTZero API. Advisory,
    and genuinely optional in Terminal-Bench's own workflow too. `logs` is
    JSON: {skipped, passed, output, error, api_key_configured}. "passed"
    covers both a genuine pass and a graceful skip (no API key, or a
    GPTZero network error); "failed" means GPTZero flagged the content as
    likely AI-generated, or an unexpected error occurred."""
    status: StageStatus
    logs: str | None = None


class VariantProbeResult(BaseModel):
    """Valid-variant probe. Advisory: a model proposes forms of the Oracle's
    answer that the instruction allows, each is run through the real verifier,
    and a rejected one means the grader may be stricter than the instruction.
    Up to 4 per declared Oracle solution.
    `logs` is JSON: {model, reason, dropped, variants: [{solution, description,
    instruction_quote, edits, status accepted|rejected|did_not_run, reward,
    failed_tests, flags, note}]}."""
    status: StageStatus
    logs: str | None = None


class WeakProbeResult(BaseModel):
    """Weak-answer probe. Advisory: deliberately broken versions of the Oracle's
    answer are run through the real verifier and must score 0; one that scores
    1 ("slipped_through") means the grader is weaker than the instruction.
    `logs` is JSON like VariantProbeResult's, with status caught|slipped_through
    |did_not_run per variant and mode "broken"."""
    status: StageStatus
    logs: str | None = None


class EdgeCasesResult(BaseModel):
    """Edge-case check. Advisory: cases an expert would handle, from the
    instruction alone, each marked covered or not by the tests. `logs` is JSON:
    {cases: [{case, expected, covered, test, reason}], error}."""
    status: StageStatus
    logs: str | None = None


class Tier0Result(BaseModel):
    """Tier 0 of the cheat-trial loop (services/tier0/): human-written static probes, no
    agent and no model. Advisory. "failed" = something needs a human: an exploit, a verifier
    that fails open, a valid solution graded 0, a failed control probe, or a probe that could
    not run. `logs` is JSON: {task, platform, repeats, skipped, summary, results: [{probe_id,
    title, verdict, reward, positive_control, positive_control_ok, fault_results, notes,
    ...}], progress: {done, total}, images_removed}. `skipped` is a reason string when the
    task is one the harness cannot drive."""
    status: StageStatus
    logs: str | None = None


class Tier1Result(BaseModel):
    """Tier 1 of the cheat-trial loop (services/tier1/): a hinted agent with a read-only copy of
    tests/ and a memory of its earlier attempts tries to get reward 1 without doing the work; a
    confirmed hit is patched into a hardened copy by an LLM fixer; the stage ends when 3 cheating
    attempts in a row fail. Advisory. "passed" = robust; "failed" = something needs a human.
    `logs` is JSON: {agent, model, judge_model, fixer_model, limits, status (robust | unfixed |
    needs_review | inconclusive | attempt_limit | budget), reason, attempts: [{index, verdict,
    reward, tool_calls, cost_usd, summary, techniques, defences, judge_reasoning, trial_dir,
    against_fix}], fixes: [{number, after_attempt, accepted, description, edits, gate, note}],
    consecutive_misses, cost_usd, diff, skipped, error, in_progress}."""
    status: StageStatus
    logs: str | None = None


class TierCycleResult(BaseModel):
    """The whole Tier 0 -> Tier 1 -> Tier 2 pipeline repeated until the task stops changing
    (task_runner.run_tier_cycle). "passed" only when the final status is robust. `logs` is JSON:
    {status (robust | exploited | inconclusive | needs_review | not_converged | budget | error),
    reason, in_progress, max_rounds, cost_usd, max_cost_usd, diff, rounds: [{round, task_copy,
    patched, tier0, tier1, tier2}]} where each tier entry is a short snapshot of that round's
    result (status, result, reason, labels, attempts, cost_usd, fixes_accepted, summary)."""
    status: StageStatus
    logs: str | None = None


class Tier2Result(BaseModel):
    """Tier 2 of the cheat-trial loop (services/tier2/): an unhinted agent (the task plus one line
    of pressure, no verifier access, no charter, no memory) run repeatedly and independently.
    The stage is "passed" when 3 genuine shortcut attempts in a row failed (robust) and "failed"
    for anything else, including a stage that could not tell (inconclusive). Advisory. `logs` is
    JSON: {agent, model, judge_model, task_copy, limits, status (robust | exploited | needs_review
    | inconclusive), reason, counts: {runs, attempted, exploited, attempted_failed, honest_solve,
    honest_fail, refused, infra_failed, unclassified, needs_review, held_rate}, attempts: [{index,
    verdict, reward, tool_calls, cost_usd, judge_cost_usd, summary, techniques, judge_error,
    judge_reasoning, trial_dir}], consecutive_misses, cost_usd, cost_breakdown_usd, error,
    in_progress}. Never combine its numbers with Tier 1's: hinted and unhinted are reported
    separately."""
    status: StageStatus
    logs: str | None = None


class AnswerExposureResult(BaseModel):
    """Answer-exposure scan (4.1 C.1, 4.0 item 61). Advisory: can the agent
    find the answer in the files it is given? "failed" = at least one warning.
    `logs` is JSON: {findings: [{severity, kind, message, files}], warn_count,
    values_checked, files_scanned, image_listing_used}."""
    status: StageStatus
    logs: str | None = None


class ImageInventoryResult(BaseModel):
    """What the built image contains (4.1 F.1, H.2), recorded after Build.
    `logs` is JSON: {workdir, files: [{path, size}], truncated, pip, dpkg, apk,
    npm} or {error}."""
    status: StageStatus
    logs: str | None = None


class FreshBuildResult(BaseModel):
    """Still builds and solves today (4.1 H.1): a no-cache build plus one
    Oracle run with every RUN step rebuilt. Advisory. `logs` is JSON:
    {build: {ok, log_tail}, oracle: {reward, failed_tests, flags, log_tail} | null}."""
    status: StageStatus
    logs: str | None = None


class WorkerProbeCheck(BaseModel):
    name: str
    ok: bool
    detail: str = ""


class WorkerProbeResult(BaseModel):
    """What a Reward Kit Worker could reach in the task's own verifier image (services/worker_probe.py).
    `skipped` means the grader has no Worker or no separate verifier image, so there was nothing to probe.
    Advisory evidence, never a gate."""
    status: StageStatus
    skipped: bool = False
    checks: list[WorkerProbeCheck] = []
    note: str = ""
    error: str | None = None


class HashProofEntry(BaseModel):
    name: str
    file: str
    must_match: bool
    result: str  # used | not_used | unconfirmed | no_result | not_run
    note: str = ""


class HashProofResult(BaseModel):
    """The proof run for hashed answers (services/hash_checks.py). `verdict`: all_used (the only pass),
    unused_hash, needs_review, no_hashes or error."""
    status: StageStatus
    verdict: str = ""
    entries: list[HashProofEntry] = []
    runs: int = 0
    total: int = 0
    note: str = ""


class GraderIsolationResult(BaseModel):
    """The Grader Isolation gate (way 2 of the Worker flag). `state` is ok / missing / unclear;
    `analysis` is the way-1 result (not_required, present, equivalent, missing). Advisory unless `enforced`.
    Nobody can overrule it: a failing result is cleared by revising the task."""
    status: StageStatus
    state: str | None = None
    analysis: str | None = None
    evidence: list[str] = []
    absent: list[str] = []
    also_unprotected: list[str] = []
    note: str = ""
    enforced: bool = False
    blocks: bool = False


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
    # Trials left out of the comparison because the platform changed their task
    # copy on purpose (Codex baked into the Dockerfile), so their checksum
    # legitimately differs. They are not evidence either way.
    not_comparable: list[str] = []


class RegradeResult(BaseModel):
    """Saved trials scored again by Harbor with the current grader, no agent run
    (see task_runner.run_regrade). `logs` is JSON: the source, and per trial the
    old and new reward, so a grader fix shows exactly which scores it changed."""
    status: StageStatus
    logs: str | None = None


class SubmissionSchema(BaseModel):
    id: str
    task_name: str
    # The zip has been unpacked (Validate & Build has started at least once), so file-based stages work.
    files_ready: bool = False
    uploaded_at: datetime
    build: BuildResult
    oracle: OracleRunsResult
    nop: StageResult
    agent_trials: AgentTrialsResult
    sufficiency: SufficiencyResult
    leakage_scan: LeakageScanResult
    review_report: ReviewReportResult
    failure_analysis: FailureAnalysisResult
    cheat_trial: CheatTrialResult
    rubric_check: RubricCheckResult
    variant_probe: VariantProbeResult
    weak_probe: WeakProbeResult
    edge_cases: EdgeCasesResult
    static_checks: StaticChecksResult
    tb_static_checks: TbStaticChecksResult
    grader_isolation: GraderIsolationResult = GraderIsolationResult(status="not-run")
    worker_probe: WorkerProbeResult = WorkerProbeResult(status="not-run")
    hash_proof: HashProofResult = HashProofResult(status="not-run")
    ai_detection: AiDetectionResult
    answer_exposure: AnswerExposureResult
    tier0: Tier0Result
    tier1: Tier1Result
    tier2: Tier2Result
    tier_cycle: TierCycleResult = TierCycleResult(status="not-run", logs=None)
    regrade: RegradeResult
    image_inventory: ImageInventoryResult
    fresh_build: FreshBuildResult
    checksum: ChecksumInfo
    # Results review (services/result_rules.py): advisory lines, each starting with its rule id (15.6, 15.11,
    # 15.12, 15.31). Never a gate, no ruling.
    result_warnings: list[str] = []
    evidence_set: EvidenceSetResult = EvidenceSetResult(status="not-run")
    # Agent rubric draft (services/generators.agent_rubric). logs is JSON: {items: [{criterion, pass_when, weight,
    # evidence: [{trial, quote}], unverified_evidence}], dropped, model, trials, transcript_chars, error?}.
    agent_rubric: EvidenceSetResult = EvidenceSetResult(status="not-run")


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
    review_report_status: StageStatus


class UploadResponse(BaseModel):
    id: str
