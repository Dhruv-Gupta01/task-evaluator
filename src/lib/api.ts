export const API_BASE_URL =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ||
  "http://localhost:8000";

export type StageStatus = "pending" | "running" | "passed" | "failed" | "not-run";

// What the verifier's own files said about one trial (advisory evidence, see
// backend services/verifier_report.py). `flags` lists places where the reward
// and the tests behind it disagree, or where no per-test record exists.
export interface CtrfSummary {
  tests: number;
  passed: number;
  failed: number;
  skipped: number;
  other: number;
  failed_names: string[];
  skipped_names: string[];
}

// One named check from Reward Kit's reward-details.json.
export interface VerifierCheck {
  name: string;
  group: string;
  passed: boolean;
  value: number | null;
  message: string;
}

// A pre-filled genuine-or-fake call for a finished trial. `fake` = the grader (not the
// agent) broke; `likely_genuine` = the agent's own work failed named checks. A reviewer
// confirms or changes it (see TrialVerdictRuling); it never changes a gate or a pass rate.
export interface SuggestedVerdict {
  verdict: "fake" | "likely_genuine" | "pass" | "unclear";
  reason: string;
  action: string;
}

export interface VerifierReport {
  reward_raw: number | null;
  ctrf: CtrfSummary | null;
  details: { file: string; content: string } | null;
  flags: string[];
  // Reward Kit's per-check results, when the task's grader writes reward-details.json.
  checks?: VerifierCheck[] | null;
  // The agent's own code failed while the grader ran it (a fair failure, not a grader fault).
  worker_error?: boolean;
  // The grader itself broke (Worker setup failed, or no usable reward file): no score, and it
  // says nothing about the agent. Such trials are left out of pass rates; rerun them.
  grader_error?: {
    reason: "worker_setup_error" | "no_reward_file" | string;
    evidence: string;
  } | null;
  // Set when the agent refused the task instead of attempting it (5.0 J).
  refusal?: { kind: "provider_block" | "phrase"; evidence: string } | null;
  // How the trial ran (E.1/E.3/E.4/E.8): limits, time used, resources, users.
  facts?: {
    agent_seconds: number | null;
    agent_limit_sec: number;
    verifier_seconds: number | null;
    verifier_limit_sec: number;
    cpus: number | null;
    memory_mb: number | null;
    peak_memory_mb?: number | null;
    platform: string | null;
    host_arch: string;
    agent_user: string | null;
    // Real uid of the agent process, read from the live container (0 = root).
    agent_uid?: number | null;
    verifier_user: string | null;
    verifier_mode: string | null;
  } | null;
}

export interface StageResult {
  status: StageStatus;
  reward: 0 | 1 | null;
  // Harbor's exact reward; `reward` is 1 only at >= 1.0, so 0.73 shows here.
  raw_reward?: number | null;
  verifier?: VerifierReport | null;
  suggested_verdict?: SuggestedVerdict | null;
  logs?: string;
  task_checksum?: string | null;
}

export interface BuildResult {
  status: StageStatus;
  logs?: string;
  image_tag?: string | null;
}

export interface TrialResult {
  solution?: string | null;
  index: number;
  reward: 0 | 1 | null;
  raw_reward?: number | null;
  verifier?: VerifierReport | null;
  suggested_verdict?: SuggestedVerdict | null;
  logs?: string;
  task_checksum?: string | null;
}

// C1: did Build/Oracle/Nop/Agent Trials all run against the same frozen
// task version? `canonical` is the first task_checksum any Harbor-backed
// run reported. `consistent: false` means some run's own task_checksum
// (see each stage's TrialResult/StageResult) differs -- `mismatched` names
// which ones (e.g. "oracle#1", "agent#0"). The Cheat Trial is deliberately
// excluded -- its task copy always has an edited instruction.md, so its
// checksum never matches canonical by design. `consistent: null` means too
// few comparable runs have finished yet.
export interface ChecksumInfo {
  canonical: string | null;
  consistent: boolean | null;
  mismatched: string[];
  // Trials whose task copy the platform extended on purpose (Codex baked into the
  // image); excluded from the comparison above.
  not_comparable?: string[];
}

export interface AgentTrialsResult {
  status: StageStatus;
  n: number;
  trials: TrialResult[];
  pass_rate: number | null;
  // C6: pass@k (standard unbiased estimator) for every k from 1 to n, keyed
  // by k as a string. Harbor's own job-level reporting only ever fills
  // powers-of-2/multiples-of-5 -- pass@1 is never computed by Harbor itself.
  pass_at_k: Record<string, number> | null;
  // A refusal scores 0 like a failure. These count them; pass_rate stays as is.
  refusals: number;
  pass_rate_excluding_refusals: number | null;
  // Trials where the grader itself broke (no score). Left out of pass_rate, pass_at_k and the
  // refusal rate; rerun them.
  grader_errors?: number;
  // Across the failing trials: which tests they failed and how close the best
  // one came. A single test failed by every failing trial is the typical
  // shape of an over-strict grader or an unstated rule (`notes`).
  test_summary?: {
    failing_trials: number;
    failing_tests: { name: string; failed_in: number }[];
    closest: { trial: number; passed: number; tests: number };
    notes: string[];
  } | null;
}

