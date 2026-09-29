export const API_BASE_URL =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ||
  "http://localhost:8000";

export type StageStatus = "pending" | "running" | "passed" | "failed" | "not-run";

export interface StageResult {
  status: StageStatus;
  reward: 0 | 1 | null;
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
}

// Oracle can be run N times, always appended to the ones already there (Gate
// 1 needs 3 *consecutive* 1.0 runs). "passed" only once every run passed.
export interface OracleRunsResult {
  status: StageStatus;
  n: number;
  runs: TrialResult[];
  all_passed: boolean | null;
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

// A model's judgment call, not a fact — unlike LeakageScanResult, false
// positives are expected and accepted. Never merge with leakage_scan.
export interface CodeSmellResult {
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
}

// One trial run with an explicit cheat directive. `logs` is JSON once the
// stage has run (see CheatTrial below); reward=1 alone doesn't mean a real
// cheat -- see cheat_check.
export interface CheatTrialResult {
  status: StageStatus;
  reward: 0 | 1 | null;
  logs?: string;
  // Recorded for audit only -- never compared against ChecksumInfo.canonical.
  // This trial's task copy always has an intentionally edited instruction.md.
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

// Reasoning levels the backend accepts for one run of trials.
export const REASONING_EFFORTS = ["low", "medium", "high", "xhigh", "max"] as const;
export type ReasoningEffort = (typeof REASONING_EFFORTS)[number];

// Agents a run of trials can use; the backend picks each one's model from .env
// (codex: AGENT_MODEL, claude-code: CLAUDE_AGENT_MODEL).
export const AGENT_CHOICES = [
  { value: "codex", label: "Codex · GPT-6 Astra" },
  { value: "claude-code", label: "Claude Code · Fable 5.1" },
] as const;
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
  code_smell: CodeSmellResult;
  review_report: ReviewReportResult;
  failure_analysis: FailureAnalysisResult;
  cheat_trial: CheatTrialResult;
  rubric_check: RubricCheckResult;
  static_checks: StaticChecksResult;
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
  code_smell_status: StageStatus;
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
  // Always appends to any earlier oracle runs on this submission.
  oracle: (id: string, n: number) =>
    fetch(`${API_BASE_URL}/submissions/${id}/oracle?n=${n}`, { method: "POST" }).then(
      handle<unknown>,
    ),
  nop: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/nop`, { method: "POST" }).then(
      handle<unknown>,
    ),
  // reasoningEffort / agent undefined = the server's defaults
  // (AGENT_REASONING_EFFORT, HARBOR_AGENT).
  agentTrials: (id: string, n: number, reasoningEffort?: ReasoningEffort, agent?: AgentChoice) =>
    fetch(
      `${API_BASE_URL}/submissions/${id}/agent-trials?n=${n}` +
        (reasoningEffort ? `&reasoning_effort=${reasoningEffort}` : "") +
        (agent ? `&agent=${agent}` : ""),
      { method: "POST" },
    ).then(handle<unknown>),
  failureAnalysis: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/failure-analysis`, { method: "POST" }).then(
      handle<unknown>,
    ),
  // agent undefined = the server's default (HARBOR_AGENT).
  cheatTrial: (id: string, agent?: AgentChoice) =>
    fetch(
      `${API_BASE_URL}/submissions/${id}/cheat-trial` + (agent ? `?agent=${agent}` : ""),
      { method: "POST" },
    ).then(handle<unknown>),
  rubricCheck: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/rubric-check`, { method: "POST" }).then(
      handle<unknown>,
    ),
  sufficiency: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/sufficiency`, { method: "POST" }).then(
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
  codeSmell: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/code-smell`, { method: "POST" }).then(
      handle<unknown>,
    ),
  staticChecks: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/static-checks`, { method: "POST" }).then(
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
