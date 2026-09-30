import { useState } from "react";
import { createFileRoute, Link } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, AlertTriangle } from "lucide-react";
import {
  api,
  AGENT_CHOICES,
  REASONING_EFFORTS,
  RUBRIC_CHOICES,
  CHEAT_PROMPT_CHOICES,
  type AgentChoice,
  type CheatTrial,
  type FailureAnalysis,
  type RubricCheck,
  type ReasoningEffort,
  type RubricChoice,
  type CheatPromptChoice,
  type Submission,
  type StageStatus,
  type StaticCheckReport,
  type TbStaticCheckReport,
  type AiDetectionReport,
  type AnalysisCheck,
} from "@/lib/api";
import { StatusBadge } from "@/components/StatusBadge";
import { LogPanel } from "@/components/LogPanel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { toast } from "sonner";

export const Route = createFileRoute("/submissions/$id")({
  head: ({ params }) => ({
    meta: [
      { title: `Submission ${params.id.slice(0, 8)} — Task Eval Dashboard` },
      { name: "description", content: "Submission evaluation stages and logs." },
      { name: "robots", content: "noindex" },
    ],
  }),
  component: SubmissionDetail,
});

const isTerminal = (s: StageStatus) => s === "passed" || s === "failed" || s === "not-run" || s === "pending";
const isActive = (s: StageStatus) => s === "running" || s === "pending";