// Oracle can be run N times, always appended to the ones already there (Gate
// 1 needs 3 *consecutive* 1.0 runs). "passed" only once every run passed.
export interface OracleRunsResult {
  solutions?: { value: string; runs: number; passed_runs: number; passed: boolean }[];
  solutions_note?: string | null;
  status: StageStatus;
  n: number;
  runs: TrialResult[];
  all_passed: boolean | null;
  // Some finished runs scored 1 and others 0: the solution or verifier is flaky.
  flaky?: boolean | null;
  flaky_note?: string | null;
  // Runs made while a sibling container kept the CPUs busy; never part of the gate.
  stress_runs?: TrialResult[];
  stress_note?: string | null;
  // Tests the passing runs reported; other runs' reports are compared with it.
  expected_tests?: number | null;
  // Runs where the grader itself broke. The gate stays unpassed, but these say nothing about
  // the solution: rerun them.
  grader_errors?: number;
}

export interface SufficiencyResult {
  status: StageStatus;
  passed: boolean | null;
  logs?: string;
}

// Advisory only — passed: false means leakage artifacts were found, NOT
// that the submission is disqualified. Never treat this as a pass/fail gate.
export interface LeakageScanResult {
  status: StageStatus;
  passed: boolean | null;
  logs?: string;
}

// "passed" means the report generated successfully — NOT a judgment on the
// submission. The actual verdict is markdown text in `logs`.
export interface ReviewReportResult {
  status: StageStatus;
  passed: boolean | null;
  logs?: string;
}

// `logs` is JSON (see FailureAnalysis below) once the stage has run.
export interface FailureAnalysisResult {
  status: StageStatus;
  logs?: string;
}

export interface AnalysisCheck {
  outcome: "pass" | "fail" | "not_applicable" | string;
  explanation: string;
}

export interface TrialAnalysis {
  trial_name?: string | null;
  summary?: string | null;
  checks: Record<string, AnalysisCheck>;
  cost_usd?: number | null;
  error?: string | null;
}

export interface FailureAnalysis {
  agent?: string;
  model?: string;
  cost_usd?: number | null;
  error?: string | null;
  results: TrialAnalysis[];
  // "default" (Harbor's own 2-criterion rubric) or "tb" (vendored 6-criterion).
  rubric?: string;
  // Only present when rubric="tb" -- Harbor has no built-in job-summary
  // step, so this is a separate synthesis over all trial results.
  job_summary?: string | null;
  // Why each failed trial failed (a reasoning bug, an instruction gap or
  // infrastructure) and trials that chose the same alternative reading.
  // Passes that may rest on a guess, and the best failing trial's misses ruled
  // agent error / documentation gap / grader error.
  audit?: {
    passing?: { trial: string; assumptions: { test: string; assumption: string }[] }[];
    best_failed?: {
      trial: string;
      misses: { test: string; ruling: string; reason: string; times?: number }[];
    } | null;
    examples?: string[];
  } | null;
  classification?: {
    trials: { trial: string; class: string; reason: string }[];
    clusters?: { reading: string; trials: string[]; label: string }[];
  } | null;
}

// One trial run with an explicit cheat directive. `logs` is JSON once the
// stage has run (see CheatTrial below); reward=1 alone doesn't mean a real
// cheat -- see cheat_check.
export interface CheatTrialResult {
  status: StageStatus;
  reward: 0 | 1 | null;
  raw_reward?: number | null;
  verifier?: VerifierReport | null;
  logs?: string;
  // Recorded for audit only -- never compared against ChecksumInfo.canonical.
  // This trial's task copy always has an intentionally edited instruction.md.
  task_checksum?: string | null;
  // A run can hold 1 to 3 trials; the fields above describe the first one.
  n?: number;
  runs?: CheatTrialRun[];
  summary?: string | null;
}

export interface CheatTrialRun {
  index: number;
  status: StageStatus;
  reward: 0 | 1 | null;
  raw_reward?: number | null;
  verifier?: VerifierReport | null;
  logs?: string;
  task_checksum?: string | null;
}

export interface CheatCheck {
  outcome: "pass" | "fail" | "unknown" | string;
  explanation?: string | null;
  cost_usd?: number | null;
  error?: string | null;
}

export interface CheatTrial {
  trial_log?: string | null;
  reward: 0 | 1 | null;
  cheat_check: CheatCheck | null;
  // "default" (this platform's own directive), "tb" (vendored red-team prompt)
  // or "combined" (a real partial solution plus a shortcut).
  cheat_prompt?: string;
  // Why a reward below 1 does not show the trap held: the agent never really
  // tried (few tool calls) or the trial produced no reward. null = it tried.
  inconclusive?: string | null;
  tool_calls?: number | null;
}

