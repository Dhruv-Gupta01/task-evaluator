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

export interface VerifierReport {
  reward_raw: number | null;
  ctrf: CtrfSummary | null;
  details: { file: string; content: string } | null;
  flags: string[];
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
  logs?: string;
  task_checksum?: string | null;
}

export interface BuildResult {
  status: StageStatus;
  logs?: string;
  image_tag?: string | null;
}

export interface TrialResult {
  index: number;
  reward: 0 | 1 | null;
  raw_reward?: number | null;
  verifier?: VerifierReport | null;
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
    probe?: EdgeCaseProbe;
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
  dropped?: { description: string; reason: string }[];
  variants: VariantEntry[];
}

export interface ReviewerDecision {
  decision: "grader_wrong" | "agent_wrong" | "hard_not_ambiguous" | "unsure";
  note: string;
  at?: string;
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
  reviewer_decisions: Record<string, ReviewerDecision>;
  static_checks: StaticChecksResult;
  tb_static_checks: TbStaticChecksResult;
  ai_detection: AiDetectionResult;
  answer_exposure: AnswerExposureResult;
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
  getAgentModels: () =>
    fetch(`${API_BASE_URL}/agents`).then(handle<Record<string, string>>),
  listSubmissions: () =>
    fetch(`${API_BASE_URL}/submissions`).then(handle<SubmissionListItem[]>),
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
    fetch(`${API_BASE_URL}/submissions/${id}/validate`, { method: "POST" }).then(
      handle<unknown>,
    ),
  // Always appends to any earlier oracle runs on this submission. stress runs
  // them under CPU load; those are kept apart and never count for the gate.
  oracle: (id: string, n: number, stress = false) =>
    fetch(`${API_BASE_URL}/submissions/${id}/oracle?n=${n}${stress ? "&stress=true" : ""}`, {
      method: "POST",
    }).then(handle<unknown>),
  nop: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/nop`, { method: "POST" }).then(
      handle<unknown>,
    ),
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
  ) =>
    fetch(
      `${API_BASE_URL}/submissions/${id}/agent-trials?n=${n}` +
        (reasoningEffort ? `&reasoning_effort=${reasoningEffort}` : "") +
        (agent ? `&agent=${agent}` : "") +
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
  // run one after another; maxCost undefined = AGENT_RUN_BUDGET_USD.
  cheatTrial: (
    id: string,
    agent?: AgentChoice,
    reasoningEffort?: ReasoningEffort,
    cheatPrompt?: CheatPromptChoice,
    n = 1,
    maxCost?: number,
  ) =>
    fetch(
      `${API_BASE_URL}/submissions/${id}/cheat-trial?` +
        [
          agent ? `agent=${agent}` : "",
          reasoningEffort ? `reasoning_effort=${reasoningEffort}` : "",
          cheatPrompt && cheatPrompt !== "default" ? `cheat_prompt=${cheatPrompt}` : "",
          `n=${n}`,
          maxCost ? `max_cost_usd=${maxCost}` : "",
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
  changes: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/changes`).then(handle<SubmissionChanges>),
  setReviewerDecision: (id: string, body: { key: string } & ReviewerDecision) =>
    fetch(`${API_BASE_URL}/submissions/${id}/reviewer-decision`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(handle<unknown>),
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
