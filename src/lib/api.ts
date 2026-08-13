export const API_BASE_URL =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ||
  "http://localhost:8000";

export type StageStatus = "pending" | "running" | "passed" | "failed" | "not-run";

export interface StageResult {
  status: StageStatus;
  reward: 0 | 1 | null;
  logs?: string;
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
}

export interface AgentTrialsResult {
  status: StageStatus;
  n: number;
  trials: TrialResult[];
  pass_rate: number | null;
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

export interface Submission {
  id: string;
  task_name: string;
  uploaded_at: string;
  build: BuildResult;
  oracle: StageResult;
  nop: StageResult;
  agent_trials: AgentTrialsResult;
  sufficiency: SufficiencyResult;
  leakage_scan: LeakageScanResult;
  code_smell: CodeSmellResult;
  review_report: ReviewReportResult;
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
  oracle: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/oracle`, { method: "POST" }).then(
      handle<unknown>,
    ),
  nop: (id: string) =>
    fetch(`${API_BASE_URL}/submissions/${id}/nop`, { method: "POST" }).then(
      handle<unknown>,
    ),
  agentTrials: (id: string, n: number) =>
    fetch(`${API_BASE_URL}/submissions/${id}/agent-trials?n=${n}`, {
      method: "POST",
    }).then(handle<unknown>),
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
};