// Weak-answer probe and edge-case check (advisory).
export interface WeakProbeResult {
  status: StageStatus;
  logs?: string;
}
export interface EdgeCasesResult {
  status: StageStatus;
  logs?: string;
}
export interface EdgeCaseProbe {
  // "slipped_through": a solution that mishandles the case still scored 1.
  status: "caught" | "slipped_through" | "did_not_run";
  reward?: number | null;
  failed_tests?: string[];
  flags?: string[];
  note?: string;
  description?: string;
  solution?: string | null; // the Oracle solution this result came from
}
export interface EdgeCases {
  cases: {
    case: string;
    expected: string;
    covered: boolean;
    test: string | null;
    reason: string;
    // false: the reference solution itself does not handle this case.
    reference_handles?: boolean | null;
    probe?: EdgeCaseProbe; // the deciding result: any slip, else any catch
    probes?: EdgeCaseProbe[]; // one per Oracle solution
    // The judge said "covered", but a wrong answer still scored 1 through the grader.
    covered_claim_wrong?: boolean;
  }[];
  spec_files?: string[];
  error?: string;
  probed?: boolean;
  probe_total?: number;
  probe_done?: number;
  in_progress?: boolean;
}

// Tier 0 (advisory): human-written static probes, no agent and no model. "failed" means
// something needs a human: an exploit, a verifier that fails open, a valid solution graded 0,
// a task that relies on Harbor, a failed control, a broken probe suite, a replay that
// disagrees, or a probe that could not run.
export interface Tier0Result {
  status: StageStatus;
  logs?: string;
}
export type Tier0Verdict =
  | "held"
  | "exploited"
  | "fail_open"
  | "false_zero"
  | "relies_on_harbor"
  | "valid_accepted"
  | "control_ok"
  | "control_failed"
  | "flaky"
  | "inconclusive"
  | "infra_failed"
  | "skipped"
  | string;
export interface Tier0ProbeResult {
  probe_id: string;
  flaw_class: string;
  title: string;
  verdict: Tier0Verdict;
  reward: number | null;
  positive_control: string | null;
  positive_control_reward: number | null;
  positive_control_ok: boolean | null;
  fault_results: { mutant: string; reward: number | null; outcome: string }[];
  unsanitized?: {
    reward: number | null;
    verdict: string;
    timed_out: boolean;
    arrived: boolean;
  } | null;
  needs_harbor: boolean;
  notes: string[];
  duration_sec: number | null;
}
export interface Tier0Replay {
  key: string;
  kind: string;
  source: "this upload" | "earlier upload" | string;
  recorded: number | null;
  replayed: number | null;
  verdict: string;
  notes: string[];
}
export interface Tier0Comparison {
  against: string;
  available: boolean;
  verifier_changed?: boolean;
  unchanged_probes?: number;
  changes: { probe_id: string; before: string | null; after: string | null; kind: string }[];
}// Tier 1 (advisory): a hinted agent with a read-only copy of tests/ and notes on its earlier
// attempts tries to get reward 1 without doing the work; a judge labels each attempt, an LLM
// fixer patches a hardened copy after a hit, and the loop ends after 3 failed cheating attempts
// in a row. "passed" = robust; "failed" = something needs a human.
export interface Tier1Result {
  status: StageStatus;
  logs?: string;
}
export type Tier1Verdict =
  | "exploited"
  | "attempted_failed"
  | "honest_solve"
  | "honest_fail"
  | "unclassified"
  | "refused"
  | "not_attempted"
  | "infra_failed"
  | "needs_review";
export interface Tier1Attempt {
  index: number;
  verdict: Tier1Verdict;
  reward: number | null;
  tool_calls: number | null;
  cost_usd: number | null;
  judge_cost_usd?: number | null;
  judge_error?: string | null;
  summary: string;
  techniques: string[];
  defences: string[];
  judge_reasoning?: string | null;
  judge_confidence?: string | null;
  trial_dir?: string | null;
  note?: string | null;
  against_fix: number;
  fix_failed?: string;
}
export interface Tier1Fix {
  number: number;
  after_attempt: number;
  accepted: boolean;
  description: string;
  edits: { file: string; find: string; replace: string }[];
  gate?: string;
  note?: string;
}
// Saved trials scored again with the current grader, no agent run. `logs` is JSON (RegradeReport).
export interface RegradeResult {
  status: StageStatus;
  logs?: string;
}
export interface RegradeItem {
  trial: string;
  job: string;
  old_reward: number | null;
  new_reward: number | null;
  change: "same" | "loosened" | "tightened" | "now_scored" | "now_no_score";
  old_grader_error: boolean;
  new_grader_error: { reason: string; evidence: string } | null;
  failed_checks: string[];
  flags: string[];
}
export interface RegradeReport {
  kind?: "agent" | "oracle";
  source?: { submission: string | null; same_upload: boolean };
  items: RegradeItem[];
  summary?: {
    total: number;
    same: number;
    loosened: number;
    tightened: number;
    now_scored: number;
    now_no_score: number;
    grader_errors_before: number;
    grader_errors_after: number;
  };
  errors: string[];
  error?: string;
  harbor_version?: string;
}