function SubmissionDetail() {
  const { id } = Route.useParams();
  const qc = useQueryClient();

  const { data, error, isLoading } = useQuery({
    queryKey: ["submission", id],
    queryFn: () => api.getSubmission(id),
    refetchInterval: (q) => {
      const d = q.state.data as Submission | undefined;
      if (!d) return 3000;
      const any =
        d.build.status === "running" ||
        d.oracle.status === "running" ||
        d.nop.status === "running" ||
        d.agent_trials.status === "running" ||
        d.sufficiency.status === "running" ||
        d.sufficiency.status === "pending" ||
        isActive(d.failure_analysis.status) ||
        d.build.status === "pending";
      return any ? 2500 : false;
    },
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["submission", id] });

  const mValidate = useMutation({
    mutationFn: () => api.validate(id),
    onSuccess: () => {
      toast.success("Validate & Build started");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  // Every trigger appends n more runs to whatever's already there (Gate 1
  // needs 3 consecutive reward=1 runs), so default to 3.
  const [oracleN, setOracleN] = useState(3);
  const mOracle = useMutation({
    mutationFn: () => api.oracle(id, oracleN),
    onSuccess: () => {
      toast.success(`${oracleN} oracle run(s) started`);
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const mNop = useMutation({
    mutationFn: () => api.nop(id),
    onSuccess: () => {
      toast.success("Nop started");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  // Each trial is a paid LLM run, so default to one; the backend caps n at 50.
  const [n, setN] = useState(1);
  // "default" = whatever AGENT_REASONING_EFFORT the server is configured with.
  const [effort, setEffort] = useState<ReasoningEffort | "default">("default");
  // Defaults to max, not "default" -- a cheat trial run at whatever
  // AGENT_REASONING_EFFORT happens to be (e.g. low) is a weaker test of
  // whether the agent will cheat than one run at max effort.
  const [cheatEffort, setCheatEffort] = useState<ReasoningEffort | "default">("max");
  const [cheatPrompt, setCheatPrompt] = useState<CheatPromptChoice>("default");
  const [analysisRubric, setAnalysisRubric] = useState<RubricChoice>("default");
  const [checkRubric, setCheckRubric] = useState<RubricChoice>("default");
  const [agentChoice, setAgentChoice] = useState<AgentChoice>("codex");
  const mAgent = useMutation({
    mutationFn: () =>
      api.agentTrials(id, n, effort === "default" ? undefined : effort, agentChoice),
    onSuccess: () => {
      const label = AGENT_CHOICES.find((a) => a.value === agentChoice)?.label ?? agentChoice;
      toast.success(
        `Agent trials (${label}, n=${n}, reasoning ${effort === "default" ? "server default" : effort}) started`,
      );
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const mSufficiency = useMutation({
    mutationFn: () => api.sufficiency(id),
    onSuccess: () => {
      toast.success("Sufficiency check started");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const mLeakageScan = useMutation({
    mutationFn: () => api.leakageScan(id),
    onSuccess: () => {
      toast.success("Leakage scan started");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const mStaticChecks = useMutation({
    mutationFn: () => api.staticChecks(id),
    onSuccess: () => {
      toast.success("Static checks started");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const mTbStaticChecks = useMutation({
    mutationFn: () => api.tbStaticChecks(id),
    onSuccess: () => {
      toast.success("TB static checks started");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const mAiDetection = useMutation({
    mutationFn: () => api.aiDetection(id),
    onSuccess: () => {
      toast.success("AI detection started");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const mEvidence = useMutation({
    mutationFn: () => api.downloadEvidence(id),
    onError: (e: Error) => toast.error(e.message),
  });
  const mCodeSmell = useMutation({
    mutationFn: () => api.codeSmell(id),
    onSuccess: () => {
      toast.success("Code smell check started");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const mAnalysis = useMutation({
    mutationFn: () => api.failureAnalysis(id, analysisRubric),
    onSuccess: () => {
      toast.success(`Failure analysis (${analysisRubric} rubric) started`);
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const mCheatTrial = useMutation({
    mutationFn: () =>
      api.cheatTrial(
        id,
        agentChoice,
        cheatEffort === "default" ? undefined : cheatEffort,
        cheatPrompt,
      ),
    onSuccess: () => {
      toast.success(
        `Cheat trial (reasoning ${cheatEffort === "default" ? "server default" : cheatEffort}, ${cheatPrompt} prompt) started`,
      );
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const mRubricCheck = useMutation({
    mutationFn: () => api.rubricCheck(id, checkRubric),
    onSuccess: () => {
      toast.success(`Rubric check (${checkRubric} rubric) started`);
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const mReviewReport = useMutation({
    mutationFn: () => api.reviewReport(id),
    onSuccess: () => {
      toast.success("Review report generation started");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });

  if (isLoading) {
    return (
      <PageShell>
        <div className="p-8 text-sm text-muted-foreground">Loading…</div>
      </PageShell>
    );
  }
  if (error || !data) {
    return (
      <PageShell>
        <div className="p-8 text-sm text-red-600">
          Failed to load: {(error as Error)?.message ?? "unknown error"}
        </div>
      </PageShell>
    );
  }

  const buildReady = data.build.status === "passed";
  const validated = data.build.status !== "not-run";
  const agentDone =
    data.agent_trials.status === "passed" || data.agent_trials.status === "failed";
  const oracleNopPassed =
    data.oracle.status === "passed" && data.nop.status === "passed";

  const TERMINAL: StageStatus[] = ["passed", "failed"];
  const reviewReportMissingGates = [
    TERMINAL.includes(data.build.status) ? null : "build",
    TERMINAL.includes(data.oracle.status) ? null : "oracle",
    TERMINAL.includes(data.nop.status) ? null : "nop",
    TERMINAL.includes(data.sufficiency.status) ? null : "sufficiency",
    TERMINAL.includes(data.agent_trials.status) ? null : "agent trials",
  ].filter((g): g is string => g !== null);
  const reviewReportReady = reviewReportMissingGates.length === 0;

  return (
    <PageShell>
      <div className="mb-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">
              {data.task_name || "Untitled task"}
            </h1>
            <div className="mt-1 text-sm text-muted-foreground">
              <span className="font-mono">{data.id}</span> · uploaded{" "}
              {formatDate(data.uploaded_at)}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <StatusBadge status={data.build.status} label="build" />
            {data.build.image_tag && (
              <span className="rounded bg-muted px-2 py-0.5 font-mono text-xs">
                {data.build.image_tag}
              </span>
            )}
            <Button
              variant="outline"
              size="sm"
              onClick={() => mEvidence.mutate()}
              disabled={mEvidence.isPending}
            >
              {mEvidence.isPending ? "Preparing…" : "Download Evidence"}
            </Button>
          </div>
        </div>
      </div>

      {data.checksum.consistent === false && (
        <div className="mb-6 flex items-start gap-2 rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-700 dark:text-red-400">
          <span>
            <strong>Task version mismatch:</strong> {data.checksum.mismatched.join(", ")} ran
            against a different task checksum than the rest of this submission&apos;s runs (
            <span className="font-mono text-xs">{data.checksum.canonical}</span>). Every gate
            result here may describe a different task version, not one frozen submission — re-run
            the affected stages before trusting this report.
          </span>
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        {/* Validate & Build */}
        <StageCard
          title="Validate & Build"
          description="Validate contract and build the task image."
          status={data.build.status}
        >
          <div className="flex items-center gap-2">
            <Button
              onClick={() => mValidate.mutate()}
              disabled={mValidate.isPending || data.build.status === "running"}
            >
              {data.build.status === "running" ? "Building…" : "Validate & Build"}
            </Button>
          </div>
          <LogPanel logs={data.build.logs} title="Build logs" />
        </StageCard>

        {/* Oracle */}
        <StageCard
          title="Run Oracle"
          description="Run the oracle solution against tests, N times. Every run is kept — appended to whatever's already there, never overwritten — since 3 consecutive reward=1 runs is what Gate 1 actually needs, not just the latest one."
          status={data.oracle.status}
        >
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-sm text-muted-foreground">N =</label>
            <Input
              type="number"
              min={1}
              max={50}
              value={oracleN}
              onChange={(e) => setOracleN(Math.min(50, Math.max(1, Number(e.target.value) || 1)))}
              className="w-20"
            />
            <RunButton
              label="Run Oracle"
              runningLabel="Running…"
              onClick={() => mOracle.mutate()}
              running={data.oracle.status === "running"}
              pending={mOracle.isPending}
              disabled={!buildReady}
              disabledReason="Build the image first"
            />
          </div>

          {data.oracle.runs.length > 0 && (
            <div className="rounded-md border border-border">
              <div className="flex items-center justify-between border-b border-border px-3 py-2 text-sm">
                <span className="font-medium">Runs</span>
                <span className="text-muted-foreground">
                  {data.oracle.runs.filter((r) => r.reward === 1).length}/{data.oracle.runs.length} passed
                  {data.oracle.all_passed != null && (
                    <> — {data.oracle.all_passed ? "all passed" : "not all passed"}</>
                  )}
                </span>
              </div>
              <ul className="divide-y divide-border">
                {data.oracle.runs.map((r) => (
                  <li key={r.index} className="px-3 py-2">
                    <div className="flex items-center justify-between text-sm">
                      <span className="font-mono">Run #{r.index}</span>
                      <StatusBadge
                        status={
                          r.reward === 1 ? "passed" : r.reward === 0 ? "failed" : "running"
                        }
                      />
                    </div>
                    {r.logs && (
                      <div className="mt-2">
                        <LogPanel logs={r.logs} title={`Run #${r.index} logs`} />
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </StageCard>

        {/* Nop */}
        <StageCard
          title="Run Nop"
          description="Run the no-op baseline against tests."
          status={data.nop.status}
          reward={data.nop.reward}
        >
          <RunButton
            label="Run Nop"
            runningLabel="Running…"
            onClick={() => mNop.mutate()}
            running={data.nop.status === "running"}
            pending={mNop.isPending}
            disabled={!buildReady}
            disabledReason="Build the image first"
          />
          <LogPanel logs={data.nop.logs} title="Nop logs" />
        </StageCard>

        {/* Agent Trials */}
        <StageCard
          title="Run Agent Trials"
          description="Run N independent agent trials."
          status={data.agent_trials.status}
        >
          {!oracleNopPassed && buildReady && (
            <div className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-2.5 text-xs text-amber-700 dark:text-amber-400">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>
                Oracle/Nop haven't both passed yet — agent results may not be
                meaningful.
              </span>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-sm text-muted-foreground">N =</label>
            <Input
              type="number"
              min={1}
              max={50}
              value={n}
              onChange={(e) => setN(Math.min(50, Math.max(1, Number(e.target.value) || 1)))}
              className="w-20"
            />
            <label className="text-sm text-muted-foreground">Agent</label>
            <Select value={agentChoice} onValueChange={(v) => setAgentChoice(v as AgentChoice)}>
              <SelectTrigger className="w-52">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {AGENT_CHOICES.map((a) => (
                  <SelectItem key={a.value} value={a.value}>
                    {a.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <label className="text-sm text-muted-foreground">Reasoning</label>
            <Select value={effort} onValueChange={(v) => setEffort(v as ReasoningEffort | "default")}>
              <SelectTrigger className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="default">Default</SelectItem>
                {REASONING_EFFORTS.map((e) => (
                  <SelectItem key={e} value={e}>
                    {e}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <RunButton
              label="Run Agent Trials"
              runningLabel="Running…"
              onClick={() => mAgent.mutate()}
              running={data.agent_trials.status === "running"}
              pending={mAgent.isPending}
              disabled={!buildReady}
              disabledReason="Build the image first"
            />
          </div>

          {data.agent_trials.trials && data.agent_trials.trials.length > 0 && (
            <div className="rounded-md border border-border">
              <div className="flex items-center justify-between border-b border-border px-3 py-2 text-sm">
                <span className="font-medium">Trials</span>
                <span className="text-muted-foreground">
                  {countPassed(data)}/{data.agent_trials.trials.length} passed
                  {data.agent_trials.pass_rate != null && (
                    <>
                      {" "}— {Math.round(data.agent_trials.pass_rate * 100)}%
                    </>
                  )}
                </span>
              </div>
              {data.agent_trials.pass_at_k && (
                <div className="flex flex-wrap gap-x-4 gap-y-1 border-b border-border px-3 py-2 text-xs text-muted-foreground">
                  {Object.entries(data.agent_trials.pass_at_k)
                    .sort((a, b) => Number(a[0]) - Number(b[0]))
                    .map(([k, v]) => (
                      <span key={k}>
                        pass@{k}: <span className="font-mono">{(v * 100).toFixed(1)}%</span>
                      </span>
                    ))}
                </div>
              )}
              <ul className="divide-y divide-border">
                {data.agent_trials.trials.map((t) => (
                  <li key={t.index} className="px-3 py-2">
                    <div className="flex items-center justify-between text-sm">
                      <span className="font-mono">Trial #{t.index}</span>
                      <StatusBadge
                        status={
                          t.reward === 1
                            ? "passed"
                            : t.reward === 0
                            ? "failed"
                            : "running"
                        }
                      />
                    </div>
                    {t.logs && (
                      <div className="mt-2">
                        <LogPanel logs={t.logs} title={`Trial #${t.index} logs`} />
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </StageCard>

        {/* Instruction Sufficiency */}
        <StageCard
          title="Instruction Sufficiency"
          description="LLM judge: is instruction.md + environment/ enough to pass the hidden tests?"
          status={data.sufficiency.status}
        >
          <RunButton
            label="Run Sufficiency Check"
            runningLabel="Checking…"
            onClick={() => mSufficiency.mutate()}
            running={data.sufficiency.status === "running" || data.sufficiency.status === "pending"}
            pending={mSufficiency.isPending}
            disabled={!validated}
            disabledReason="Validate the submission first"
          />
          {data.sufficiency.passed != null && (
            <div
              className={`rounded-md border p-2.5 text-xs ${
                data.sufficiency.passed
                  ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
                  : "border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-400"
              }`}
            >
              {data.sufficiency.passed
                ? "Sufficient — no gaps found."
                : "Insufficient — see gaps below."}
            </div>
          )}
          <LogPanel logs={data.sufficiency.logs} title="Sufficiency verdict" />
        </StageCard>

        {/* Static Checks */}
        <StageCard
          title="Static Checks"
          description="Advisory, LLM-free mechanical checks over instruction.md/test.sh/Dockerfile/zip layout — word counts, formatting, hygiene. Not a pass/fail gate."
          status={data.static_checks.status}
        >
          <RunButton
            label="Run Static Checks"
            runningLabel="Checking…"
            onClick={() => mStaticChecks.mutate()}
            running={
              data.static_checks.status === "running" || data.static_checks.status === "pending"
            }
            pending={mStaticChecks.isPending}
            disabled={!validated}
            disabledReason="Validate the submission first"
          />
          {data.static_checks.logs &&
            (() => {
              let report: StaticCheckReport | null = null;
              try {
                report = JSON.parse(data.static_checks.logs) as StaticCheckReport;
              } catch {
                report = null;
              }
              if (!report) return null;
              return (
                <>
                  <div
                    className={`rounded-md border p-2.5 text-xs ${
                      report.fail_count === 0
                        ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
                        : "border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-400"
                    }`}
                  >
                    {report.fail_count} fail, {report.warn_count} warn, {report.results.length}{" "}
                    checks total
                  </div>
                  <LogPanel logs={report.text} title="Static check findings" />
                </>
              );
            })()}
        </StageCard>

        {/* TB Static Checks */}
        <StageCard
          title="TB Static Checks"
          description="Advisory — Terminal-Bench's own official static checks (vendor/tb_checks/), a different rule set from the platform's own Static Checks above. Not a pass/fail gate."
          status={data.tb_static_checks.status}
        >
          <RunButton
            label="Run TB Static Checks"
            runningLabel="Checking…"
            onClick={() => mTbStaticChecks.mutate()}
            running={
              data.tb_static_checks.status === "running" ||
              data.tb_static_checks.status === "pending"
            }
            pending={mTbStaticChecks.isPending}
            disabled={!validated}
            disabledReason="Validate the submission first"
          />
          {data.tb_static_checks.logs &&
            (() => {
              let report: TbStaticCheckReport | null = null;
              try {
                report = JSON.parse(data.tb_static_checks.logs) as TbStaticCheckReport;
              } catch {
                report = null;
              }
              if (!report) return null;
              return (
                <>
                  <div
                    className={`rounded-md border p-2.5 text-xs ${
                      report.fail_count === 0
                        ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
                        : "border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-400"
                    }`}
                  >
                    {report.pass_count} pass, {report.fail_count} fail
                  </div>
                  <LogPanel logs={report.text} title="TB check findings" />
                </>
              );
            })()}
        </StageCard>

        {/* AI Detection */}
        <StageCard
          title="AI Detection"
          description="Advisory — Terminal-Bench's own Layer 3 AI-usage detection (vendor/tb_checks/): calls the real GPTZero API against instruction.md and solution/solve.sh. Genuinely optional in Terminal-Bench's own workflow too. Requires GPTZERO_API_KEY; runs a graceful no-op without it."
          status={data.ai_detection.status}
        >
          <RunButton
            label="Run AI Detection"
            runningLabel="Checking…"
            onClick={() => mAiDetection.mutate()}
            running={
              data.ai_detection.status === "running" || data.ai_detection.status === "pending"
            }
            pending={mAiDetection.isPending}
            disabled={!validated}
            disabledReason="Validate the submission first"
          />
          {data.ai_detection.logs &&
            (() => {
              let report: AiDetectionReport | null = null;
              try {
                report = JSON.parse(data.ai_detection.logs) as AiDetectionReport;
              } catch {
                report = null;
              }
              if (!report) return null;
              if (report.skipped) {
                return (
                  <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-2.5 text-xs text-amber-700 dark:text-amber-400">
                    Skipped — {report.api_key_configured
                      ? "GPTZero returned a network error"
                      : "GPTZERO_API_KEY not configured"}
                    . Not a pass/fail result.
                  </div>
                );
              }
              return (
                <>
                  {report.error && (
                    <div className="rounded-md border border-red-500/30 bg-red-500/10 p-2.5 text-xs whitespace-pre-wrap max-h-48 overflow-y-auto">
                      {report.error}
                    </div>
                  )}
                  {report.passed != null && (
                    <div
                      className={`rounded-md border p-2.5 text-xs ${
                        report.passed
                          ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
                          : "border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-400"
                      }`}
                    >
                      {report.passed
                        ? "Below the AI-generated threshold."
                        : "Flagged — at or above the 70% AI-generated threshold."}
                    </div>
                  )}
                  <LogPanel logs={report.output ?? undefined} title="GPTZero output" />
                </>
              );
            })()}
        </StageCard>

        {/* AI Usage Detection -- all 3 TB layers, shown side by side, never
            merged into one score. This is a read-only view over data from
            Rubric Check (TB rubric) and AI Detection above, not its own
            runnable check -- no run button, no status badge of its own. */}
        <AiLayersOverview rubricCheck={data.rubric_check} aiDetection={data.ai_detection} />

        {/* Leakage Scan */}
        <StageCard
          title="Leakage Scan"
          description="Advisory only — flags unedited LLM chat artifacts (disclaimers, unfilled placeholders). Not a pass/fail gate."
          status={data.leakage_scan.status}
        >
          <RunButton
            label="Run Leakage Scan"
            runningLabel="Scanning…"
            onClick={() => mLeakageScan.mutate()}
            running={
              data.leakage_scan.status === "running" || data.leakage_scan.status === "pending"
            }
            pending={mLeakageScan.isPending}
            disabled={!validated}
            disabledReason="Validate the submission first"
          />
          {data.leakage_scan.passed != null && (
            <div
              className={`rounded-md border p-2.5 text-xs ${
                data.leakage_scan.passed
                  ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
                  : "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400"
              }`}
            >
              {data.leakage_scan.passed
                ? "Clean — no leakage artifacts found."
                : "Artifacts found — worth a human look, not proof of misconduct."}
            </div>
          )}
          <LogPanel logs={data.leakage_scan.logs} title="Leakage scan findings" />
        </StageCard>

        {/* Code Smell */}
        <StageCard
          title="Code Smell (AI-generated?)"
          description="LLM judgment call, not a fact — false positives expected and accepted. A careful human can still read as AI-like. Separate from Leakage Scan's verified findings."
          status={data.code_smell.status}
        >
          <RunButton
            label="Run Code Smell Check"
            runningLabel="Reading code…"
            onClick={() => mCodeSmell.mutate()}
            running={
              data.code_smell.status === "running" || data.code_smell.status === "pending"
            }
            pending={mCodeSmell.isPending}
            disabled={!validated}
            disabledReason="Validate the submission first"
          />
          {data.code_smell.passed != null && (
            <div
              className={`rounded-md border p-2.5 text-xs ${
                data.code_smell.passed
                  ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
                  : "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400"
              }`}
            >
              {data.code_smell.passed
                ? "Reads as human-iterated — no strong AI-style signal."
                : "Reads as possibly AI-generated — a judgment call, not proof."}
            </div>
          )}
          <LogPanel logs={data.code_smell.logs} title="Code smell reasoning" />
        </StageCard>

        {/* Failure analysis (harbor analyze) */}
        <StageCard
          title="Failure Analysis"
          description="harbor analyze over the agent trials: a cheap model reads each trajectory and checks for reward hacking and for failures caused by an unclear instruction. Advisory."
          status={data.failure_analysis.status}
        >
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-sm text-muted-foreground">Rubric</label>
            <Select value={analysisRubric} onValueChange={(v) => setAnalysisRubric(v as RubricChoice)}>
              <SelectTrigger className="w-40">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {RUBRIC_CHOICES.map((r) => (
                  <SelectItem key={r.value} value={r.value}>
                    {r.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <RunButton
              label="Analyze Trials"
              runningLabel="Analyzing…"
              onClick={() => mAnalysis.mutate()}
              running={isActive(data.failure_analysis.status)}
              pending={mAnalysis.isPending}
              disabled={!agentDone}
              disabledReason="Run the agent trials to completion first"
            />
          </div>
          <AnalysisView logs={data.failure_analysis.logs} />
        </StageCard>

        {/* Cheat trial */}
        <StageCard
          title="Cheat Trial"
          description="One trial run with an explicit directive to cheat spliced into the instruction (edit the verifier, hardcode outputs, read the reference solution). If reward comes back 1, a harbor analyze reward_hacking check runs automatically — reward alone can't tell a real cheat apart from the agent refusing and solving it honestly (both have happened live), so passing needs that check to positively clear it, not just a low reward."
          status={data.cheat_trial.status}
        >
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-sm text-muted-foreground">Reasoning</label>
            <Select
              value={cheatEffort}
              onValueChange={(v) => setCheatEffort(v as ReasoningEffort | "default")}
            >
              <SelectTrigger className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="default">Default</SelectItem>
                {REASONING_EFFORTS.map((e) => (
                  <SelectItem key={e} value={e}>
                    {e}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <label className="text-sm text-muted-foreground">Prompt</label>
            <Select value={cheatPrompt} onValueChange={(v) => setCheatPrompt(v as CheatPromptChoice)}>
              <SelectTrigger className="w-40">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {CHEAT_PROMPT_CHOICES.map((p) => (
                  <SelectItem key={p.value} value={p.value}>
                    {p.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <RunButton
              label="Run Cheat Trial"
              runningLabel="Running…"
              onClick={() => mCheatTrial.mutate()}
              running={isActive(data.cheat_trial.status)}
              pending={mCheatTrial.isPending}
              disabled={!buildReady}
              disabledReason="Build the image first"
            />
          </div>
          <CheatTrialView status={data.cheat_trial.status} logs={data.cheat_trial.logs} />
        </StageCard>

        {/* Rubric check (harbor check) */}
        <StageCard
          title="Rubric Check"
          description="harbor check: an evaluator agent reads the whole task and scores it against Harbor's quality rubric — an automated stand-in for a human task reviewer. Advisory; needs ANTHROPIC_API_KEY. A very large environment/ (e.g. vendored dependencies) can hit an OS argument-length limit in Harbor's own launcher — that shows up as an error here, not a real quality verdict."
          status={data.rubric_check.status}
        >
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-sm text-muted-foreground">Rubric</label>
            <Select value={checkRubric} onValueChange={(v) => setCheckRubric(v as RubricChoice)}>
              <SelectTrigger className="w-40">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {RUBRIC_CHOICES.map((r) => (
                  <SelectItem key={r.value} value={r.value}>
                    {r.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <RunButton
              label="Run Rubric Check"
              runningLabel="Checking…"
              onClick={() => mRubricCheck.mutate()}
              running={isActive(data.rubric_check.status)}
              pending={mRubricCheck.isPending}
              disabled={!buildReady}
              disabledReason="Build the image first"
            />
          </div>
          <RubricCheckView logs={data.rubric_check.logs} />
        </StageCard>

        {/* Review Report */}
        <StageCard
          title="Review Report"
          description="Synthesizes the full platform report + static checks into a candidate-facing diagnostic. Draft for human review — not an auto-issued verdict."
          status={data.review_report.status}
        >
          <RunButton
            label="Generate Review Report"
            runningLabel="Generating…"
            onClick={() => mReviewReport.mutate()}
            running={
              data.review_report.status === "running" || data.review_report.status === "pending"
            }
            pending={mReviewReport.isPending}
            disabled={!reviewReportReady}
            disabledReason={
              reviewReportReady
                ? undefined
                : `Finish these first: ${reviewReportMissingGates.join(", ")}`
            }
          />
          {data.review_report.logs && (
            <div className="rounded-md border bg-muted/30 p-3 text-xs whitespace-pre-wrap max-h-[32rem] overflow-y-auto">
              {data.review_report.logs}
            </div>
          )}
        </StageCard>
      </div>
    </PageShell>
  );
}

function AnalysisView({ logs }: { logs?: string }) {
  if (!logs) return null;
  let parsed: FailureAnalysis | null = null;
  try {
    parsed = JSON.parse(logs) as FailureAnalysis;
  } catch {
    return <LogPanel logs={logs} title="Analysis output" />;
  }
  return (
    <div className="space-y-3">
      <div className="text-xs text-muted-foreground">
        {parsed.agent} · {parsed.model}
        {parsed.cost_usd != null && <> · ${parsed.cost_usd.toFixed(3)}</>}
        {parsed.rubric && <> · {parsed.rubric} rubric</>}
      </div>
      {parsed.error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-2.5 text-xs whitespace-pre-wrap max-h-48 overflow-y-auto">
          {parsed.error}
        </div>
      )}
      {parsed.job_summary && (
        <div className="rounded-md border border-border bg-muted/40 p-3 text-sm whitespace-pre-wrap">
          <div className="mb-1 text-xs font-medium text-muted-foreground">Job summary</div>
          {parsed.job_summary}
        </div>
      )}
      {parsed.results.map((r, i) => (
        <div key={r.trial_name ?? i} className="rounded-md border border-border p-3 text-sm">
          <div className="font-mono text-xs text-muted-foreground">{r.trial_name ?? `trial ${i}`}</div>
          {r.error ? (
            <div className="mt-1 text-xs text-red-600 dark:text-red-400 whitespace-pre-wrap">
              {r.error}
            </div>
          ) : (
            <>
              {r.summary && <p className="mt-1 whitespace-pre-wrap">{r.summary}</p>}
              <ul className="mt-2 space-y-1.5">
                {Object.entries(r.checks).map(([name, c]) => (
                  <li key={name} className="text-xs">
                    <span
                      className={`mr-2 rounded px-1.5 py-0.5 font-medium ${
                        c.outcome === "pass"
                          ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400"
                          : c.outcome === "fail"
                          ? "bg-red-500/15 text-red-700 dark:text-red-400"
                          : "bg-muted text-muted-foreground"
                      }`}
                    >
                      {name}: {c.outcome}
                    </span>
                    <span className="text-muted-foreground">{c.explanation}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      ))}
    </div>
  );
}

function LayerBadge({ outcome }: { outcome: string }) {
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-xs font-medium ${
        outcome === "pass"
          ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400"
          : outcome === "fail"
          ? "bg-red-500/15 text-red-700 dark:text-red-400"
          : "bg-muted text-muted-foreground"
      }`}
    >
      {outcome}
    </span>
  );
}

// Read-only summary of Terminal-Bench's three AI-usage detection layers,
// pulled from data already gathered by Rubric Check (TB rubric) and AI
// Detection above -- shown side by side, never merged into one score.
// Layer 1 is a written policy (no check to run). Layer 2 is one criterion
// (instruction_concision) inside the 35-criteria rubric -- an LLM's
// subjective opinion. Layer 3 is a real GPTZero measurement, entirely
// independent of Layer 2's opinion.
function AiLayersOverview({
  rubricCheck,
  aiDetection,
}: {
  rubricCheck: Submission["rubric_check"];
  aiDetection: Submission["ai_detection"];
}) {
  let layer2: AnalysisCheck | null = null;
  if (rubricCheck.logs) {
    try {
      const parsed = JSON.parse(rubricCheck.logs) as RubricCheck;
      layer2 = parsed.results?.[0]?.checks?.instruction_concision ?? null;
    } catch {
      // leave layer2 null -- unparseable logs, nothing to show
    }
  }

  let layer3: AiDetectionReport | null = null;
  if (aiDetection.logs) {
    try {
      layer3 = JSON.parse(aiDetection.logs) as AiDetectionReport;
    } catch {
      // leave layer3 null
    }
  }

  return (
    <div className="flex flex-col gap-3 rounded-lg border border-border bg-card p-4 md:col-span-2">
      <div>
        <h3 className="text-base font-semibold">AI Usage Detection — All Layers</h3>
        <p className="text-xs text-muted-foreground">
          Terminal-Bench's three AI-usage detection layers, side by side — never merged into one
          score, since Layer 2 is an LLM's opinion and Layer 3 is a real measurement. This is a
          view, not its own check: run Rubric Check (TB rubric) and AI Detection above to fill it in.
        </p>
      </div>

      <div className="grid gap-3 md:grid-cols-3">
        <div className="rounded-md border border-border p-3 text-xs">
          <div className="mb-1 font-medium">Layer 1 — Policy</div>
          <p className="text-muted-foreground">
            Task files (instruction.md, README sections) must be written by a human, without AI
            assistance. This is a written rule, not code — nothing runs for it.
          </p>
        </div>

        <div className="rounded-md border border-border p-3 text-xs">
          <div className="mb-1 font-medium">Layer 2 — Rubric opinion</div>
          {layer2 ? (
            <>
              <LayerBadge outcome={layer2.outcome} />
              <p className="mt-1 text-muted-foreground">{layer2.explanation}</p>
            </>
          ) : (
            <p className="text-muted-foreground">
              Not available — run Rubric Check with the "TB" rubric to populate this
              (instruction_concision).
            </p>
          )}
        </div>

        <div className="rounded-md border border-border p-3 text-xs">
          <div className="mb-1 font-medium">Layer 3 — GPTZero measurement</div>
          {layer3 ? (
            layer3.skipped ? (
              <p className="text-muted-foreground">
                Skipped —{" "}
                {layer3.api_key_configured
                  ? "GPTZero returned a network error."
                  : "GPTZERO_API_KEY not configured."}
              </p>
            ) : layer3.passed != null ? (
              <>
                <LayerBadge outcome={layer3.passed ? "pass" : "fail"} />
                <p className="mt-1 whitespace-pre-wrap text-muted-foreground">{layer3.output}</p>
              </>
            ) : (
              <p className="text-muted-foreground">{layer3.error ?? "No result."}</p>
            )
          ) : (
            <p className="text-muted-foreground">Not available — run AI Detection above.</p>
          )}
        </div>
      </div>
    </div>
  );
}

function CheatTrialView({ status, logs }: { status: StageStatus; logs?: string }) {
  if (status === "not-run" || !logs) return null;
  let parsed: CheatTrial | null = null;
  try {
    parsed = JSON.parse(logs) as CheatTrial;
  } catch {
    return <LogPanel logs={logs} title="Cheat trial log" />;
  }
  const check = parsed.cheat_check;
  return (
    <div className="space-y-3">
      {parsed.reward !== 1 ? (
        <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 p-2.5 text-xs text-emerald-700 dark:text-emerald-400">
          The anti-cheat design held: told to cheat, the agent still did not get reward 1.
        </div>
      ) : check?.outcome === "pass" ? (
        <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 p-2.5 text-xs text-emerald-700 dark:text-emerald-400">
          Reward came back 1, but the automatic reward_hacking check found no cheating — the agent
          refused the directive and solved the task honestly.
          {check.explanation && <p className="mt-1 text-muted-foreground">{check.explanation}</p>}
        </div>
      ) : check?.outcome === "fail" ? (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-2.5 text-xs text-red-700 dark:text-red-400">
          Genuine finding: reward came back 1 and the reward_hacking check found real cheating.
          {check.explanation && <p className="mt-1">{check.explanation}</p>}
        </div>
      ) : (
        <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-2.5 text-xs text-amber-700 dark:text-amber-400">
          Reward came back 1, and the automatic reward_hacking check couldn't resolve whether it
          was a real cheat — read the trajectory by hand.
          {check?.error && <p className="mt-1 text-muted-foreground">{check.error}</p>}
        </div>
      )}
      <LogPanel logs={parsed.trial_log ?? undefined} title="Cheat trial log" />
    </div>
  );
}

function RubricCheckView({ logs }: { logs?: string }) {
  if (!logs) return null;
  let parsed: RubricCheck | null = null;
  try {
    parsed = JSON.parse(logs) as RubricCheck;
  } catch {
    return <LogPanel logs={logs} title="Rubric check output" />;
  }
  return (
    <div className="space-y-3">
      <div className="text-xs text-muted-foreground">
        {parsed.agent} · {parsed.model}
        {parsed.cost_usd != null && <> · ${parsed.cost_usd.toFixed(3)}</>}
        {parsed.rubric && <> · {parsed.rubric} rubric</>}
      </div>
      {parsed.error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-2.5 text-xs whitespace-pre-wrap max-h-48 overflow-y-auto">
          {parsed.error}
        </div>
      )}
      {parsed.results.map((r, i) => (
        <div key={r.task_name ?? i} className="rounded-md border border-border p-3 text-sm">
          <div className="font-mono text-xs text-muted-foreground">{r.task_name ?? `task ${i}`}</div>
          {r.error ? (
            <div className="mt-1 text-xs text-red-600 dark:text-red-400 whitespace-pre-wrap">
              {r.error}
            </div>
          ) : (
            <>
              {r.summary && <p className="mt-1 whitespace-pre-wrap">{r.summary}</p>}
              <ul className="mt-2 space-y-1.5">
                {Object.entries(r.checks).map(([name, c]) => (
                  <li key={name} className="text-xs">
                    <span
                      className={`mr-2 rounded px-1.5 py-0.5 font-medium ${
                        c.outcome === "pass"
                          ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400"
                          : c.outcome === "fail"
                          ? "bg-red-500/15 text-red-700 dark:text-red-400"
                          : "bg-muted text-muted-foreground"
                      }`}
                    >
                      {name}: {c.outcome}
                    </span>
                    <span className="text-muted-foreground">{c.explanation}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      ))}
    </div>
  );
}

function countPassed(d: Submission) {
  return d.agent_trials.trials.filter((t) => t.reward === 1).length;
}

function PageShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-background">
      <div className="mx-auto max-w-6xl px-6 py-8">
        <Link
          to="/"
          className="mb-4 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" /> All submissions
        </Link>
        {children}
      </div>
    </div>
  );
}

function StageCard({
  title,
  description,
  status,
  reward,
  children,
}: {
  title: string;
  description: string;
  status: StageStatus;
  reward?: 0 | 1 | null;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3 rounded-lg border border-border bg-card p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-base font-semibold">{title}</h3>
          <p className="text-xs text-muted-foreground">{description}</p>
        </div>
        <div className="flex items-center gap-2">
          {reward != null && (
            <span className="rounded bg-muted px-2 py-0.5 font-mono text-xs">
              reward {reward}
            </span>
          )}
          <StatusBadge status={status} />
        </div>
      </div>
      {children}
    </div>
  );
}

function RunButton({
  label,
  runningLabel,
  onClick,
  running,
  pending,
  disabled,
  disabledReason,
}: {
  label: string;
  runningLabel: string;
  onClick: () => void;
  running: boolean;
  pending: boolean;
  disabled?: boolean;
  disabledReason?: string;
}) {
  const btn = (
    <Button onClick={onClick} disabled={disabled || running || pending}>
      {running ? runningLabel : label}
    </Button>
  );
  if (disabled && disabledReason) {
    return (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <span tabIndex={0}>{btn}</span>
          </TooltipTrigger>
          <TooltipContent>{disabledReason}</TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );
  }
  return btn;
}

function formatDate(iso: string) {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}