export interface VersionInfo {
  harbor: {
    version: string;
    tested_with: string;
    min_for_reward_kit_worker: string;
    worker_supported: boolean | null;
    lint_available: boolean;
    regrade_available: boolean;
  };
  reward_kit: { pinned_version: string };
  notes: string[];
}
export interface Tier1 {
  agent?: string;
  model?: string;
  judge_model?: string;
  fixer_model?: string;
  limits?: { max_attempts?: number; max_fixes?: number; max_cost_usd: number };
  status?:
    | "robust"
    | "patched"
    | "unfixed"
    | "needs_review"
    | "unpatchable"
    | "inconclusive"
    | "attempt_limit"
    | "budget"
    | null;
  reason?: string | null;
  in_progress?: boolean;
  attempts: Tier1Attempt[];
  fixes: Tier1Fix[];
  consecutive_misses?: number;
  cost_usd?: number;
  cost_breakdown_usd?: { hacker: number; judge: number; fixer: number };
  diff?: string;
  skipped?: string;
  error?: string;
}
// Tier 2 (advisory): an unhinted agent (the task plus one line of pressure, no verifier access,
// no cheat charter, no memory) run repeatedly and independently. Stops "robust" after 3 genuine
// failed shortcut attempts in a row, or "exploited" at the first shortcut that gets reward 1.
// Honest solves and failures are neutral, so many runs end "inconclusive". "passed" = robust.
// The whole Tier 0 -> 1 -> 2 pipeline repeated until the task stops changing (POST /tiers).
export interface TierCycleStage {
  status: string;
  result?: string | null;
  reason?: string | null;
  error?: string | null;
  skipped?: string | null;
  attempts?: number;
  labels?: Record<string, number>;
  cost_usd?: number;
  fixes_accepted?: number;
  summary?: Record<string, number | null>;
}
export interface TierCycleRound {
  round: number;
  task_copy: string;
  patched: number;
  tier0: TierCycleStage | null;
  tier1: TierCycleStage | null;
  tier2: TierCycleStage | null;
}
export interface TierCycleReport {
  status:
    | "robust"
    | "exploited"
    | "inconclusive"
    | "needs_review"
    | "not_converged"
    | "budget"
    | "error"
    | null;
  reason: string | null;
  in_progress: boolean;
  max_rounds: number | null;
  cost_usd: number;
  max_cost_usd: number;
  diff: string;
  error?: string;
  rounds: TierCycleRound[];
}
export interface TierCycleResult {
  status: StageStatus;
  logs?: string;
}
// Keep these numbers apart from Tier 1's: hinted and unhinted are never combined.
export interface Tier2Result {
  status: StageStatus;
  logs?: string;
}
export type Tier2Verdict =
  | "exploited"
  | "attempted_failed"
  | "honest_solve"
  | "honest_fail"
  | "refused"
  | "infra_failed"
  | "unclassified"
  | "needs_review";
export interface Tier2Attempt {
  index: number;
  verdict: Tier2Verdict;
  reward: number | null;
  tool_calls: number | null;
  cost_usd: number | null;
  judge_cost_usd?: number | null;
  judge_error?: string | null;
  judge_label?: string | null;
  summary: string;
  techniques: string[];
  judge_reasoning?: string | null;
  judge_confidence?: string | null;
  trial_dir?: string | null;
  note?: string | null;
}
export interface Tier2Counts {
  runs: number;
  attempted: number;
  hits?: number;
  misses?: number;
  exploited: number;
  attempted_failed: number;
  honest_solve: number;
  honest_fail: number;
  refused: number;
  infra_failed: number;
  unclassified: number;
  needs_review: number;
  held_rate: number | null;
}
export interface Tier2 {
  agent?: string;
  model?: string;
  judge_model?: string;
  task_copy?: string;
  pressure_line?: string;
  limits?: { max_attempts?: number; max_cost_usd: number };
  status?: "robust" | "exploited" | "needs_review" | "inconclusive" | null;
  reason?: string | null;
  in_progress?: boolean;
  attempts: Tier2Attempt[];
  counts?: Tier2Counts;
  consecutive_misses?: number;
  cost_usd?: number;
  cost_breakdown_usd?: { hacker: number; judge: number };
  skipped?: string;
  error?: string;
}
export interface Tier0 {
  task: string | null;
  platform: string;
  repeats: number;
  skipped: string | null;
  summary: {
    probes_run: number;
    held: number;
    exploited: number;
    fail_open: number;
    false_zero: number;
    relies_on_harbor?: number;
    // null = no control probe ran; false = the task's own solution did not score 1 here
    control_ok: boolean | null;
    held_but_positive_control_failed: string[];
    flaky_ids: string[];
    needs_harbor_to_settle: string[];
    replay_disagreements?: number;
    loosened?: number;
    regressed?: number;
  } | null;
  results: Tier0ProbeResult[];
  progress?: { done: number; total: number };
  images_removed?: number;
  selftest?: string[];
  replay?: Tier0Replay[];
  comparison?: Tier0Comparison | null;
  verifier_fingerprint?: string;
  error?: string;
}

// Answer-exposure scan (advisory): can the agent find the answer in its files?
export interface AnswerExposureResult {
  status: StageStatus;
  logs?: string;
}
export interface AnswerExposure {
  findings: {
    severity: "warn" | "info" | string;
    kind: string;
    message: string;
    files: string[];
  }[];
  warn_count: number;
  values_checked: number;
  files_scanned: number;
  image_listing_used: boolean;
  error?: string;
}

// What the built image contains, recorded after Build.
export interface ImageInventoryResult {
  status: StageStatus;
  logs?: string;
}
export interface ImageInventory {
  workdir?: string | null;
  files?: { path: string; size: number }[];
  truncated?: boolean;
  pip?: string[];
  dpkg?: string[];
  apk?: string[];
  npm?: string[];
  // 5.0 E: the image's own CPU architecture, OS and glibc, and what the registry says about each base image.
  system?: { arch?: string; os?: string; glibc?: string };
  base_images?: {
    ref: string;
    index?: boolean;
    platforms?: string[];
    warnings?: string[];
    error?: string;
  }[];
  error?: string;
}

// Still builds and solves today: no-cache build plus one Oracle run.
export interface FreshBuildResult {
  status: StageStatus;
  logs?: string;
}
export interface FreshBuild {
  build: { ok: boolean; log_tail: string } | null;
  oracle: {
    reward: number | null;
    failed_tests: string[];
    flags: string[];
    log_tail: string;
  } | null;
  error?: string;
}

// Valid-variant probe (advisory). `logs` is JSON (VariantProbe) once run.
export interface VariantProbeResult {
  status: StageStatus;
  logs?: string;
}
export interface VariantEntry {
  solution?: string | null; // the Oracle solution (SOLUTION value) this edit ran against
  description: string;
  instruction_quote: string;
  status: "accepted" | "rejected" | "caught" | "slipped_through" | "did_not_run";
  reward: number | null;
  failed_tests: string[];
  flags: string[];
  note: string;
}
export interface VariantProbe {
  model?: string;
  mode?: "valid" | "broken";
  reason?: string;
  error?: string;
  in_progress?: boolean;
  dropped?: { description: string; reason: string; solution?: string | null }[];
  variants: VariantEntry[];
}

// What changed since an earlier upload of the same task.
export interface SubmissionChanges {
  against: string | null;
  against_uploaded_at?: string;
  added?: string[];
  removed?: string[];
  changed?: string[];
  counts?: { added: number; removed: number; changed: number };
  tests_removed?: string[];
  tests_added?: string[];
  tests_removed_count?: number;
  tests_added_count?: number;
  instruction_words?: { before: number | null; after: number | null };
}

// `logs` is JSON (see RubricCheck below) once the stage has run.
export interface RubricCheckResult {
  status: StageStatus;
  logs?: string;
}

export interface RubricCheckEntry {
  task_name?: string | null;
  summary?: string | null;
  checks: Record<string, AnalysisCheck>;
  cost_usd?: number | null;
  error?: string | null;
}

export interface RubricCheck {
  agent?: string;
  model?: string;
  cost_usd?: number | null;
  error?: string | null;
  results: RubricCheckEntry[];
  // "default" (Harbor's own 11-criterion rubric) or "tb" (vendored 35-criterion).
  rubric?: string;
}

// `logs` is JSON (see StaticCheckReport below) once the stage has run.
export interface StaticChecksResult {
  status: StageStatus;
  logs?: string;
}

export interface StaticCheckFinding {
  name: string;
  severity: "fail" | "warn" | "info" | string;
  message: string;
}

export interface StaticCheckReport {
  fail_count: number;
  warn_count: number;
  results: StaticCheckFinding[];
  text: string;
}

// `logs` is JSON (see TbStaticCheckReport below) once the stage has run.
// A separate, mostly non-overlapping rule set from StaticChecksResult above
// -- Terminal-Bench's own official checks, not this platform's own.
export interface TbStaticChecksResult {
  status: StageStatus;
  logs?: string;
}

export interface TbCheckFinding {
  name: string;
  passed: boolean;
  output: string;
  // The check could not run here (a tool it needs is missing); not a failure.
  skipped?: boolean;
}

export interface TbStaticCheckReport {
  pass_count: number;
  fail_count: number;
  skip_count?: number;
  // Which upstream commit the vendored rule set comes from (5.0 F).
  vendor?: { commit: string | null; date: string | null; scripts: number };
  results: TbCheckFinding[];
  text: string;
}

// `logs` is JSON (see AiDetectionReport below) once the stage has run.
// Terminal-Bench's own Layer 3 AI-usage detection -- calls the real
// GPTZero API. Genuinely optional in Terminal-Bench's own workflow too.
export interface AiDetectionResult {
  // status is the effective one: a current ruling decides it
  detected_status?: string | null;
  decision?: { ruling: string; adjudicator: string; reason: string; at: string } | null;
  decision_stale?: boolean;
  status: StageStatus;
  logs?: string;
}

export interface AiDetectionReport {
  skipped: boolean | null;
  passed: boolean | null;
  output: string | null;
  error: string | null;
  api_key_configured: boolean;
}

// Reasoning levels the backend accepts for one run of trials.
export const REASONING_EFFORTS = ["low", "medium", "high", "xhigh", "max"] as const;
export type ReasoningEffort = (typeof REASONING_EFFORTS)[number];

// "tb" swaps in the vendored, richer Terminal-Bench rubric/prompt
// (backend/vendor/tb_prompts/) instead of Harbor's own default.
export const RUBRIC_CHOICES = [
  { value: "default", label: "Default" },
  { value: "tb", label: "TB (richer)" },
] as const;
export type RubricChoice = (typeof RUBRIC_CHOICES)[number]["value"];

export const CHEAT_PROMPT_CHOICES = [
  { value: "default", label: "Default" },
  { value: "tb", label: "TB (red-team)" },
  { value: "combined", label: "Partial solve + shortcut" },
  { value: "targeted", label: "Targeted (this task's rules)" },
] as const;
export type CheatPromptChoice = (typeof CHEAT_PROMPT_CHOICES)[number]["value"];

// Agents a run of trials can use; the backend picks each one's model from .env
// (codex: AGENT_MODEL, claude-code: CLAUDE_AGENT_MODEL). The model shown next
// to each name comes from GET /agents, never hard-coded, so it can't go stale.
export const AGENT_CHOICES = [
  { value: "codex", label: "Codex" },
  { value: "claude-code", label: "Claude Code" },
] as const;

/** "Codex · gpt-6-luna" from the backend's LiteLLM model string ("openai/gpt-6-luna"). */
export function agentLabel(
  choice: { value: string; label: string },
  models?: Record<string, string>,
): string {
  const model = models?.[choice.value]?.split("/").pop();
  return model ? `${choice.label} · ${model}` : choice.label;
}
export type AgentChoice = (typeof AGENT_CHOICES)[number]["value"];

// Grader Isolation gate (way 2 of the Worker flag): does agent-written code run in the verifier
// without a Worker or equivalent isolation? Advisory unless `enforced`.
export interface GraderIsolationResult {
  status: StageStatus;
  state: "ok" | "missing" | "unclear" | null;
  analysis: "not_required" | "present" | "equivalent" | "missing" | null;
  evidence: string[];
  absent: string[];
  also_unprotected: string[];
  note: string;
  enforced: boolean;
  blocks: boolean;
}

// Worker probe (NR-10): what a Reward Kit Worker could reach in the task's own verifier image.
// `skipped` = no Worker in the grader or no separate verifier image. Advisory evidence, never a gate.
export interface WorkerProbeCheck {
  name: string;
  ok: boolean;
  detail: string;
}
export interface WorkerProbeResult {
  status: StageStatus;
  skipped: boolean;
  checks: WorkerProbeCheck[];
  note: string;
  error: string | null;
}

// Hash proof (docs/hash-rules.md): damage one stored digest at a time and run the reference solution.
export interface HashProofEntry {
  name: string;
  file: string;
  must_match: boolean;
  result: "used" | "not_used" | "unconfirmed" | "no_result" | "not_run";
  note: string;
}
export interface HashProofResult {
  status: StageStatus;
  verdict: string;
  entries: HashProofEntry[];
  runs: number;
  total: number;
  note: string;
}

export interface Submission {
  id: string;
  task_name: string;
  uploaded_at: string;
  build: BuildResult;
  oracle: OracleRunsResult;
  nop: StageResult;
  agent_trials: AgentTrialsResult;
  sufficiency: SufficiencyResult;
  leakage_scan: LeakageScanResult;
  review_report: ReviewReportResult;
  failure_analysis: FailureAnalysisResult;
  cheat_trial: CheatTrialResult;
  rubric_check: RubricCheckResult;
  variant_probe: VariantProbeResult;
  weak_probe: WeakProbeResult;
  edge_cases: EdgeCasesResult;
  static_checks: StaticChecksResult;
  tb_static_checks: TbStaticChecksResult;
  grader_isolation: GraderIsolationResult;
  worker_probe: WorkerProbeResult;
  hash_proof: HashProofResult;
  ai_detection: AiDetectionResult;
  answer_exposure: AnswerExposureResult;
  tier0: Tier0Result;
  tier1: Tier1Result;
  tier2: Tier2Result;
  tier_cycle?: TierCycleResult;
  // A reviewer's genuine-or-fake call per trial, key "<stage>:<index>" (oracle | nop | agent).
  regrade?: RegradeResult;
  image_inventory: ImageInventoryResult;
  fresh_build: FreshBuildResult;
  checksum: ChecksumInfo;
}

export interface SubmissionListItem {
  id: string;
  task_name: string;
  uploaded_at: string;
  build_status: StageStatus;
  oracle_status: StageStatus;
  nop_status: StageStatus;
  agent_status: StageStatus;
  sufficiency_status: StageStatus;
  leakage_scan_status: StageStatus;
  review_report_status: StageStatus;
}

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}${text ? `: ${text}` : ""}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  getAgentModels: () => fetch(`${API_BASE_URL}/agents`).then(handle<Record<string, string>>),
  // Per agent, the models a single run can pick (the .env model first).
  getAgentModelChoices: () =>
    fetch(`${API_BASE_URL}/agent-models`).then(handle<Record<string, string[]>>),
  listSubmissions: () => fetch(`${API_BASE_URL}/submissions`).then(handle<SubmissionListItem[]>),
  getSubmission: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}`).then(handle<Submission>),
  uploadSubmission: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return fetch(`${API_BASE_URL}/submissions`, { method: "POST", body: fd }).then(
      handle<{ id: string }>,
    );
  },
  validate: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/validate`, { method: "POST" }).then(handle<unknown>),
  // Always appends to any earlier oracle runs on this submission. stress runs
  // them under CPU load; those are kept apart and never count for the gate.
  oracle: (id: string, n: number, stress = false) =>
    fetch(`${API_BASE_URL}/submissions/${id}/oracle?n=${n}${stress ? "&stress=true" : ""}`, {
      method: "POST",
    }).then(handle<unknown>),
  nop: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/nop`, { method: "POST" }).then(handle<unknown>),
  // reasoningEffort / agent undefined = the server's defaults
  // (AGENT_REASONING_EFFORT, HARBOR_AGENT).
  // maxCost undefined = AGENT_RUN_BUDGET_USD from .env; costMode says whether
  // it is the whole run's cap or each trial's share (times n).
  agentTrials: (
    id: string,
    n: number,
    reasoningEffort?: ReasoningEffort,
    agent?: AgentChoice,
    maxCost?: number,
    costMode?: "total" | "per_trial",
    model?: string,
  ) =>
    fetch(
      `${API_BASE_URL}/submissions/${id}/agent-trials?n=${n}` +
        (reasoningEffort ? `&reasoning_effort=${reasoningEffort}` : "") +
        (agent ? `&agent=${agent}` : "") +
        (model ? `&model=${encodeURIComponent(model)}` : "") +
        (maxCost ? `&max_cost_usd=${maxCost}&cost_mode=${costMode ?? "total"}` : ""),
      { method: "POST" },
    ).then(handle<unknown>),
  // rubric "default" is omitted from the query string; "tb" swaps in the
  // vendored, richer Terminal-Bench rubric on the backend.
  failureAnalysis: (id: string, rubric?: RubricChoice) =>
    fetch(
      `${API_BASE_URL}/submissions/${id}/failure-analysis` +
        (rubric && rubric !== "default" ? `?rubric=${rubric}` : ""),
      { method: "POST" },
    ).then(handle<unknown>),
  // agent undefined = the server's default (HARBOR_AGENT). n = 1 to 3 trials,
  // run one after another; maxCost undefined = AGENT_RUN_BUDGET_USD; model
  // undefined = the agent's .env model, the one Agent Trials use.
  cheatTrial: (
    id: string,
    agent?: AgentChoice,
    reasoningEffort?: ReasoningEffort,
    cheatPrompt?: CheatPromptChoice,
    n = 1,
    maxCost?: number,
    model?: string,
  ) =>
    fetch(
      `${API_BASE_URL}/submissions/${id}/cheat-trial?` +
        [
          agent ? `agent=${agent}` : "",
          reasoningEffort ? `reasoning_effort=${reasoningEffort}` : "",
          cheatPrompt && cheatPrompt !== "default" ? `cheat_prompt=${cheatPrompt}` : "",
          `n=${n}`,
          maxCost ? `max_cost_usd=${maxCost}` : "",
          model ? `model=${encodeURIComponent(model)}` : "",
        ]
          .filter(Boolean)
          .join("&"),
      { method: "POST" },
    ).then(handle<unknown>),
  variantProbe: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/variant-probe`, { method: "POST" }).then(
      handle<unknown>,
    ),
  weakProbe: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/weak-probe`, { method: "POST" }).then(handle<unknown>),
  // probe also runs every uncovered case through the real verifier (needs an Oracle pass);
  // probeCovered adds the cases the judge marked covered, to check that verdict too.
  edgeCases: (id: string, probe = false, probeCovered = false) =>
    fetch(
      `${API_BASE_URL}/submissions/${id}/edge-cases${
        probe ? `?probe=true${probeCovered ? "&probe_covered=true" : ""}` : ""
      }`,
      { method: "POST" },
    ).then(handle<unknown>),
  tier0: (id: string, repeats = 1, harbor = true) =>
    fetch(`${API_BASE_URL}/submissions/${id}/tier0?repeats=${repeats}&harbor=${harbor}`, {
      method: "POST",
    }).then(handle<unknown>),
  // Tier 0, 1, 2 repeated on the patched task until a round needs no patch. maxCostUsd is one
  // shared cap for Tier 1 and Tier 2 across every round.
  allTiers: (
    id: string,
    opts: {
      repeats?: number;
      maxCostUsd?: number;
      model?: string;
      maxRounds?: number;
    } = {},
  ) => {
    const q = new URLSearchParams();
    if (opts.maxRounds) q.set("max_rounds", String(opts.maxRounds));
    if (opts.repeats) q.set("repeats", String(opts.repeats));
    if (opts.maxCostUsd !== undefined) q.set("max_cost_usd", String(opts.maxCostUsd));
    if (opts.model) q.set("model", opts.model);
    const qs = q.toString();
    return fetch(`${API_BASE_URL}/submissions/${id}/tiers${qs ? `?${qs}` : ""}`, {
      method: "POST",
    }).then(handle<unknown>);
  },
  tier1: (id: string, opts: { maxCostUsd?: number; model?: string } = {}) => {
    const q = new URLSearchParams();
    if (opts.maxCostUsd !== undefined) q.set("max_cost_usd", String(opts.maxCostUsd));
    if (opts.model) q.set("model", opts.model);
    const qs = q.toString();
    return fetch(`${API_BASE_URL}/submissions/${id}/tier1${qs ? `?${qs}` : ""}`, {
      method: "POST",
    }).then(handle<unknown>);
  },
  tier2: (
    id: string,
    opts: { maxCostUsd?: number; model?: string; taskCopy?: string } = {},
  ) => {
    const q = new URLSearchParams();
    if (opts.maxCostUsd !== undefined) q.set("max_cost_usd", String(opts.maxCostUsd));
    if (opts.model) q.set("model", opts.model);
    if (opts.taskCopy) q.set("task_copy", opts.taskCopy);
    const qs = q.toString();
    return fetch(`${API_BASE_URL}/submissions/${id}/tier2${qs ? `?${qs}` : ""}`, {
      method: "POST",
    }).then(handle<unknown>);
  },
  answerExposure: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/answer-exposure`, { method: "POST" }).then(
      handle<unknown>,
    ),
  imageInventory: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/image-inventory`, { method: "POST" }).then(
      handle<unknown>,
    ),
  freshBuild: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/fresh-build`, { method: "POST" }).then(
      handle<unknown>,
    ),
  // Score saved trials again with this upload's grader, no agent run. `previous` (default)
  // re-scores the latest earlier upload's trials, which is how a grader fix is checked.
  regrade: (
    id: string,
    kind: "agent" | "oracle" = "agent",
    source: "previous" | "self" = "previous",
  ) =>
    fetch(`${API_BASE_URL}/submissions/${id}/regrade?kind=${kind}&source=${source}`, {
      method: "POST",
    }).then(handle<unknown>),
  versions: () => fetch(`${API_BASE_URL}/versions`).then(handle<VersionInfo>),
  changes: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/changes`).then(handle<SubmissionChanges>),
  // Always sends the choice: the backend default is "tb", so leaving it out
  // would never select Harbor's own rubric.
  rubricCheck: (id: string, rubric: RubricChoice = "tb") =>
    fetch(`${API_BASE_URL}/submissions/${id}/rubric-check?rubric=${rubric}`, {
      method: "POST",
    }).then(handle<unknown>),
  // runs 1..3: independent judge calls, majority decides (cost grows with runs)
  sufficiency: (id: string, runs = 1) =>
    fetch(`${API_BASE_URL}/submissions/${id}/sufficiency?runs=${runs}`, { method: "POST" }).then(
      handle<unknown>,
    ),
  leakageScan: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/leakage-scan`, { method: "POST" }).then(
      handle<unknown>,
    ),
  reviewReport: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/review-report`, { method: "POST" }).then(
      handle<unknown>,
    ),
  staticChecks: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/static-checks`, { method: "POST" }).then(
      handle<unknown>,
    ),
  graderIsolation: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/grader-isolation`, { method: "POST" }).then(
      handle<GraderIsolationResult>,
    ),
  aiDetectionDecision: (
    id: string,
    body: { ruling: "approved" | "disapproved"; adjudicator: string; reason: string },
  ) =>
    fetch(`${API_BASE_URL}/submissions/${id}/ai-detection-decision`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(handle<unknown>),
  hashProof: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/hash-proof`, { method: "POST" }).then(
      handle<unknown>,
    ),
  workerProbe: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/worker-probe`, { method: "POST" }).then(
      handle<unknown>,
    ),
  tbStaticChecks: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/tb-static-checks`, { method: "POST" }).then(
      handle<unknown>,
    ),
  aiDetection: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/ai-detection`, { method: "POST" }).then(
      handle<unknown>,
    ),
  // C5: one downloadable bundle of the build log, static checks and rubric
  // check -- triggers a browser download rather than returning JSON to read
  // inline, since the whole point is a file a human can save/attach.
  downloadEvidence: async (id: string) => {
    const res = await fetch(`${API_BASE_URL}/submissions/${id}/evidence`);
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${id}-evidence.json`;
    a.click();
    URL.revokeObjectURL(url);
  },
};
