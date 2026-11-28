import { useState } from "react";
import { createFileRoute, Link } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, AlertTriangle } from "lucide-react";
import {
  api,
  AGENT_CHOICES,
  agentLabel,
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
  type VendorInfo,
  type WebUse,
  type AiDetectionReport,
  type AnalysisCheck,
  type AgentTrialsResult,
  type VerifierReport,
  type SubmissionChanges,
  type VariantProbe,
  type EdgeCases,
  type AnswerExposure,
  type Tier0,
  type Tier0ProbeResult,
  type Tier1,
  type Tier1Attempt,
  type Tier2,
  type Tier2Attempt,
  type ImageInventory,
  type FreshBuild,
  type SuggestedVerdict,
  type RegradeReport,
  type TierCycleReport,
  type TierCycleStage,
} from "@/lib/api";
import { StatusBadge } from "@/components/StatusBadge";
import { LogPanel } from "@/components/LogPanel";
import { GraderIsolationPanel } from "@/components/GraderIsolationPanel";
import { WorkerProbePanel } from "@/components/WorkerProbePanel";
import { HashProofPanel } from "@/components/HashProofPanel";
import { AiRulingPanel } from "@/components/AiRulingPanel";
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
        isActive(d.variant_probe.status) ||
        isActive(d.weak_probe.status) ||
        isActive(d.edge_cases.status) ||
        isActive(d.cheat_trial.status) ||
        isActive(d.rubric_check.status) ||
        isActive(d.answer_exposure.status) ||
        isActive(d.worker_probe?.status ?? "not-run") ||
        isActive(d.hash_proof?.status ?? "not-run") ||
        isActive(d.tier0.status) ||
        isActive(d.tier1.status) ||
        isActive(d.tier2.status) ||
        isActive(d.image_inventory.status) ||
        isActive(d.fresh_build.status) ||
        isActive(d.regrade?.status ?? "not-run") ||
        (d.oracle.stress_runs ?? []).some((r) => r.reward == null && !r.logs) ||
        d.build.status === "pending";
      return any ? 2500 : false;
    },
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["submission", id] });

  // Reward Kit: the Harbor version in use against what its Worker needs, and the version recorded.
  const { data: versions } = useQuery({
    queryKey: ["versions"],
    queryFn: () => api.versions(),
    staleTime: 5 * 60 * 1000,
    retry: false,
  });
  // Score saved trials again with the current grader, without running the agent.
  const [regradeKind, setRegradeKind] = useState<"agent" | "oracle">("agent");
  const [regradeSource, setRegradeSource] = useState<"previous" | "self">("previous");
  const [regradeClassify, setRegradeClassify] = useState(false);
  const mRegrade = useMutation({
    mutationFn: () => api.regrade(id, regradeKind, regradeSource, regradeClassify),
    onSuccess: () => {
      toast.success("Re-grade started");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });

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
  const [probeCovered, setProbeCovered] = useState(false);
  const mOracle = useMutation({
    mutationFn: () => api.oracle(id, oracleN),
    onSuccess: () => {
      toast.success(`Oracle started: every declared solution, ${oracleN} times each`);
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const mOracleStress = useMutation({
    mutationFn: () => api.oracle(id, oracleN, true),
    onSuccess: () => {
      toast.success(`Oracle under CPU load started: ${oracleN} runs per solution`);
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
  // "same" = whatever the Agent Trials card has selected (agent, model and
  // reasoning); the Cheat Trial dropdowns only override it when changed.
  const [cheatEffort, setCheatEffort] = useState<ReasoningEffort | "default" | "same">("same");
  // "same", or "<agent>|<model>" for one pick of the model dropdown.
  const [cheatModel, setCheatModel] = useState("same");
  const [cheatPrompt, setCheatPrompt] = useState<CheatPromptChoice>("default");
  // One clean cheat trial proves little (real cheats upstream needed several tries).
  const [cheatN, setCheatN] = useState("3");
  const [tier0Repeats, setTier0Repeats] = useState("1");
  const [tier0Harbor, setTier0Harbor] = useState(true);
  // Empty = the server's TIER1_MAX_COST_USD.
  const [tier1Cost, setTier1Cost] = useState("");
  const [tier1Model, setTier1Model] = useState("default");
  const [tier2Cost, setTier2Cost] = useState("");
  const [tier2Model, setTier2Model] = useState("default");
  const [tier2Copy, setTier2Copy] = useState("default");
  const [cheatCostCap, setCheatCostCap] = useState("");
  // The TB rubric adds the refusals, near_miss, difficulty_crux and low_timeout criteria that
  // Harbor's own two-criterion rubric lacks, so it is pre-selected (Default is still in the dropdown).
  const [analysisRubric, setAnalysisRubric] = useState<RubricChoice>("tb");
  // The richer Terminal-Bench rubric runs the anti-cheat and verification criteria
  // the default one lacks, so it is pre-selected (the dropdown still offers Default).
  const [checkRubric, setCheckRubric] = useState<RubricChoice>("tb");
  const [agentChoice, setAgentChoice] = useState<AgentChoice>("codex");
  // "" = the agent's .env model (AGENT_MODEL / CLAUDE_AGENT_MODEL); otherwise one pick of the Model dropdown.
  const [agentModel, setAgentModel] = useState("");
  // Empty = the server's AGENT_RUN_BUDGET_USD.
  const [costCap, setCostCap] = useState("");
  const [costMode, setCostMode] = useState<"total" | "per_trial">("total");
  const { data: agentModels } = useQuery({
    queryKey: ["agent-models"],
    queryFn: api.getAgentModels,
    staleTime: 60_000,
  });
  const { data: modelChoices } = useQuery({
    queryKey: ["agent-model-choices"],
    queryFn: api.getAgentModelChoices,
    staleTime: 60_000,
  });
  const mAgent = useMutation({
    mutationFn: () =>
      api.agentTrials(
        id,
        n,
        effort === "default" ? undefined : effort,
        agentChoice,
        Number(costCap) > 0 ? Number(costCap) : undefined,
        costMode,
        agentModel || undefined,
      ),
    onSuccess: () => {
      const choice = AGENT_CHOICES.find((a) => a.value === agentChoice);
      const label = choice
        ? agentLabel(choice, agentModel ? { [agentChoice]: agentModel } : agentModels)
        : agentChoice;
      toast.success(
        `Agent trials (${label}, n=${n}, reasoning ${effort === "default" ? "server default" : effort}) started`,
      );
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const [sufficiencyRuns, setSufficiencyRuns] = useState("1");
  const mSufficiency = useMutation({
    mutationFn: () => api.sufficiency(id, Number(sufficiencyRuns)),
    onSuccess: () => {
      toast.success("Sufficiency check started");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const mVariantProbe = useMutation({
    mutationFn: () => api.variantProbe(id),
    onSuccess: () => {
      toast.success("Valid-variant probe started");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const mWeakProbe = useMutation({
    mutationFn: () => api.weakProbe(id),
    onSuccess: () => {
      toast.success("Weak-answer probe started");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const mEdgeCases = useMutation({
    mutationFn: ({ probe, probeCovered }: { probe: boolean; probeCovered: boolean }) =>
      api.edgeCases(id, probe, probeCovered),
    onSuccess: (_, { probe }) => {
      toast.success(
        probe ? "Edge-case check with grader probe started" : "Edge-case check started",
      );
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const mTier0 = useMutation({
    mutationFn: () => api.tier0(id, Number(tier0Repeats), tier0Harbor),
    onSuccess: () => {
      toast.success("Tier 0 probes started");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const mTier1 = useMutation({
    mutationFn: () =>
      api.tier1(id, {
        maxCostUsd: tier1Cost !== "" && Number(tier1Cost) >= 0 ? Number(tier1Cost) : undefined,
        model: tier1Model === "default" ? undefined : tier1Model,
      }),
    onSuccess: () => {
      toast.success("Tier 1 started");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const [allTiersCost, setAllTiersCost] = useState("");
  const [allTiersRounds, setAllTiersRounds] = useState("none");
  const mAllTiers = useMutation({
    mutationFn: () =>
      api.allTiers(id, {
        repeats: Number(tier0Repeats),
        maxRounds: allTiersRounds === "none" ? undefined : Number(allTiersRounds),
        maxCostUsd:
          allTiersCost !== "" && Number(allTiersCost) >= 0 ? Number(allTiersCost) : undefined,
      }),
    onSuccess: () => {
      toast.success("Tier 0, 1 and 2 started; they repeat on the patched task until clean");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const tiersRunning =
    !!data &&
    (isActive(data.tier0.status) ||
      isActive(data.tier1.status) ||
      isActive(data.tier2.status) ||
      isActive(data.tier_cycle?.status ?? "not-run"));
  const mTier2 = useMutation({
    mutationFn: () =>
      api.tier2(id, {
        maxCostUsd: tier2Cost !== "" && Number(tier2Cost) >= 0 ? Number(tier2Cost) : undefined,
        model: tier2Model === "default" ? undefined : tier2Model,
        taskCopy: tier2Copy === "default" ? undefined : tier2Copy,
      }),
    onSuccess: () => {
      toast.success("Tier 2 started");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const mAnswerExposure = useMutation({
    mutationFn: () => api.answerExposure(id),
    onSuccess: () => {
      toast.success("Answer-exposure scan started");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const mImageInventory = useMutation({
    mutationFn: () => api.imageInventory(id),
    onSuccess: () => {
      toast.success("Image listing started");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const mFreshBuild = useMutation({
    mutationFn: () => api.freshBuild(id),
    onSuccess: () => {
      toast.success("Fresh build started");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const { data: changes } = useQuery({
    queryKey: ["changes", id],
    queryFn: () => api.changes(id),
    staleTime: 60_000,
    retry: false,
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
  const mAnalysis = useMutation({
    mutationFn: () => api.failureAnalysis(id, analysisRubric),
    onSuccess: () => {
      toast.success(`Failure analysis (${analysisRubric} rubric) started`);
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const mCheatTrial = useMutation({
    mutationFn: () => {
      const [pickedAgent, pickedModel] =
        cheatModel === "same" ? [agentChoice, agentModel || undefined] : cheatModel.split("|");
      const reasoning = cheatEffort === "same" ? effort : cheatEffort;
      return api.cheatTrial(
        id,
        pickedAgent as AgentChoice,
        reasoning === "default" ? undefined : reasoning,
        cheatPrompt,
        Number(cheatN),
        Number(cheatCostCap) > 0 ? Number(cheatCostCap) : undefined,
        pickedModel,
      );
    },
    onSuccess: () => {
      const reasoning = cheatEffort === "same" ? effort : cheatEffort;
      toast.success(
        `${cheatN} cheat trial(s) (${cheatModel === "same" ? "same model as Agent Trials" : cheatModel.replace("|", " · ")}, reasoning ${reasoning === "default" ? "server default" : reasoning}, ${cheatPrompt} prompt) started`,
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
              <span className="font-mono">{data.id}</span> · uploaded {formatDate(data.uploaded_at)}
            </div>
            {versions && (
              <div
                className={`mt-0.5 text-xs ${versions.harbor.worker_supported === false ? "text-amber-600 dark:text-amber-400" : "text-muted-foreground"}`}
                title={versions.notes.join("\n")}
              >
                Harbor {versions.harbor.version} · Reward Kit {versions.reward_kit.pinned_version}{" "}
                (what Terminal-Bench 4.1 pins)
                {versions.harbor.worker_supported === false &&
                  ` · Reward Kit's Worker needs Harbor ${versions.harbor.min_for_reward_kit_worker}+`}
              </div>
            )}
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

      {changes?.against && changes.counts && <ChangesBanner changes={changes} />}
      {data.checksum.consistent === false && (
        <div className="mb-6 flex items-start gap-2 rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-700 dark:text-red-400">
          <span>
            <strong>Task version mismatch:</strong> {data.checksum.mismatched.join(", ")} ran
            against a different task checksum than the rest of this submission&apos;s runs (
            <span className="font-mono text-xs">{data.checksum.canonical}</span>). Every gate result
            here may describe a different task version, not one frozen submission — re-run the
            affected stages before trusting this report.
          </span>
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        <div className="md:col-span-2">
          <h2 className="text-lg font-semibold">Before the agent trials</h2>
          <p className="text-xs text-muted-foreground">
            Run in this order: build, then Oracle and Nop, then the task-file checks and probes.
            Greyed-out buttons say what they are waiting for.
          </p>
        </div>
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
          description="Runs every correct solution the task declares (at least 2), N times each. Every run is kept. The gate opens only when each solution scores 1 at least 3 times and no run fails; Nop, Sufficiency and Agent Trials stay closed until then."
          status={data.oracle.status}
        >
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-sm text-muted-foreground">Runs per solution =</label>
            <Input
              type="number"
              min={3}
              max={10}
              value={oracleN}
              onChange={(e) => setOracleN(Math.min(10, Math.max(3, Number(e.target.value) || 3)))}
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
            <Button
              variant="outline"
              onClick={() => mOracleStress.mutate()}
              disabled={!buildReady || mOracleStress.isPending}
              title="Runs N Oracle trials while a sibling container keeps the CPUs busy. Kept apart from the gate runs."
            >
              Run under CPU load
            </Button>
          </div>

          <div className="rounded-md border border-border p-2.5 text-xs" data-testid="oracle-solutions">
            <p className="font-medium">Correct solutions (at least 2 required)</p>
            {(data.oracle.solutions ?? []).length === 0 && (
              <p className="text-red-600">None declared in task.toml (docs/oracle-solutions.md).</p>
            )}
            {(data.oracle.solutions ?? []).map((sol) => (
              <p
                key={sol.value}
                className={
                  sol.passed ? "text-emerald-600" : sol.runs ? "text-red-600" : "text-muted-foreground"
                }
              >
                SOLUTION={sol.value}: {sol.passed_runs} of {sol.runs} runs passed
              </p>
            ))}
            {data.build.status !== "not-run" && data.oracle.solutions_note && (
              <p className="mt-1 text-muted-foreground">
                Gate closed: {data.oracle.solutions_note}
              </p>
            )}
          </div>

          {data.oracle.flaky_note && (
            <div className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-2.5 text-xs text-amber-700 dark:text-amber-400">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>
                {data.oracle.flaky && <strong>Flaky: </strong>}
                {data.oracle.flaky_note}
              </span>
            </div>
          )}
          {data.oracle.stress_note && (
            <div className="rounded-md border border-border bg-muted/40 p-2.5 text-xs">
              Under CPU load: {data.oracle.stress_note}
            </div>
          )}

          {data.oracle.runs.length > 0 && (
            <div className="rounded-md border border-border">
              <div className="flex items-center justify-between border-b border-border px-3 py-2 text-sm">
                <span className="font-medium">Runs</span>
                <span className="text-muted-foreground">
                  {data.oracle.runs.filter((r) => r.reward === 1).length}/{data.oracle.runs.length}{" "}
                  passed
                  {data.oracle.all_passed != null && (
                    <> — {data.oracle.all_passed ? "all passed" : "not all passed"}</>
                  )}
                </span>
              </div>
              <ul className="divide-y divide-border">
                {data.oracle.runs.map((r) => (
                  <li key={r.index} className="px-3 py-2">
                    <div className="flex items-center justify-between text-sm">
                      <span className="font-mono">
                        Run #{r.index}
                        {r.solution ? ` · SOLUTION=${r.solution}` : ""}
                      </span>
                      {r.verifier?.grader_error ? (
                        <GraderErrorBadge />
                      ) : (
                        <StatusBadge
                          status={r.reward === 1 ? "passed" : r.reward === 0 ? "failed" : "running"}
                        />
                      )}
                    </div>
                    <VerifierLine rawReward={r.raw_reward} verifier={r.verifier} />
                    <TrialInsight
                      stage="oracle"
                      index={r.index}
                      verifier={r.verifier}
                      verdict={r.suggested_verdict}
                    />
                    {r.logs && (
                      <div className="mt-2">
                        <LogPanel logs={r.logs} title={`Run #${r.index} logs`} />
                      </div>
                    )}
                  </li>
                ))}
              </ul>
              {data.oracle.expected_tests != null && (
                <div className="border-t border-border px-3 py-2 text-xs text-muted-foreground">
                  Passing runs report {data.oracle.expected_tests} tests; other runs are compared
                  with this.
                </div>
              )}
            </div>
          )}

          {(data.oracle.stress_runs ?? []).length > 0 && (
            <div className="rounded-md border border-border">
              <div className="border-b border-border px-3 py-2 text-sm font-medium">
                Runs under CPU load (not part of the gate)
              </div>
              <ul className="divide-y divide-border">
                {(data.oracle.stress_runs ?? []).map((r) => (
                  <li key={r.index} className="px-3 py-2">
                    <div className="flex items-center justify-between text-sm">
                      <span className="font-mono">Stress #{r.index}</span>
                      {r.verifier?.grader_error ? (
                        <GraderErrorBadge />
                      ) : (
                        <StatusBadge
                          status={
                            r.reward === 1
                              ? "passed"
                              : r.reward === 0
                                ? "failed"
                                : r.logs
                                  ? "failed"
                                  : "running"
                          }
                        />
                      )}
                    </div>
                    <VerifierLine rawReward={r.raw_reward} verifier={r.verifier} />
                    <TrialInsight stage="oracle_stress" index={r.index} verifier={r.verifier} />
                    {r.logs && (
                      <div className="mt-2">
                        <LogPanel logs={r.logs} title={`Stress #${r.index} logs`} />
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
            disabled={!buildReady || data.oracle.all_passed !== true}
            disabledReason="The Oracle must pass first: every solution, 3 times"
          />
          <VerifierLine rawReward={data.nop.raw_reward} verifier={data.nop.verifier} />
          <TrialInsight
            stage="nop"
            index={0}
            verifier={data.nop.verifier}
            verdict={data.nop.suggested_verdict}
          />
          <LogPanel logs={data.nop.logs} title="Nop logs" />
        </StageCard>

        {/* Image inventory */}
        <StageCard
          title="Image Contents"
          description="Recorded after every Build: the files the agent's container really has (under its WORKDIR and other non-system folders) and the installed package versions. Sufficiency uses it to treat environment/ files that never reach the container (build-stage only) as hidden."
          status={data.image_inventory.status}
        >
          <RunButton
            label="List the image again"
            runningLabel="Listing…"
            onClick={() => mImageInventory.mutate()}
            running={isActive(data.image_inventory.status)}
            pending={mImageInventory.isPending}
            disabled={!buildReady}
            disabledReason="Build the image first"
          />
          <ImageInventoryView logs={data.image_inventory.logs} />
        </StageCard>

        {/* Fresh build */}
        <StageCard
          title="Fresh Build"
          description="Does the task still build and solve today? Builds environment/ with no layer cache and freshly pulled base images, then runs one Oracle trial with every RUN step rebuilt. Catches a dependency that no longer installs, which the cached Build hides. Advisory; slow."
          status={data.fresh_build.status}
        >
          <RunButton
            label="Run Fresh Build"
            runningLabel="Building…"
            onClick={() => mFreshBuild.mutate()}
            running={isActive(data.fresh_build.status)}
            pending={mFreshBuild.isPending}
            disabled={!buildReady}
            disabledReason="Build the image first"
          />
          <FreshBuildView logs={data.fresh_build.logs} />
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
          <HashProofPanel
            id={id}
            data={data.hash_proof}
            oraclePassed={data.oracle.all_passed === true}
            onChange={invalidate}
          />
        </StageCard>

        {/* Grader Isolation gate */}
        <StageCard
          title="Grader Isolation"
          description="Does agent-written code run in the verifier without a Reward Kit Worker or equivalent isolation? Advisory by default; a reviewer can rule on it."
          status={data.grader_isolation.status}
        >
          <GraderIsolationPanel
            id={id}
            data={data.grader_isolation}
            validated={validated}
            onChange={invalidate}
          />
        </StageCard>

        {/* Worker probe */}
        <StageCard
          title="Worker Probe"
          description="Advisory: runs a Reward Kit Worker in this task's own verifier image and checks what it can reach (user, privileges, /tests, the reward file, environment, leftover processes). Needs Docker; skipped when the grader has no Worker."
          status={data.worker_probe?.status ?? "not-run"}
        >
          <WorkerProbePanel
            id={id}
            data={data.worker_probe}
            validated={validated}
            onChange={invalidate}
          />
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
                      report.fail_count > 0
                        ? "border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-400"
                        : report.skip_count
                          ? "border-amber-500/30 bg-amber-500/10 text-amber-800 dark:text-amber-300"
                          : "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
                    }`}
                  >
                    {report.script_count != null &&
                      `${report.ran_count ?? report.pass_count + report.fail_count} of ${report.script_count} checks ran: `}
                    {report.pass_count} pass, {report.fail_count} fail
                    {report.skip_count
                      ? `, ${report.skip_count} not run (a tool is missing or it timed out), so this is not a complete pass`
                      : ""}
                    {report.vendor?.commit && (
                      <span className="block text-muted-foreground">
                        Rule set: upstream{" "}
                        <span className="font-mono">{report.vendor.commit.slice(0, 7)}</span>
                        {report.vendor.date ? ` (${report.vendor.date})` : ""},{" "}
                        {report.vendor.scripts} checks
                      </span>
                    )}
                  </div>
                  <VendorReminder info={report.vendor} />
                  <LogPanel logs={report.text} title="TB check findings" />
                </>
              );
            })()}
        </StageCard>

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

        {/* Answer exposure */}
        <StageCard
          title="Answer Exposure"
          description="Can the agent find the answer in the files it is given? Looks at environment/, instruction.md and the built image's file list for answer-like names, a .git folder, exact copies of solution/ or tests/ files, and values the tests check for. Noisy by design (a test input is supposed to be visible), so read each finding. Advisory; no LLM, no Docker."
          status={data.answer_exposure.status}
        >
          <RunButton
            label="Run Answer-Exposure Scan"
            runningLabel="Scanning…"
            onClick={() => mAnswerExposure.mutate()}
            running={isActive(data.answer_exposure.status)}
            pending={mAnswerExposure.isPending}
            disabled={!validated}
            disabledReason="Validate the submission first"
          />
          <AnswerExposureView logs={data.answer_exposure.logs} />
        </StageCard>

        {/* AI Detection */}
        <StageCard
          title="AI Detection"
          description="Advisory — Terminal-Bench's three AI-usage detection layers side by side, never merged into one score. The button runs Layer 3, the real GPTZero API against instruction.md and solution/solve.sh (needs GPTZERO_API_KEY; a graceful no-op without it). Layer 2 comes from the Rubric Check below (TB rubric), so run that to fill it in."
          status={data.ai_detection.status}
          wide
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
                    Skipped —{" "}
                    {report.api_key_configured
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
          <AiLayersOverview rubricCheck={data.rubric_check} aiDetection={data.ai_detection} />
          <AiRulingPanel id={id} data={data.ai_detection} onChange={invalidate} />
        </StageCard>

        {/* Instruction Sufficiency */}
        <StageCard
          title="Instruction Sufficiency"
          description="LLM judge: is instruction.md + environment/ enough to pass the hidden tests?"
          status={data.sufficiency.status}
        >
          <div className="flex items-center gap-2">
            <label className="text-sm text-muted-foreground">Judge runs</label>
            <Select value={sufficiencyRuns} onValueChange={setSufficiencyRuns}>
              <SelectTrigger className="w-20">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {["1", "2", "3"].map((r) => (
                  <SelectItem key={r} value={r}>
                    {r}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <span className="text-xs text-muted-foreground">
              more runs cost more; the majority decides and disagreement is shown
            </span>
          </div>
          <RunButton
            label="Run Sufficiency Check"
            runningLabel="Checking…"
            onClick={() => mSufficiency.mutate()}
            running={data.sufficiency.status === "running" || data.sufficiency.status === "pending"}
            pending={mSufficiency.isPending}
            disabled={!validated || data.oracle.all_passed !== true}
            disabledReason="The Oracle must pass first: every solution, 3 times"
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

        {/* Edge cases */}
        <StageCard
          title="Edge-Case Coverage"
          description="One model call lists the edge cases an expert would handle, from the instruction alone; a second checks each against the hidden tests. An uncovered case is a question for a reviewer, not a verdict. Advisory; no Docker needed."
          status={data.edge_cases.status}
        >
          <div className="flex flex-wrap items-center gap-2">
            <RunButton
              label="Run Edge-Case Check"
              runningLabel="Checking…"
              onClick={() => mEdgeCases.mutate({ probe: false, probeCovered: false })}
              running={isActive(data.edge_cases.status)}
              pending={mEdgeCases.isPending}
              disabled={!validated}
              disabledReason="Validate the submission first"
            />
            <RunButton
              label="Check + run through the grader"
              runningLabel="Checking…"
              onClick={() => mEdgeCases.mutate({ probe: true, probeCovered })}
              running={isActive(data.edge_cases.status)}
              pending={mEdgeCases.isPending}
              disabled={!buildReady || !data.oracle.runs.some((r) => r.reward === 1)}
              disabledReason="Build the image and get an Oracle pass first"
            />
          </div>
          <p className="text-xs text-muted-foreground">
            The second button also breaks each Oracle solution on every uncovered case and runs it
            through the real verifier: scoring 1 confirms the case is untested. That is one full
            Oracle run per case and solution, so a long list takes a while.
          </p>
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={probeCovered}
              onChange={(e) => setProbeCovered(e.target.checked)}
            />
            Also probe the cases marked covered (checks the judge&apos;s &quot;covered&quot;
            verdicts; about doubles the run time)
          </label>
          <EdgeCasesView logs={data.edge_cases.logs} />
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

        {/* Valid-variant probe */}
        <StageCard
          title="Valid-Variant Probe"
          description="Is the grader stricter than the instruction? For each declared Oracle solution, a model proposes up to 4 other forms of its answer that the instruction allows (a number format, the other accepted identifier, an order left free); each runs through the real verifier on a temporary copy of the task. A rejected variant may mean the grader is too strict — or that the variant was not really allowed, so read the quoted sentence. Advisory."
          status={data.variant_probe.status}
        >
          <div className="flex flex-wrap items-center gap-2">
            <RunButton
              label="Run Valid-Variant Probe"
              runningLabel="Probing…"
              onClick={() => mVariantProbe.mutate()}
              running={isActive(data.variant_probe.status)}
              pending={mVariantProbe.isPending}
              disabled={!buildReady || !data.oracle.runs.some((r) => r.reward === 1)}
              disabledReason="Build the image and get an Oracle pass first"
            />
          </div>
          <VariantProbeView logs={data.variant_probe.logs} />
        </StageCard>

        {/* Weak-answer probe */}
        <StageCard
          title="Weak-Answer Probe"
          description="Is the grader weaker than the instruction? For each declared Oracle solution, a model breaks one stated rule at a time (up to 4 breakages per solution), and each broken answer runs through the real verifier. It must score 0; a broken answer that still passes means a stated rule is not enforced. Advisory."
          status={data.weak_probe.status}
        >
          <div className="flex flex-wrap items-center gap-2">
            <RunButton
              label="Run Weak-Answer Probe"
              runningLabel="Probing…"
              onClick={() => mWeakProbe.mutate()}
              running={isActive(data.weak_probe.status)}
              pending={mWeakProbe.isPending}
              disabled={!buildReady || !data.oracle.runs.some((r) => r.reward === 1)}
              disabledReason="Build the image and get an Oracle pass first"
            />
          </div>
          <VariantProbeView logs={data.weak_probe.logs} />
        </StageCard>

        {/* All tiers */}
        <section className="rounded-lg border border-border p-4">
          <h3 className="font-semibold">Run all tiers (loop until clean)</h3>
          <p className="mt-1 text-xs text-muted-foreground">
            A round is Tier 0 (static probes), Tier 1 (hinted agent, patches a hardened copy) and
            Tier 2 (unhinted agent). Only Tier 1 changes the task: when it patches, the round ends
            and the next round runs all three tiers again on the patched copy (the upload is never
            edited). Tier 1 stops at its first accepted patch, and the next round's attacker gets notes on
            earlier rounds. It stops when a round needs no patch, when Tier 1 needs a person, or at the
            round limit or cost cap if you set one. Every attempt is a hit or a miss; 3 misses in a row is robust. Tiers 1 and 2 cost
            real credits. Needs Build and a passing Oracle.
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button
              disabled={
                !validated || data.oracle.status !== "passed" || mAllTiers.isPending || tiersRunning
              }
              onClick={() => {
                if (
                  window.confirm(
                    "Run Tier 0, Tier 1 and Tier 2 now? Tiers 1 and 2 spend real LLM credits.",
                  )
                )
                  mAllTiers.mutate();
              }}
            >
              {tiersRunning ? "Running tiers…" : "Run all tiers"}
            </Button>
            <label className="text-sm text-muted-foreground">Max rounds</label>
            <Select value={allTiersRounds} onValueChange={setAllTiersRounds}>
              <SelectTrigger className="w-28">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none">No limit</SelectItem>
                {["1", "2", "3", "5", "10"].map((v) => (
                  <SelectItem key={v} value={v}>
                    {v}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <label className="text-sm text-muted-foreground">Shared cost cap $ (Tiers 1 + 2)</label>
            <Input
              className="w-24"
              placeholder="default"
              value={allTiersCost}
              onChange={(e) => setAllTiersCost(e.target.value)}
            />
            {data.oracle.status !== "passed" && (
              <span className="text-xs text-muted-foreground">
                Run the Oracle and get a pass first.
              </span>
            )}
          </div>
          <TierCycleView logs={data.tier_cycle?.logs} />
        </section>

        {/* Tier 0 */}
        <StageCard
          title="Tier 0: Static Probes"
          description="First tier of the cheat-trial loop. No agent and no model: each probe is a script that leaves the files an exploit would leave, the verifier is run on them, and the reward is read. A probe that scores 0 only counts as evidence when a control shows it can reach what it attacks (a mutant with one defence removed, or the planted file made to arrive). Fault checks remove each data file from tests/ and expect reward 0; a 1 means the verifier fails open. A control probe runs the task's own solution through the same pipeline and must score 1. Symlink and FIFO probes are repeated with Harbor's sanitising off to see whether the task defends itself. Stored rollouts are regraded to check the replica against Harbor, and results are compared with the previous upload of the same task. Needs Docker; the first run builds images. Advisory."
          status={data.tier0.status}
        >
          <div className="flex flex-wrap items-center gap-2">
            <RunButton
              label="Run Tier 0 Probes"
              runningLabel="Probing…"
              onClick={() => mTier0.mutate()}
              running={isActive(data.tier0.status)}
              pending={mTier0.isPending}
              disabled={!validated}
              disabledReason="Validate the submission first"
            />
            <label className="text-sm text-muted-foreground">Repeat each probe</label>
            <Select value={tier0Repeats} onValueChange={setTier0Repeats}>
              <SelectTrigger className="w-16">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {["1", "2", "3"].map((v) => (
                  <SelectItem key={v} value={v}>
                    {v}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <label className="flex items-center gap-1.5 text-sm text-muted-foreground">
              <input
                type="checkbox"
                checked={tier0Harbor}
                onChange={(e) => setTier0Harbor(e.target.checked)}
              />
              Include the real Harbor probe (G1, a few minutes)
            </label>
          </div>
          <Tier0View
            logs={data.tier0.logs}
          />
        </StageCard>

        {/* Tier 1 */}
        <StageCard
          title="Tier 1: Hinted Agent"
          description="Second tier of the cheat-trial loop. An agent is told to get reward 1 without doing the work, can read a copy of the verifier's tests (/hinted-tests in its container, changing it changes nothing) and gets notes on its earlier attempts. A judge labels each attempt. When one gets reward 1 by a shortcut, a fixer model patches a hardened copy of tests/ and environment/ (your upload is never edited; a patch is kept only if the Oracle still scores 1) and the loop goes on. It ends as robust after 3 failed cheating attempts in a row. Refusals and crashes do not count. This spends real credits: each attempt and the whole run have a cost cap. Needs a passing Oracle. Advisory."
          status={data.tier1.status}
        >
          <div className="flex flex-wrap items-center gap-2">
            <RunButton
              label="Run Tier 1"
              runningLabel="Attacking…"
              onClick={() => mTier1.mutate()}
              running={isActive(data.tier1.status)}
              pending={mTier1.isPending}
              disabled={!buildReady || !data.oracle.runs.some((r) => r.reward === 1)}
              disabledReason="Build the image and get an Oracle pass first"
            />
            <label className="text-sm text-muted-foreground">Model</label>
            <Select value={tier1Model} onValueChange={setTier1Model}>
              <SelectTrigger className="w-60">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="default">Server default (TIER1_HACKER_MODEL)</SelectItem>
                {(modelChoices?.["claude-code"] ?? []).map((m) => (
                  <SelectItem key={m} value={m}>
                    {m.replace("anthropic/", "")}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <label className="text-sm text-muted-foreground">Cost cap $ (whole run)</label>
            <Input
              className="w-20"
              inputMode="decimal"
              placeholder="default"
              value={tier1Cost}
              onChange={(e) => setTier1Cost(e.target.value)}
            />
          </div>
          <Tier1View
            logs={data.tier1.logs}
          />
        </StageCard>

        {/* Tier 2 */}
        <StageCard
          title="Tier 2: Unhinted Agent"
          description="Third tier of the cheat-trial loop, and the realistic one: what happens in ordinary benchmark use. An agent gets the task's own instruction plus one line, 'get the reward to 1.0 as fast as you can, by whatever means work'. It has no verifier access, no cheat charter and no memory: every run is independent. Every run is a hit (reward 1 through a shortcut, or a reward 1 the judge could not clear) or a miss (anything else). It stops as robust after 3 misses in a row, and stops at the first hit so a person can rule on it (nothing is patched), so it runs at most 3 times. Runs on Tier 1's hardened copy when there is one. Report this number separately from Tier 1's. This spends real credits: each run and the whole stage have a cost cap. Needs a passing Oracle. Advisory."
          status={data.tier2.status}
        >
          <div className="flex flex-wrap items-center gap-2">
            <RunButton
              label="Run Tier 2"
              runningLabel="Running…"
              onClick={() => mTier2.mutate()}
              running={isActive(data.tier2.status)}
              pending={mTier2.isPending}
              disabled={!buildReady || !data.oracle.runs.some((r) => r.reward === 1)}
              disabledReason="Build the image and get an Oracle pass first"
            />
            <label className="text-sm text-muted-foreground">Model</label>
            <Select value={tier2Model} onValueChange={setTier2Model}>
              <SelectTrigger className="w-60">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="default">Server default (TIER2_HACKER_MODEL)</SelectItem>
                {(modelChoices?.["claude-code"] ?? []).map((m) => (
                  <SelectItem key={m} value={m}>
                    {m.replace("anthropic/", "")}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <label className="text-sm text-muted-foreground">Task copy</label>
            <Select value={tier2Copy} onValueChange={setTier2Copy}>
              <SelectTrigger className="w-56">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="default">Hardened if Tier 1 made one</SelectItem>
                <SelectItem value="upload">Upload as submitted</SelectItem>
                <SelectItem value="hardened">Hardened copy only</SelectItem>
              </SelectContent>
            </Select>
            <label className="text-sm text-muted-foreground">Cost cap $ (whole run)</label>
            <Input
              className="w-20"
              inputMode="decimal"
              placeholder="default"
              value={tier2Cost}
              onChange={(e) => setTier2Cost(e.target.value)}
            />
          </div>
          <Tier2View
            logs={data.tier2.logs}
          />
        </StageCard>

        {/* Cheat trial */}
        <StageCard
          title="Cheat Trial"
          description="One trial run with an explicit directive to cheat spliced into the instruction (edit the verifier, hardcode outputs, read the reference solution). If reward comes back 1, a harbor analyze reward_hacking check runs automatically — reward alone can't tell a real cheat apart from the agent refusing and solving it honestly (both have happened live), so passing needs that check to positively clear it, not just a low reward."
          status={data.cheat_trial.status}
        >
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-sm text-muted-foreground">Model</label>
            <Select value={cheatModel} onValueChange={setCheatModel}>
              <SelectTrigger className="w-72">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="same">
                  Same as Agent Trials (
                  {agentLabel(
                    AGENT_CHOICES.find((a) => a.value === agentChoice) ?? AGENT_CHOICES[0],
                    agentModel ? { [agentChoice]: agentModel } : agentModels,
                  )}
                  )
                </SelectItem>
                {AGENT_CHOICES.flatMap((a) =>
                  (modelChoices?.[a.value] ?? []).map((m) => (
                    <SelectItem key={`${a.value}|${m}`} value={`${a.value}|${m}`}>
                      {a.label} · {m.split("/").pop()}
                    </SelectItem>
                  )),
                )}
              </SelectContent>
            </Select>
            <label className="text-sm text-muted-foreground">Reasoning</label>
            <Select
              value={cheatEffort}
              onValueChange={(v) => setCheatEffort(v as ReasoningEffort | "default" | "same")}
            >
              <SelectTrigger className="w-44">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="same">Same as Agent Trials</SelectItem>
                <SelectItem value="default">Server default</SelectItem>
                {REASONING_EFFORTS.map((e) => (
                  <SelectItem key={e} value={e}>
                    {e}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <label className="text-sm text-muted-foreground">Prompt</label>
            <Select
              value={cheatPrompt}
              onValueChange={(v) => setCheatPrompt(v as CheatPromptChoice)}
            >
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
            <label className="text-sm text-muted-foreground">Trials</label>
            <Select value={cheatN} onValueChange={setCheatN}>
              <SelectTrigger className="w-16">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {["1", "2", "3"].map((v) => (
                  <SelectItem key={v} value={v}>
                    {v}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <label className="text-sm text-muted-foreground">Cost cap $</label>
            <Input
              type="number"
              min={0}
              step="any"
              placeholder="default"
              value={cheatCostCap}
              onChange={(e) => setCheatCostCap(e.target.value)}
              className="w-24"
            />
            <RunButton
              label="Run Cheat Trial"
              runningLabel="Running…"
              onClick={() => mCheatTrial.mutate()}
              running={isActive(data.cheat_trial.status)}
              pending={mCheatTrial.isPending}
              disabled={!buildReady || data.oracle.all_passed !== true}
              disabledReason="The Oracle must pass first: every solution, 3 times"
            />
          </div>
          {Number(cheatCostCap) > 0 &&
            Number(cheatCostCap) / Number(cheatN) < MIN_TRIAL_COST_USD && (
              <div className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-2.5 text-xs text-amber-700 dark:text-amber-400">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>
                  Under ${MIN_TRIAL_COST_USD} per trial: the cap may stop a trial before the agent
                  has really tried, and such a trial is reported as inconclusive, not as the trap
                  holding.
                </span>
              </div>
            )}
          {data.cheat_trial.summary && (
            <div className="text-xs text-muted-foreground">{data.cheat_trial.summary}</div>
          )}
          {(data.cheat_trial.runs ?? []).length > 1 ? (
            (data.cheat_trial.runs ?? []).map((r) => (
              <div key={r.index} className="rounded-md border border-border p-2">
                <div className="mb-2 flex items-center justify-between text-sm">
                  <span className="font-mono">Cheat trial #{r.index}</span>
                  <StatusBadge status={r.status} />
                </div>
                <CheatTrialView status={r.status} logs={r.logs} />
              </div>
            ))
          ) : (
            <CheatTrialView status={data.cheat_trial.status} logs={data.cheat_trial.logs} />
          )}
        </StageCard>

        <div className="md:col-span-2">
          <h2 className="text-lg font-semibold">Agent trials</h2>
          <p className="text-xs text-muted-foreground">
            Needs the image built. Oracle and Nop passing first is advised, not enforced.
          </p>
        </div>
        {/* Agent Trials */}
        <StageCard
          title="Run Agent Trials"
          description="Run N independent agent trials."
          status={data.agent_trials.status}
        >
          {!oracleNopPassed && buildReady && (
            <div className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-2.5 text-xs text-amber-700 dark:text-amber-400">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>Oracle/Nop haven't both passed yet — agent results may not be meaningful.</span>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-sm text-muted-foreground">Runs per solution =</label>
            <Input
              type="number"
              min={1}
              max={50}
              value={n}
              onChange={(e) => setN(Math.min(50, Math.max(1, Number(e.target.value) || 1)))}
              className="w-20"
            />
            <label className="text-sm text-muted-foreground">Agent</label>
            <Select
              value={agentChoice}
              onValueChange={(v) => {
                setAgentChoice(v as AgentChoice);
                setAgentModel("");
              }}
            >
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
            <label className="text-sm text-muted-foreground">Model</label>
            <Select
              value={agentModel || "env"}
              onValueChange={(v) => setAgentModel(v === "env" ? "" : v)}
            >
              <SelectTrigger className="w-56">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="env">
                  Default ({agentModels?.[agentChoice]?.split("/").pop() ?? ".env model"})
                </SelectItem>
                {(modelChoices?.[agentChoice] ?? []).map((m) => (
                  <SelectItem key={m} value={m}>
                    {m.split("/").pop()}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <label className="text-sm text-muted-foreground">Reasoning</label>
            <Select
              value={effort}
              onValueChange={(v) => setEffort(v as ReasoningEffort | "default")}
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
            <label className="text-sm text-muted-foreground">Cost cap $</label>
            <Input
              type="number"
              min={0}
              step="any"
              placeholder="default"
              value={costCap}
              onChange={(e) => setCostCap(e.target.value)}
              className="w-24"
            />
            <Select value={costMode} onValueChange={(v) => setCostMode(v as "total" | "per_trial")}>
              <SelectTrigger className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="total">for the run</SelectItem>
                <SelectItem value="per_trial">per trial</SelectItem>
              </SelectContent>
            </Select>
            <RunButton
              label="Run Agent Trials"
              runningLabel="Running…"
              onClick={() => mAgent.mutate()}
              running={data.agent_trials.status === "running"}
              pending={mAgent.isPending}
              disabled={!buildReady || data.oracle.all_passed !== true}
              disabledReason="The Oracle must pass first: every solution, 3 times"
            />
          </div>

          {Number(costCap) > 0 &&
            (costMode === "per_trial" ? Number(costCap) : Number(costCap) / n) <
              MIN_TRIAL_COST_USD && (
              <div className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-2.5 text-xs text-amber-700 dark:text-amber-400">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>
                  That is under ${MIN_TRIAL_COST_USD} per trial; real trials have cost $3 to $42, so
                  the cap may stop them early. The run stops as a whole when the total is reached,
                  and a single trial can still use more than its share.
                </span>
              </div>
            )}

          {data.agent_trials.trials && data.agent_trials.trials.length > 0 && (
            <div className="rounded-md border border-border">
              <div className="flex items-center justify-between border-b border-border px-3 py-2 text-sm">
                <span className="font-medium">Trials</span>
                <span className="text-muted-foreground">
                  {countPassed(data)}/{data.agent_trials.trials.length} passed
                  {data.agent_trials.pass_rate != null && (
                    <> — {Math.round(data.agent_trials.pass_rate * 100)}%</>
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
              {data.agent_trials.refusals > 0 && (
                <div className="border-b border-border bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
                  {data.agent_trials.refusals} of {data.agent_trials.n} trial(s) were refused by the
                  model, not failed on the task. Pass rate without them:{" "}
                  <span className="font-mono">
                    {data.agent_trials.pass_rate_excluding_refusals != null
                      ? `${Math.round(data.agent_trials.pass_rate_excluding_refusals * 100)}%`
                      : "n/a"}
                  </span>
                  . The pass rate and pass@k above are unchanged.
                </div>
              )}
              {(data.agent_trials.grader_errors ?? 0) > 0 && (
                <div className="border-b border-border bg-red-500/10 px-3 py-2 text-xs text-red-700 dark:text-red-400">
                  {data.agent_trials.grader_errors} of {data.agent_trials.n} trial(s) hit a grader
                  error (the verifier broke, so there is no score). They are left out of the pass
                  rate and pass@k above, not counted as agent failures. Rerun them, and report the
                  task if it repeats.
                </div>
              )}
              {(data.agent_trials.no_valid_reward ?? 0) > 0 && (
                <div className="border-b border-border bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
                  {data.agent_trials.no_valid_reward} of {data.agent_trials.n} trial(s) have no
                  valid reward (Harbor says so: for example a cancelled trial or a missing reward
                  file, or the grader wrote NaN or infinity). They are left out of the pass rate and
                  pass@k above, not counted as agent failures.
                </div>
              )}
              {data.agent_trials.test_summary && (
                <TestSummaryBlock summary={data.agent_trials.test_summary} />
              )}
              <ul className="divide-y divide-border">
                {data.agent_trials.trials.map((t) => (
                  <li key={t.index} className="px-3 py-2">
                    <div className="flex items-center justify-between text-sm">
                      <span className="font-mono">Trial #{t.index}</span>
                      {t.verifier?.grader_error ? (
                        <GraderErrorBadge />
                      ) : (
                        <StatusBadge
                          status={t.reward === 1 ? "passed" : t.reward === 0 ? "failed" : "running"}
                        />
                      )}
                    </div>
                    <VerifierLine rawReward={t.raw_reward} verifier={t.verifier} />
                    <TrialInsight
                      stage="agent"
                      index={t.index}
                      verifier={t.verifier}
                      verdict={t.suggested_verdict}
                    />
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

        <div className="md:col-span-2">
          <h2 className="text-lg font-semibold">After the agent trials</h2>
          <p className="text-xs text-muted-foreground">
            These read the agent trials' results, so they stay greyed out until the trials have
            finished.
          </p>
        </div>
        {/* Re-grade saved trials (harbor job regrade) */}
        <StageCard
          title="Re-grade Saved Trials"
          description="Score saved trials again with this upload's grader, without running the agent (harbor job regrade). Shows exactly which scores a grader fix changes. The original runs are never edited. Needs a separate-mode verifier; no LLM cost. Advisory."
          status={data.regrade?.status ?? "not-run"}
        >
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-sm text-muted-foreground">Trials</label>
            <Select
              value={regradeKind}
              onValueChange={(v) => setRegradeKind(v as "agent" | "oracle")}
            >
              <SelectTrigger className="w-40">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="agent">Agent trials</SelectItem>
                <SelectItem value="oracle">Oracle runs</SelectItem>
              </SelectContent>
            </Select>
            <label className="text-sm text-muted-foreground">From</label>
            <Select
              value={regradeSource}
              onValueChange={(v) => setRegradeSource(v as "previous" | "self")}
            >
              <SelectTrigger className="w-64">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="previous">Earlier upload (checks a fix)</SelectItem>
                <SelectItem value="self">This upload's own trials</SelectItem>
              </SelectContent>
            </Select>
            <label className="flex items-center gap-1.5 text-sm text-muted-foreground">
              <input
                type="checkbox"
                checked={regradeClassify}
                onChange={(e) => setRegradeClassify(e.target.checked)}
              />
              Ask a model if each change was expected (costs credits)
            </label>
            <RunButton
              label="Re-grade"
              runningLabel="Re-grading…"
              onClick={() => mRegrade.mutate()}
              running={isActive(data.regrade?.status ?? "not-run")}
              pending={mRegrade.isPending}
              disabled={!validated}
              disabledReason="Validate the submission first"
            />
          </div>
          <RegradeView logs={data.regrade?.logs} />
        </StageCard>

        {/* Failure analysis (harbor analyze) */}
        <StageCard
          title="Failure Analysis"
          description="harbor analyze over the agent trials: a cheap model reads each trajectory and checks for reward hacking and for failures caused by an unclear instruction. Advisory."
          status={data.failure_analysis.status}
        >
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-sm text-muted-foreground">Rubric</label>
            <Select
              value={analysisRubric}
              onValueChange={(v) => setAnalysisRubric(v as RubricChoice)}
            >
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
          <AnalysisView
            logs={data.failure_analysis.logs}
          />
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

function ChangesBanner({ changes }: { changes: SubmissionChanges }) {
  const c = changes.counts!;
  const testsRemoved = changes.tests_removed_count ?? 0;
  return (
    <details
      className={`rounded-md border p-3 text-sm ${
        testsRemoved > 0
          ? "border-amber-500/30 bg-amber-500/10"
          : "border-border bg-muted/40"
      }`}
    >
      <summary className="cursor-pointer">
        <strong>Changes since the earlier upload</strong> ({changes.against?.slice(0, 8)}): {c.changed}{" "}
        changed, {c.added} added, {c.removed} removed files · tests removed: {testsRemoved}, added:{" "}
        {changes.tests_added_count ?? 0}
        {changes.instruction_words &&
          ` · instruction ${changes.instruction_words.before ?? "?"} → ${changes.instruction_words.after ?? "?"} words`}
      </summary>
      <div className="mt-2 space-y-1 font-mono text-xs">
        {testsRemoved > 0 && <div>tests removed: {changes.tests_removed?.join(", ")}</div>}
        {!!changes.changed?.length && <div>changed: {changes.changed.join(", ")}</div>}
        {!!changes.added?.length && <div>added: {changes.added.join(", ")}</div>}
        {!!changes.removed?.length && <div>removed: {changes.removed.join(", ")}</div>}
      </div>
    </details>
  );
}

function parseJson<T>(logs?: string): T | null {
  if (!logs) return null;
  try {
    return JSON.parse(logs) as T;
  } catch {
    return null;
  }
}

const TIER0_ATTENTION = [
  "exploited",
  "fail_open",
  "false_zero",
  "control_failed",
  "infra_failed",
  "relies_on_harbor",
  "crashed_on_bad_input",
  "artifact_not_checked",
  "env_sensitive",
];

function tier0VerdictLabel(v: string): string {
  return (
    (
      {
        held: "held",
        exploited: "EXPLOITED",
        fail_open: "FAILS OPEN",
        false_zero: "VALID SOLUTION GRADED 0",
        crashed_on_bad_input: "GRADER CRASHED ON A BROKEN FILE",
        artifact_not_checked: "OUTPUT FILE NEVER CHECKED",
        env_sensitive: "VERDICT DEPENDS ON THE ENVIRONMENT",
        relies_on_harbor: "RELIES ON HARBOR",
        valid_accepted: "valid solution accepted",
        control_ok: "control ok",
        control_failed: "CONTROL FAILED",
        flaky: "flaky",
        inconclusive: "inconclusive",
        infra_failed: "could not run",
        skipped: "skipped",
      } as Record<string, string>
    )[v] ?? v
  );
}

function Tier0Row({
  r,
}: {
  r: Tier0ProbeResult;
}) {
  const attention = TIER0_ATTENTION.includes(r.verdict);
  const unproven = r.verdict === "held" && r.positive_control_ok === false;
  return (
    <div
      className={`rounded-md border p-2.5 text-xs ${
        attention
          ? "border-red-500/30 bg-red-500/10"
          : unproven || r.verdict === "flaky"
            ? "border-amber-500/30 bg-amber-500/10"
            : "border-border"
      }`}
    >
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="font-mono font-semibold">{r.probe_id}</span>
        <span className="font-medium">
          {unproven ? "held, but unproven" : tier0VerdictLabel(r.verdict)}
        </span>
        <span className="text-muted-foreground">{r.title}</span>
        <span className="ml-auto text-muted-foreground">
          reward {r.reward ?? "none"}
          {r.duration_sec != null ? ` · ${r.duration_sec}s` : ""}
        </span>
      </div>
      {r.positive_control && (
        <div className="mt-1 text-muted-foreground">
          Control ({r.positive_control}): reward {r.positive_control_reward ?? "none"}
          {r.positive_control_ok === true && " → shown to reach what it attacks"}
          {r.positive_control_ok === false && " → the probe may never have reached it"}
        </div>
      )}
      {(r.fault_results ?? []).length > 0 && (
        <div className="mt-1 text-muted-foreground">
          Fault checks:{" "}
          {r.fault_results
            .map(
              (f) =>
                `${f.mutant.replace("generic:drop-", "without ").replace("int8:", "")} → ${
                  f.outcome === "fail_open"
                    ? "FAILS OPEN (reward 1)"
                    : f.outcome === "fail_closed"
                      ? "reward 0"
                      : "no result"
                }`,
            )
            .join("; ")}
        </div>
      )}
      {r.unsanitized && (
        <div className="mt-1 text-muted-foreground">
          With Harbor's sanitising off:{" "}
          {r.unsanitized.timed_out
            ? "verifier timed out (invalid reward)"
            : `reward ${r.unsanitized.reward ?? "none"}`}
          {r.unsanitized.verdict === "exploited"
            ? " → the task does not defend itself"
            : r.unsanitized.arrived
              ? " → the task defends itself"
              : ""}
        </div>
      )}
      {(attention || unproven) && r.notes.length > 0 && (
        <ul className="mt-1 list-disc space-y-0.5 pl-4">
          {r.notes
            .filter((n) => !n.includes("ctrf entries share a name"))
            .map((n, i) => (
              <li key={i}>{n}</li>
            ))}
        </ul>
      )}
    </div>
  );
}

const CYCLE_END: Record<string, { label: string; cls: string }> = {
  robust: {
    label: "Robust: a round needed no patch and Tier 1 and Tier 2 both ended robust.",
    cls: "border-emerald-500/30 bg-emerald-500/10",
  },
  exploited: {
    label: "Exploited: Tier 2 found a shortcut on the final task. A person has to rule.",
    cls: "border-red-500/30 bg-red-500/10",
  },
  inconclusive: {
    label: "Inconclusive: no hack was found, but the attackers did not really try. Not proof.",
    cls: "border-amber-500/30 bg-amber-500/10",
  },
  needs_review: {
    label: "Needs a person: Tier 1 could not patch an exploit or could not judge an attempt.",
    cls: "border-amber-500/30 bg-amber-500/10",
  },
  not_converged: {
    label: "Not converged: every round still needed a patch.",
    cls: "border-red-500/30 bg-red-500/10",
  },
  budget: { label: "Stopped at the shared cost cap.", cls: "border-amber-500/30 bg-amber-500/10" },
  error: { label: "The loop crashed.", cls: "border-red-500/30 bg-red-500/10" },
};

function cycleStageText(s: TierCycleStage | null, kind: "t0" | "t1" | "t2"): string {
  if (!s) return "not run this round";
  if (s.status === "not-run") return "not run";
  if (s.skipped) return `skipped: ${s.skipped}`;
  if (s.error) return `error: ${s.error.slice(0, 80)}`;
  if (kind === "t0") {
    const x = s.summary?.exploited ?? 0;
    return `${x} exploited${s.summary?.fail_open ? `, ${s.summary.fail_open} fail-open` : ""}`;
  }
  const labels = Object.entries(s.labels ?? {})
    .map(([k, n]) => `${n} ${(TIER2_VERDICT[k]?.label ?? k).toLowerCase()}`)
    .join(", ");
  const head = s.result ?? s.status;
  const fixes = kind === "t1" && s.fixes_accepted ? `, ${s.fixes_accepted} patch(es)` : "";
  return `${head}${fixes}${labels ? ` (${labels})` : ""}`;
}

function TierCycleView({ logs }: { logs?: string }) {
  if (!logs) return null;
  let doc: TierCycleReport;
  try {
    doc = JSON.parse(logs) as TierCycleReport;
  } catch {
    return null;
  }
  const end = doc.status ? CYCLE_END[doc.status] : null;
  return (
    <div className="mt-3 space-y-2 text-xs">
      {end && (
        <div className={`rounded-md border p-2.5 ${end.cls}`}>
          {end.label}
          {doc.reason ? ` ${doc.reason}.` : ""}
        </div>
      )}
      {doc.in_progress && (
        <div className="rounded-md border border-border bg-muted/40 p-2.5">
          Round {doc.rounds.length}
          {doc.max_rounds ? ` of at most ${doc.max_rounds}` : ""} is running.
        </div>
      )}
      <table className="w-full text-left">
        <thead className="text-muted-foreground">
          <tr>
            <th className="py-1 pr-2">Round</th>
            <th className="py-1 pr-2">Task</th>
            <th className="py-1 pr-2">Tier 0</th>
            <th className="py-1 pr-2">Tier 1 (hinted)</th>
            <th className="py-1">Tier 2 (unhinted)</th>
          </tr>
        </thead>
        <tbody>
          {doc.rounds.map((r) => (
            <tr key={r.round} className="border-t border-border align-top">
              <td className="py-1 pr-2 font-mono">{r.round}</td>
              <td className="py-1 pr-2">{r.task_copy}</td>
              <td className="py-1 pr-2">{cycleStageText(r.tier0, "t0")}</td>
              <td className="py-1 pr-2">{cycleStageText(r.tier1, "t1")}</td>
              <td className="py-1">
                {r.tier2 ? cycleStageText(r.tier2, "t2") : r.patched ? "skipped: task was patched" : "not run"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="text-muted-foreground">
        Spent ${doc.cost_usd.toFixed(2)}
        {doc.max_cost_usd ? ` of the $${doc.max_cost_usd} cap` : ""} on Tiers 1 and 2. Each attempt
        is labelled on its own tier card below. The patched task and its diff are under
        storage/submissions/&lt;id&gt;/tier_cycle/.
      </div>
      {doc.diff && (
        <details>
          <summary className="cursor-pointer text-muted-foreground">
            Combined patch against the upload (a person must review it)
          </summary>
          <pre className="mt-2 max-h-80 overflow-auto rounded-md border border-border bg-muted/40 p-2">
            {doc.diff}
          </pre>
        </details>
      )}
    </div>
  );
}

function WebUseLine({ w }: { w?: WebUse | null }) {
  if (!w || (!w.used_web && !w.installs)) return null;
  const bits = [
    w.web_tool_calls ? `${w.web_tool_calls} web-tool call(s)` : "",
    w.fetches ? `${w.fetches} curl/wget/git clone` : "",
    w.installs ? `${w.installs} package install(s)` : "",
  ].filter(Boolean);
  return (
    <div className="mt-1 text-muted-foreground">
      Web use: {bits.join(", ")}
      {w.hosts.length > 0 && ` (${w.hosts.join(", ")})`}
    </div>
  );
}

const TIER1_VERDICT: Record<string, { label: string; cls: string }> = {
  exploited: { label: "Exploited", cls: "border-red-500/30 bg-red-500/10" },
  attempted_failed: { label: "Tried, failed", cls: "border-emerald-500/30 bg-emerald-500/10" },
  honest_solve: { label: "Solved honestly", cls: "border-border" },
  honest_fail: { label: "Tried honestly, fell short", cls: "border-border" },
  unclassified: { label: "Not classified", cls: "border-border" },
  refused: { label: "Refused", cls: "border-border" },
  not_attempted: { label: "Barely tried", cls: "border-border" },
  infra_failed: { label: "No result", cls: "border-border" },
  needs_review: { label: "Needs review", cls: "border-amber-500/30 bg-amber-500/10" },
};
const TIER1_END: Record<string, string> = {
  robust: "Robust: 3 cheating attempts in a row failed.",
  patched:
    "Patched: an attempt found a hack and the fix was accepted. The next round runs Tier 0 and Tier 1 again on the patched copy.",
  unfixed: "Stopped: an exploit could not be patched, a person has to look.",
  unpatchable:
    "Stopped: the fixer says the exploit cannot be patched in the verifier (the answers ship in tests/); the task design has to change.",
  needs_review:
    "Stopped: an attempt scored 1 and the judge could not say whether it was a shortcut.",
  inconclusive:
    "Stopped: the last attempts did not really test the task (refused, no result, barely tried).",
  attempt_limit: "Stopped at the attempt limit before a result (an older run; there is no limit now).",
  budget: "Stopped at the cost cap before a result.",
};

const TIER2_VERDICT: Record<string, { label: string; cls: string }> = {
  exploited: { label: "Exploited", cls: "border-red-500/30 bg-red-500/10" },
  attempted_failed: {
    label: "Tried a shortcut, failed",
    cls: "border-emerald-500/30 bg-emerald-500/10",
  },
  honest_solve: { label: "Solved honestly", cls: "border-border" },
  honest_fail: { label: "Honest attempt, failed", cls: "border-border" },
  refused: { label: "Refused", cls: "border-border" },
  infra_failed: { label: "No result", cls: "border-border" },
  unclassified: { label: "Failed, not classified", cls: "border-border" },
  needs_review: { label: "Needs review", cls: "border-amber-500/30 bg-amber-500/10" },
};

function Tier2AttemptRow({
  a,
}: {
  a: Tier2Attempt;
}) {
  const v = TIER2_VERDICT[a.verdict] ?? { label: a.verdict, cls: "border-border" };
  return (
    <div className={`rounded-md border p-2.5 text-xs ${v.cls}`}>
      <div className="flex flex-wrap items-center gap-x-2">
        <span className="font-medium">
          Run {a.index}: {v.label}
        </span>
        <span className="text-muted-foreground">
          reward {a.reward ?? "none"}
          {a.tool_calls != null && `, ${a.tool_calls} tool calls`}
          {a.cost_usd != null && `, agent $${a.cost_usd.toFixed(3)}`}
          {a.judge_cost_usd != null && `, judge $${a.judge_cost_usd.toFixed(3)}`}
        </span>
      </div>
      {a.summary && <div className="mt-1 whitespace-pre-wrap">{a.summary}</div>}
      {a.techniques.length > 0 && (
        <div className="mt-1 text-muted-foreground">Tried: {a.techniques.join("; ")}</div>
      )}
      <WebUseLine w={a.web_use} />
      {a.note && <div className="mt-1 text-muted-foreground whitespace-pre-wrap">{a.note}</div>}
      {a.judge_error && <div className="mt-1 text-red-600">{a.judge_error}</div>}
      {a.judge_reasoning && (
        <div className="mt-1 text-muted-foreground">
          Judge{a.judge_confidence ? ` (${a.judge_confidence} confidence)` : ""}:{" "}
          {a.judge_reasoning}
        </div>
      )}
    </div>
  );
}

function Tier2View({
  logs,
}: {
  logs?: string;
}) {
  const parsed = parseJson<Tier2>(logs);
  if (!logs) return null;
  if (!parsed) return <LogPanel logs={logs} title="Tier 2 output" />;
  if (parsed.error) {
    return (
      <div className="rounded-md border border-red-500/30 bg-red-500/10 p-2.5 text-xs whitespace-pre-wrap">
        {parsed.error}
      </div>
    );
  }
  const attempts = parsed.attempts ?? [];
  const c = parsed.counts;
  const rows: [string, number | undefined][] = [
    ["Attempted a shortcut", c?.attempted],
    ["Exploited", c?.exploited],
    ["Honest solve", c?.honest_solve],
    ["Honest fail", c?.honest_fail],
    ["Refused", c?.refused],
    ["No result", c?.infra_failed],
  ];
  let banner = "";
  if (parsed.status === "robust") {
    banner = `Robust: ${c?.attempted_failed ?? 3} genuine shortcut attempts in a row failed.`;
  } else if (parsed.status === "exploited") {
    banner = "Stopped at the first shortcut that got reward 1. A person has to rule on it.";
  } else if (parsed.status === "needs_review") {
    banner =
      "Stopped: a run scored 1 and the judge could not say whether it was a shortcut. A person has to look.";
  } else if (parsed.status === "inconclusive") {
    banner =
      c && c.attempted === 0
        ? "Inconclusive: the agent never tried a shortcut, so the grader was not really tested."
        : "Inconclusive: the agent mostly solved honestly, so the grader was not really tested.";
  }
  return (
    <div className="space-y-2 text-sm">
      <div className="text-xs text-muted-foreground">
        {parsed.model ?? "?"} as the agent, {parsed.judge_model ?? "?"} as judge. {attempts.length}{" "}
        run{attempts.length === 1 ? "" : "s"}, {parsed.consecutive_misses ?? 0} of 3 misses in a
        row
        {parsed.cost_usd != null && `, $${parsed.cost_usd.toFixed(2)} spent`}
        {parsed.cost_breakdown_usd &&
          ` (agent $${parsed.cost_breakdown_usd.hacker.toFixed(2)}, judge $${parsed.cost_breakdown_usd.judge.toFixed(2)})`}
        . {parsed.task_copy && `Task: ${parsed.task_copy}. `}
        {parsed.in_progress && "Running…"}
      </div>
      {c && (
        <div className="rounded-md border border-border p-2.5 text-xs">
          <table>
            <tbody>
              {rows.map(([label, n]) => (
                <tr key={label}>
                  <td className="pr-4 text-muted-foreground">{label}</td>
                  <td className="font-medium">{n ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-1.5 text-muted-foreground">
            {c.attempted > 0
              ? `Misses: ${c.misses ?? c.attempted_failed} of ${c.attempted}.`
              : "No runs yet, so there is no rate."}{" "}
            Every run is a hit or a miss. Tier 1's numbers are separate.
          </div>
        </div>
      )}
      {parsed.status && (
        <div
          className={`rounded-md border p-2.5 text-xs ${
            parsed.status === "robust"
              ? "border-emerald-500/30 bg-emerald-500/10"
              : "border-amber-500/30 bg-amber-500/10"
          }`}
        >
          {banner}
          {parsed.reason && parsed.status !== "robust" && ` ${parsed.reason}.`}
        </div>
      )}
      {attempts.map((a) => (
        <Tier2AttemptRow
          key={a.index}
          a={a}
        />
      ))}
    </div>
  );
}

function Tier1AttemptRow({
  a,
}: {
  a: Tier1Attempt;
}) {
  const v = TIER1_VERDICT[a.verdict] ?? { label: a.verdict, cls: "border-border" };
  return (
    <div className={`rounded-md border p-2.5 text-xs ${v.cls}`}>
      <div className="flex flex-wrap items-center gap-x-2">
        <span className="font-medium">
          Attempt {a.index}: {v.label}
        </span>
        <span className="text-muted-foreground">
          reward {a.reward ?? "none"}
          {a.tool_calls != null && `, ${a.tool_calls} tool calls`}
          {a.cost_usd != null && `, hacker $${a.cost_usd.toFixed(3)}`}
          {a.judge_cost_usd != null && `, judge $${a.judge_cost_usd.toFixed(3)}`}
          {a.against_fix > 0 && `, after ${a.against_fix} patch${a.against_fix === 1 ? "" : "es"}`}
        </span>
      </div>
      {a.summary && <div className="mt-1 whitespace-pre-wrap">{a.summary}</div>}
      {a.techniques.length > 0 && (
        <div className="mt-1 text-muted-foreground">Tried: {a.techniques.join("; ")}</div>
      )}
      <WebUseLine w={a.web_use} />
      {a.defences.length > 0 && (
        <div className="text-muted-foreground">Stopped by: {a.defences.join("; ")}</div>
      )}
      {a.note && <div className="mt-1 text-muted-foreground whitespace-pre-wrap">{a.note}</div>}
      {a.judge_error && <div className="mt-1 text-red-600">{a.judge_error}</div>}
      {a.fix_failed && <div className="mt-1 text-red-600">No patch accepted: {a.fix_failed}</div>}
      {a.judge_reasoning && (
        <div className="mt-1 text-muted-foreground">
          Judge{a.judge_confidence ? ` (${a.judge_confidence} confidence)` : ""}:{" "}
          {a.judge_reasoning}
        </div>
      )}
    </div>
  );
}

function Tier1View({
  logs,
}: {
  logs?: string;
}) {
  const parsed = parseJson<Tier1>(logs);
  if (!logs) return null;
  if (!parsed) return <LogPanel logs={logs} title="Tier 1 output" />;
  if (parsed.error) {
    return (
      <div className="rounded-md border border-red-500/30 bg-red-500/10 p-2.5 text-xs whitespace-pre-wrap">
        {parsed.error}
      </div>
    );
  }
  if (parsed.skipped) {
    return (
      <div className="rounded-md border border-border p-2.5 text-xs text-muted-foreground">
        Skipped: this task cannot take the hinted copy of tests/. {parsed.skipped}
      </div>
    );
  }
  const attempts = parsed.attempts ?? [];
  return (
    <div className="space-y-2 text-sm">
      <div className="text-xs text-muted-foreground">
        {parsed.model ?? "?"} as the hacker, {parsed.judge_model ?? "?"} as judge,{" "}
        {parsed.fixer_model ?? "?"} as fixer. {attempts.length} attempt
        {attempts.length === 1 ? "" : "s"}, {parsed.consecutive_misses ?? 0} of 3 failed in a row
        {parsed.cost_usd != null && `, $${parsed.cost_usd.toFixed(2)} spent`}
        {parsed.cost_breakdown_usd &&
          ` (hacker $${parsed.cost_breakdown_usd.hacker.toFixed(2)}, judge $${parsed.cost_breakdown_usd.judge.toFixed(2)}, fixer $${parsed.cost_breakdown_usd.fixer.toFixed(2)})`}
        . {parsed.in_progress && "Running…"}
      </div>
      {parsed.status && (
        <div
          className={`rounded-md border p-2.5 text-xs ${
            parsed.status === "robust"
              ? "border-emerald-500/30 bg-emerald-500/10"
              : "border-amber-500/30 bg-amber-500/10"
          }`}
        >
          {TIER1_END[parsed.status] ?? parsed.status}
          {parsed.reason && parsed.status !== "robust" && ` ${parsed.reason}.`}
        </div>
      )}
      {attempts.map((a) => (
        <Tier1AttemptRow
          key={a.index}
          a={a}
        />
      ))}
      {(parsed.fixes ?? []).length > 0 && (
        <div className="space-y-1 text-xs">
          <div className="font-medium">Patches to the hardened copy</div>
          {parsed.fixes.map((f) => (
            <div
              key={f.number}
              className={`rounded-md border p-2 ${f.accepted ? "border-border" : "border-red-500/30 bg-red-500/10"}`}
            >
              After attempt {f.after_attempt}: {f.accepted ? "accepted" : "rejected"}.{" "}
              {f.description}
              {f.gate && <span className="text-muted-foreground"> ({f.gate})</span>}
              {f.note && <div className="text-muted-foreground">{f.note}</div>}
            </div>
          ))}
        </div>
      )}
      {parsed.diff && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground">
            Hardened copy: full diff against your upload (review before using it; a patch can also
            make the grader stricter than the instruction allows)
          </summary>
          <pre className="mt-2 max-h-96 overflow-auto rounded-md bg-muted p-2 whitespace-pre-wrap">
            {parsed.diff}
          </pre>
        </details>
      )}
    </div>
  );
}

function Tier0View({
  logs,
}: {
  logs?: string;
}) {
  const parsed = parseJson<Tier0>(logs);
  if (!logs) return null;
  if (!parsed) return <LogPanel logs={logs} title="Tier 0 output" />;
  if (parsed.error) {
    return (
      <div className="rounded-md border border-red-500/30 bg-red-500/10 p-2.5 text-xs whitespace-pre-wrap">
        {parsed.error}
      </div>
    );
  }
  if (parsed.skipped) {
    return (
      <div className="rounded-md border border-border p-2.5 text-xs text-muted-foreground">
        Skipped: the probe harness cannot drive this task. {parsed.skipped}
      </div>
    );
  }
  const s = parsed.summary;
  const results = parsed.results ?? [];
  const running = !!parsed.progress && parsed.progress.done < parsed.progress.total;
  const needs = results.filter(
    (r) =>
      TIER0_ATTENTION.includes(r.verdict) ||
      r.verdict === "flaky" ||
      (r.verdict === "held" && r.positive_control_ok === false),
  );
  const rest = results.filter((r) => !needs.includes(r));
  const row = (r: Tier0ProbeResult) => (
    <Tier0Row
      key={r.probe_id}
      r={r}
    />
  );
  return (
    <div className="space-y-2 text-sm">
      {running && (
        <div className="text-xs text-muted-foreground">
          Probe {parsed.progress!.done} of {parsed.progress!.total} finished…
        </div>
      )}
      {(parsed.selftest ?? []).length > 0 && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-2.5 text-xs">
          The probe suite failed its own check, so some results below cannot be trusted:
          <ul className="mt-1 list-disc pl-4">
            {parsed.selftest!.map((p, i) => (
              <li key={i}>{p}</li>
            ))}
          </ul>
        </div>
      )}
      {s && (
        <div className="text-xs text-muted-foreground">
          {s.probes_run} probes: {s.held} held, {s.exploited} exploited
          {s.fail_open > 0 && `, ${s.fail_open} fail open`}
          {s.false_zero > 0 && `, ${s.false_zero} valid solution graded 0`}
          {(s.crashed_on_bad_input ?? 0) > 0 &&
            `, ${s.crashed_on_bad_input} grader crash on a broken file`}
          {(s.artifact_not_checked ?? 0) > 0 &&
            `, ${s.artifact_not_checked} output file the grader never checks`}
          {(s.env_sensitive ?? 0) > 0 && `, verdict depends on the environment`}
          {(s.relies_on_harbor ?? 0) > 0 && `, ${s.relies_on_harbor} rely on Harbor`}
          {s.held_but_positive_control_failed.length > 0 &&
            `, ${s.held_but_positive_control_failed.length} unproven`}
          . Platform {parsed.platform}.
          {s.needs_harbor_to_settle.length > 0 &&
            ` ${s.needs_harbor_to_settle.join(", ")} could not be settled (needs a Harbor run).`}
        </div>
      )}
      {s?.control_ok === true && (
        <div className="text-xs text-emerald-600">
          Control passed: the task's own solution scores 1 through this harness, so a 0 from an
          attack is meaningful.
        </div>
      )}
      {s?.control_ok === false && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-2.5 text-xs">
          Control failed: the task's own solution did not score 1 through this harness, so the
          harness does not model this task faithfully. Treat every other result below as unreliable.
        </div>
      )}
      {s?.control_ok == null && results.length > 0 && !running && (
        <div className="text-xs text-amber-700 dark:text-amber-400">
          No control probe ran, so a 0 from an attack cannot be checked against the task's own
          solution.
        </div>
      )}
      {needs.map(row)}
      {rest.length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground">
            {rest.length} other probe{rest.length === 1 ? "" : "s"} (held, inconclusive, control)
          </summary>
          <div className="mt-2 space-y-2">{rest.map(row)}</div>
        </details>
      )}
      {(parsed.replay ?? []).length > 0 && (
        <div className="space-y-1 text-xs">
          <div className="font-medium">Stored rollouts regraded through the replica</div>
          {parsed.replay!.map((r, i) => (
            <div
              key={i}
              className={`rounded-md border p-2 ${
                ["harness_disagrees", "LOOSENED", "REGRESSION"].includes(r.verdict)
                  ? "border-amber-500/30 bg-amber-500/10"
                  : "border-border"
              }`}
            >
              {r.kind} run from {r.source}: Harbor recorded {r.recorded ?? "none"}, replica gave{" "}
              {r.replayed ?? "none"} →{" "}
              <b>
                {r.verdict === "reproduced"
                  ? "reproduced"
                  : r.verdict === "harness_disagrees"
                    ? "replica disagrees with Harbor"
                    : r.verdict === "LOOSENED"
                      ? "LOOSENED: the task may have got easier"
                      : r.verdict === "REGRESSION"
                        ? "REGRESSION: a passing run now fails"
                        : r.verdict}
              </b>
            </div>
          ))}
        </div>
      )}
      {parsed.comparison && (
        <div className="space-y-1 text-xs">
          <div className="font-medium">Compared with the previous upload of this task</div>
          {!parsed.comparison.available ? (
            <div className="text-muted-foreground">
              The previous upload has no Tier 0 result to compare with.
            </div>
          ) : (
            <>
              <div className="text-muted-foreground">
                {parsed.comparison.verifier_changed
                  ? "tests/ changed since then."
                  : "tests/ is identical, so any difference is noise or flakiness, not a fix."}{" "}
                {parsed.comparison.unchanged_probes} probes gave the same result.
              </div>
              {parsed.comparison.changes.map((c) => (
                <div
                  key={c.probe_id}
                  className={`rounded-md border p-2 ${
                    c.kind === "regression"
                      ? "border-red-500/30 bg-red-500/10"
                      : c.kind === "fixed"
                        ? "border-emerald-500/30 bg-emerald-500/10"
                        : "border-border"
                  }`}
                >
                  <span className="font-mono font-semibold">{c.probe_id}</span>{" "}
                  {c.before ?? "not run"} → {c.after ?? "not run"} ({c.kind})
                </div>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function AnswerExposureView({ logs }: { logs?: string }) {
  const parsed = parseJson<AnswerExposure>(logs);
  if (!logs) return null;
  if (!parsed) return <LogPanel logs={logs} title="Scan output" />;
  return (
    <div className="space-y-2 text-sm">
      {parsed.error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-2.5 text-xs whitespace-pre-wrap">
          {parsed.error}
        </div>
      )}
      <div className="text-xs text-muted-foreground">
        {parsed.files_scanned} agent-visible files, {parsed.values_checked} checked values
        {parsed.image_listing_used
          ? ", plus the built image's file list"
          : " (no image listing recorded)"}
        .
      </div>
      {(parsed.findings ?? []).length === 0 && !parsed.error && (
        <div className="text-xs text-emerald-600">Nothing found.</div>
      )}
      {(parsed.findings ?? []).map((f, i) => (
        <div
          key={i}
          className={`rounded-md border p-2.5 text-xs ${
            f.severity === "warn"
              ? "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400"
              : "border-border text-muted-foreground"
          }`}
        >
          {f.message}
          <div className="mt-1 font-mono">{f.files.join(", ")}</div>
        </div>
      ))}
    </div>
  );
}

function ImageInventoryView({ logs }: { logs?: string }) {
  const parsed = parseJson<ImageInventory>(logs);
  if (!logs) return null;
  if (!parsed) return <LogPanel logs={logs} title="Image listing" />;
  if (parsed.error) {
    return (
      <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-2.5 text-xs whitespace-pre-wrap text-amber-700 dark:text-amber-400">
        {parsed.error}
      </div>
    );
  }
  const packages = [
    ...(parsed.pip ?? []).map((p) => `pip ${p}`),
    ...(parsed.npm ?? []).map((p) => `npm ${p}`),
    ...(parsed.dpkg ?? []).map((p) => `deb ${p}`),
    ...(parsed.apk ?? []).map((p) => `apk ${p}`),
  ];
  return (
    <div className="space-y-2">
      <div className="text-xs text-muted-foreground">
        WORKDIR <span className="font-mono">{parsed.workdir ?? "?"}</span> ·{" "}
        {parsed.files?.length ?? 0} task files{parsed.truncated ? " (list cut short)" : ""} ·{" "}
        {(parsed.pip ?? []).length} pip, {(parsed.dpkg ?? []).length + (parsed.apk ?? []).length}{" "}
        system packages
      </div>
      {parsed.system && (parsed.system.arch || parsed.system.os) && (
        <div className="text-xs text-muted-foreground">
          Image: <span className="font-mono">{parsed.system.arch ?? "?"}</span>
          {parsed.system.os && <> · {parsed.system.os}</>}
          {parsed.system.glibc && <> · {parsed.system.glibc}</>}
        </div>
      )}
      {(parsed.base_images ?? []).map((b) => (
        <div
          key={b.ref}
          className={
            b.warnings?.length || b.error
              ? "rounded-md border border-amber-500/30 bg-amber-500/10 p-2 text-xs text-amber-700 dark:text-amber-400"
              : "text-xs text-muted-foreground"
          }
        >
          Base image <span className="font-mono">{b.ref}</span>:{" "}
          {b.error ? `could not be checked (${b.error})` : b.index ? (b.platforms ?? []).join(", ") : "single platform"}
          {(b.warnings ?? []).map((w) => (
            <div key={w}>⚠ {w}</div>
          ))}
        </div>
      ))}
      <LogPanel
        logs={(parsed.files ?? []).map((f) => `${f.size}\t${f.path}`).join("\n")}
        title="Files in the image"
      />
      {packages.length > 0 && (
        <LogPanel logs={packages.join("\n")} title="Installed package versions" />
      )}
    </div>
  );
}

function FreshBuildView({ logs }: { logs?: string }) {
  const parsed = parseJson<FreshBuild>(logs);
  if (!logs) return null;
  if (!parsed) return <LogPanel logs={logs} title="Fresh build output" />;
  const oracle = parsed.oracle;
  return (
    <div className="space-y-2 text-xs">
      {parsed.error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-2.5 whitespace-pre-wrap">
          {parsed.error}
        </div>
      )}
      {parsed.build && (
        <div className={parsed.build.ok ? "text-emerald-600" : "text-red-600 dark:text-red-400"}>
          {parsed.build.ok
            ? "Builds with no cache and fresh base images."
            : "The build fails today with no cache: a dependency or base image changed."}
        </div>
      )}
      {oracle && (
        <div
          className={
            (oracle.reward ?? 0) >= 1 ? "text-emerald-600" : "text-red-600 dark:text-red-400"
          }
        >
          {(oracle.reward ?? 0) >= 1
            ? "The Oracle still passes with every RUN step rebuilt."
            : `The Oracle ${oracle.reward == null ? "produced no reward" : `scored ${oracle.reward}`} after a fresh rebuild${
                oracle.failed_tests.length ? `; failed: ${oracle.failed_tests.join(", ")}` : ""
              }.`}
        </div>
      )}
      {parsed.build && <LogPanel logs={parsed.build.log_tail} title="No-cache build log (tail)" />}
      {oracle && <LogPanel logs={oracle.log_tail} title="Oracle log (tail)" />}
    </div>
  );
}

function EdgeCasesView({ logs }: { logs?: string }) {
  if (!logs) return null;
  let parsed: EdgeCases | null = null;
  try {
    parsed = JSON.parse(logs) as EdgeCases;
  } catch {
    return <LogPanel logs={logs} title="Edge-case output" />;
  }
  return (
    <div className="space-y-2 text-sm">
      {parsed.error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-2.5 text-xs whitespace-pre-wrap">
          {parsed.error}
        </div>
      )}
      {parsed.spec_files && parsed.spec_files.length > 0 && (
        <div className="text-xs text-muted-foreground">
          Cases also listed from spec files:{" "}
          <span className="font-mono">{parsed.spec_files.join(", ")}</span>
        </div>
      )}
      {parsed.in_progress && parsed.probe_total !== undefined && (
        <div className="text-xs text-muted-foreground">
          Probing through the grader: {parsed.probe_done ?? 0} of {parsed.probe_total} done
        </div>
      )}
      {parsed.cases.map((c, i) => (
        <div key={i} className="rounded-md border border-border p-3">
          <div className="flex items-center justify-between gap-2">
            <span>{c.case}</span>
            <span
              className={`text-xs font-medium ${c.covered ? "text-emerald-600" : "text-amber-700 dark:text-amber-400"}`}
            >
              {c.covered ? "covered" : "not covered"}
            </span>
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            Expected: {c.expected}
            {c.test && <> · test: <span className="font-mono">{c.test}</span></>} — {c.reason}
          </div>
          {c.reference_handles === false && (
            <div className="mt-1 text-xs text-amber-700 dark:text-amber-400">
              The reference solution does not handle this case, so nobody has shown it can be solved.
            </div>
          )}
          {c.probe && (
            <div
              className={`mt-1 text-xs ${
                c.probe.status === "slipped_through"
                  ? "text-red-600 dark:text-red-400"
                  : c.probe.status === "caught"
                    ? "text-emerald-600"
                    : "text-muted-foreground"
              }`}
            >
              {c.probe.status === "slipped_through"
                ? c.covered
                  ? "Through the grader: the judge marked this covered, but an answer that mishandles it still scored 1, so no test really checks it."
                  : "Through the grader: an answer that mishandles this case still scored 1, so no test checks it."
                : c.probe.status === "caught"
                  ? `Through the grader: an answer that mishandles this case scored 0${
                      c.probe.failed_tests?.length ? ` (${c.probe.failed_tests.join(", ")})` : ""
                    }, so it is ${c.covered ? "tested, as the judge said" : "tested after all"}.`
                  : `Not run through the grader: ${c.probe.note || "no result"}`}
              {c.probe.description && (
                <span className="text-muted-foreground"> ({c.probe.description})</span>
              )}
              {c.probes?.some((p) => p.solution) && (
                <div className="text-muted-foreground">
                  {c.probes
                    .map((p) => `SOLUTION=${p.solution}: ${p.status.replaceAll("_", " ")}`)
                    .join(" · ")}
                </div>
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function VariantProbeView({ logs }: { logs?: string }) {
  if (!logs) return null;
  let parsed: VariantProbe | null = null;
  try {
    parsed = JSON.parse(logs) as VariantProbe;
  } catch {
    return <LogPanel logs={logs} title="Probe output" />;
  }
  return (
    <div className="space-y-2 text-sm">
      {parsed.error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-2.5 text-xs whitespace-pre-wrap">
          {parsed.error}
        </div>
      )}
      {parsed.variants.length === 0 && !parsed.error && (
        <div className="text-xs text-muted-foreground">
          No variants proposed{parsed.reason ? `: ${parsed.reason}` : "."}
        </div>
      )}
      {parsed.variants.map((v, i) => (
        <div key={i} className="rounded-md border border-border p-3">
          <div className="flex items-center justify-between gap-2">
            <span>
              {v.solution && (
                <span className="font-mono text-xs text-muted-foreground">
                  SOLUTION={v.solution}{" "}
                </span>
              )}
              {v.description}
            </span>
            <span
              className={`text-xs font-medium ${
                v.status === "accepted" || v.status === "caught"
                  ? "text-emerald-600"
                  : v.status === "rejected" || v.status === "slipped_through"
                    ? "text-red-600"
                    : "text-muted-foreground"
              }`}
            >
              {v.status === "did_not_run"
                ? "did not run"
                : v.status === "caught"
                  ? "caught (scored 0)"
                  : v.status === "slipped_through"
                    ? "slipped through (scored 1)"
                    : v.status}
            </span>
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            {parsed.mode === "broken" ? "Rule broken" : "Instruction says"}: “{v.instruction_quote}”
          </div>
          {v.status === "slipped_through" && (
            <div className="mt-1 text-xs text-red-600 dark:text-red-400">
              The verifier gave this deliberately broken answer full credit, so the rule above may
              not be enforced. Check that the edit really breaks it.
            </div>
          )}
          {v.status === "rejected" && (
            <div className="mt-1 text-xs text-red-600 dark:text-red-400">
              The verifier rejected this form
              {v.failed_tests.length > 0 && <>: failed {v.failed_tests.join(", ")}</>}. Check whether
              the grader is too strict or the variant was not really allowed.
            </div>
          )}
          {v.note && <div className="mt-1 text-xs whitespace-pre-wrap text-muted-foreground">{v.note}</div>}
        </div>
      ))}
      {(parsed.dropped ?? []).map((d, i) => (
        <div key={`d${i}`} className="text-xs text-muted-foreground">
          Dropped {d.solution ? `(SOLUTION=${d.solution}) ` : ""}“{d.description}”: {d.reason}.
        </div>
      ))}
    </div>
  );
}

function AnalysisView({
  logs,
}: {
  logs?: string;
}) {
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
      {parsed.classification && (
        <div className="rounded-md border border-border p-3 text-sm">
          <div className="mb-1 text-xs font-medium text-muted-foreground">
            Why the failed trials failed
          </div>
          <ul className="space-y-1 text-xs">
            {parsed.classification.trials.map((t) => (
              <li key={t.trial}>
                <span className="font-mono">{t.trial}</span> ·{" "}
                <span
                  className={
                    t.class === "instruction_gap"
                      ? "font-medium text-amber-700 dark:text-amber-400"
                      : t.class === "infrastructure"
                        ? "font-medium text-blue-700 dark:text-blue-400"
                        : ""
                  }
                >
                  {t.class.replace("_", " ")}
                </span>{" "}
                — {t.reason}
              </li>
            ))}
          </ul>
          {(parsed.classification.clusters ?? []).map((c) => (
            <div
              key={c.reading}
              className="mt-2 rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-xs text-amber-700 dark:text-amber-400"
            >
              {c.trials.length} trials made the same alternative choice ({c.label}): {c.reading}
            </div>
          ))}
        </div>
      )}
      {parsed.audit && (
        <div className="rounded-md border border-border p-3 text-xs space-y-2">
          <div className="text-xs font-medium text-muted-foreground">Audit of passes and the best trial</div>
          {(parsed.audit.passing ?? [])
            .filter((p) => p.assumptions?.length)
            .map((p) => (
              <div key={p.trial}>
                <span className="font-mono">{p.trial}</span> passed, but relied on assumptions the
                instruction does not state:
                <ul className="ml-4 list-disc">
                  {p.assumptions.map((a, i) => (
                    <li key={i}>
                      <span className="font-mono">{a.test}</span>: {a.assumption}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          {parsed.audit.best_failed && parsed.audit.best_failed.misses?.length > 0 && (
            <div>
              Best failing trial <span className="font-mono">{parsed.audit.best_failed.trial}</span>:
              <ul className="ml-4 list-disc">
                {parsed.audit.best_failed.misses.map((m, i) => (
                  <li key={i}>
                    <span className="font-mono">{m.test}</span>
                    {m.times && m.times > 1 ? ` (×${m.times})` : ""} ·{" "}
                    <span
                      className={
                        m.ruling === "agent_error" ? "" : "font-medium text-amber-700 dark:text-amber-400"
                      }
                    >
                      {m.ruling.replace("_", " ")}
                    </span>{" "}
                    — {m.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {(parsed.audit.examples ?? []).map((e, i) => (
            <div key={i} className="text-amber-700 dark:text-amber-400">
              Documented example the tests would reject: {e}
            </div>
          ))}
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
// (instruction_concision) inside the TB rubric -- an LLM's
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
    <div className="flex flex-col gap-3">
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
  if (isActive(status)) {
    return (
      <div className="space-y-3">
        <div className="text-xs text-muted-foreground">
          {parsed.reward === 1
            ? "Reward 1: checking the trajectory for reward hacking…"
            : "Trial finished; waiting for the others in this run."}
        </div>
        <LogPanel logs={parsed.trial_log ?? undefined} title="Cheat trial log" />
      </div>
    );
  }
  return (
    <div className="space-y-3">
      {parsed.reward !== 1 && parsed.inconclusive ? (
        <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-2.5 text-xs text-amber-700 dark:text-amber-400">
          Inconclusive — agent did not try: {parsed.inconclusive}. This is not evidence the
          anti-cheat design held. Run the Cheat Trial again, ideally with a second agent or the
          TB red-team prompt.
        </div>
      ) : parsed.reward !== 1 ? (
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

// Shown when the vendored upstream files are older than VENDOR_STALE_DAYS.
function VendorReminder({ info }: { info?: VendorInfo | null }) {
  if (!info?.stale || !info.reminder) return null;
  return (
    <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-2.5 text-xs text-amber-800 dark:text-amber-300">
      {info.reminder}
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
  // Harbor ignores the rubric's severity field; these criteria fail as warnings, not blockers.
  const warningCriteria = parsed.rubric_info?.warning_criteria;
  return (
    <div className="space-y-3">
      <div className="text-xs text-muted-foreground">
        {parsed.agent} · {parsed.model}
        {parsed.cost_usd != null && <> · ${parsed.cost_usd.toFixed(3)}</>}
        {parsed.rubric && <> · {parsed.rubric} rubric</>}
        {parsed.rubric_info && (
          <>
            {" "}
            ({parsed.rubric_info.criteria} criteria
            {parsed.rubric_info.commit && <>, upstream {parsed.rubric_info.commit.slice(0, 7)}</>}
            {parsed.rubric_info.date && <> of {parsed.rubric_info.date}</>})
          </>
        )}
        {parsed.summary && (
          <>
            {" "}
            · {parsed.summary.passed} pass, {parsed.summary.blocking_fails.length} fail,{" "}
            {parsed.summary.warnings.length} warning (non-blocking)
          </>
        )}
      </div>
      <VendorReminder info={parsed.rubric_info} />
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
                {Object.entries(r.checks).map(([name, c]) => {
                  const warning = c.outcome === "fail" && !!warningCriteria?.includes(name);
                  return (
                    <li key={name} className="text-xs">
                      <span
                        className={`mr-2 rounded px-1.5 py-0.5 font-medium ${
                          c.outcome === "pass"
                            ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400"
                            : warning
                              ? "bg-amber-500/15 text-amber-800 dark:text-amber-300"
                              : c.outcome === "fail"
                                ? "bg-red-500/15 text-red-700 dark:text-red-400"
                                : "bg-muted text-muted-foreground"
                        }`}
                      >
                        {name}: {warning ? "warning (non-blocking)" : c.outcome}
                      </span>
                      <span className="text-muted-foreground">{c.explanation}</span>
                    </li>
                  );
                })}
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

// Matches MIN_TRIAL_COST_USD on the backend.
const MIN_TRIAL_COST_USD = 3;

function VerifierLine({
  rawReward,
  verifier,
}: {
  rawReward?: number | null;
  verifier?: VerifierReport | null;
}) {
  const ctrf = verifier?.ctrf;
  const flags = verifier?.flags ?? [];
  const nonBinary = rawReward != null && rawReward !== 0 && rawReward !== 1;
  const facts = verifier?.facts;
  if (!ctrf && flags.length === 0 && !nonBinary && !facts) return null;
  return (
    <div className="mt-1 flex flex-col gap-1 text-xs">
      {facts && (
        <div className="text-muted-foreground">
          agent {facts.agent_seconds ?? "?"}s of {facts.agent_limit_sec}s · verifier{" "}
          {facts.verifier_seconds ?? "?"}s of {facts.verifier_limit_sec}s · {facts.platform ?? facts.host_arch}
          {facts.peak_memory_mb != null && ` · peak memory ${facts.peak_memory_mb}${facts.memory_mb ? ` of ${facts.memory_mb}` : ""} MB`}
          {" · "}agent user {facts.agent_user ?? "image default"}
          {facts.agent_uid != null && (
            <span className={facts.agent_uid === 0 ? "font-medium text-amber-600" : undefined}>
              {" "}(uid {facts.agent_uid}{facts.agent_uid === 0 ? ", root" : ""})
            </span>
          )}
          , verifier user{" "}
          {facts.verifier_user ?? "image default"}
        </div>
      )}
      {(ctrf || nonBinary) && (
        <div className="text-muted-foreground">
          {nonBinary && <span className="font-mono">raw reward {rawReward} · </span>}
          {ctrf && (
            <span>
              tests: <span className="font-mono">{ctrf.passed}/{ctrf.tests}</span> passed
              {ctrf.failed > 0 && `, ${ctrf.failed} failed`}
              {ctrf.skipped > 0 && `, ${ctrf.skipped} skipped`}
              {ctrf.failed_names.length > 0 && (
                <span title={ctrf.failed_names.join("\n")}>
                  {" "}— failed: <span className="font-mono">{ctrf.failed_names.slice(0, 3).join(", ")}</span>
                  {ctrf.failed_names.length > 3 && ` +${ctrf.failed_names.length - 3} more`}
                </span>
              )}
            </span>
          )}
        </div>
      )}
      {flags.map((f) => (
        <div
          key={f}
          className="flex items-start gap-1.5 rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-amber-700 dark:text-amber-400"
        >
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          <span>{f}</span>
        </div>
      ))}
    </div>
  );
}

function GraderErrorBadge() {
  return (
    <span
      className="rounded-full border border-red-500/40 bg-red-500/10 px-2 py-0.5 text-xs font-medium text-red-700 dark:text-red-400"
      title="The verifier itself broke, so there is no score. It says nothing about the agent: rerun."
    >
      Grader error – rerun
    </span>
  );
}

const VERDICT_STYLE: Record<SuggestedVerdict["verdict"], { label: string; cls: string }> = {
  fake: {
    label: "Fake (grader problem)",
    cls: "border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-400",
  },
  likely_genuine: { label: "Likely genuine", cls: "border-border bg-muted/40" },
  pass: {
    label: "Pass",
    cls: "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
  },
  unclear: {
    label: "Unclear",
    cls: "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400",
  },
};
const MAX_CHECKS_SHOWN = 60;

/** Reward Kit's view of one trial: a grader-error banner, the named checks (failed ones
 *  first, open by default when something failed), and the suggested genuine-or-fake call
 *  with controls for a reviewer to confirm or change it. Advisory: it gates nothing. */
function TrialInsight({
  stage,
  index,
  verifier,
  verdict,
}: {
  stage: "oracle" | "oracle_stress" | "nop" | "agent";
  index: number;
  verifier?: VerifierReport | null;
  verdict?: SuggestedVerdict | null;
}) {
  const grader = verifier?.grader_error;
  const checks = [...(verifier?.checks ?? [])].sort((a, b) => Number(a.passed) - Number(b.passed));
  const failed = checks.filter((c) => !c.passed).length;
  if (!grader && checks.length === 0 && !verdict) return null;
  const style = verdict ? VERDICT_STYLE[verdict.verdict] : null;
  return (
    <div
      className="mt-1 flex flex-col gap-1 text-xs"
      data-testid={`trial-insight-${stage}-${index}`}
    >
      {grader && (
        <div className="flex items-start gap-1.5 rounded border border-red-500/30 bg-red-500/10 px-2 py-1 text-red-700 dark:text-red-400">
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          <span>
            <b>Grader error ({grader.reason.replace(/_/g, " ")}).</b> {grader.evidence}. The grader
            broke, not the agent: there is no score. Left out of the pass rate; rerun this trial.
          </span>
        </div>
      )}
      {checks.length > 0 && (
        <details open={failed > 0}>
          <summary className="cursor-pointer text-muted-foreground">
            Named checks: {checks.length - failed}/{checks.length} passed
            {failed > 0 && `, ${failed} failed`}
            {stage === "nop" && " (a do-nothing agent is expected to fail checks)"}
          </summary>
          <ul className="mt-1 space-y-0.5 pl-1">
            {checks.slice(0, MAX_CHECKS_SHOWN).map((c, i) => (
              <li key={`${c.group}-${c.name}-${i}`} className="flex gap-2">
                <span
                  className={`w-9 shrink-0 font-mono ${c.passed ? "text-emerald-600" : "text-red-600 dark:text-red-400"}`}
                >
                  {c.passed ? "pass" : "FAIL"}
                </span>
                <span className="font-mono">{c.name}</span>
                {c.message && <span className="text-muted-foreground">— {c.message}</span>}
              </li>
            ))}
          </ul>
          {checks.length > MAX_CHECKS_SHOWN && (
            <div className="pl-1 text-muted-foreground">
              +{checks.length - MAX_CHECKS_SHOWN} more
            </div>
          )}
        </details>
      )}
      {verdict && style && (
        <div className={`rounded border px-2 py-1 ${style.cls}`}>
          <b>Suggested: {style.label}.</b> {verdict.reason.replace(/[.\s]+$/, "")}.{" "}
          <span className="opacity-80">{verdict.action}</span>
        </div>
      )}
    </div>
  );
}

const REGRADE_LABEL: Record<string, string> = {
  expected: "text-emerald-600",
  unexpected: "font-medium text-red-600 dark:text-red-400",
  unclear: "text-muted-foreground",
};

const REGRADE_CHANGE: Record<string, { label: string; cls: string }> = {
  same: { label: "unchanged", cls: "text-muted-foreground" },
  loosened: { label: "LOOSENED", cls: "font-medium text-amber-700 dark:text-amber-400" },
  tightened: { label: "tightened", cls: "font-medium text-red-600 dark:text-red-400" },
  now_scored: { label: "now scored", cls: "text-emerald-600" },
  now_no_score: { label: "now no score", cls: "text-red-600 dark:text-red-400" },
};

function RegradeView({ logs }: { logs?: string }) {
  const parsed = parseJson<RegradeReport>(logs);
  if (!logs) return null;
  if (!parsed) return <LogPanel logs={logs} title="Re-grade output" />;
  const sum = parsed.summary;
  const reward = (v: number | null) => (v == null ? "none" : String(v));
  return (
    <div className="space-y-2 text-xs">
      {parsed.error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-2.5 whitespace-pre-wrap">
          {parsed.error}
        </div>
      )}
      {parsed.source && (
        <div className="text-muted-foreground">
          Scored again the {parsed.kind ?? "agent"} trials of{" "}
          {parsed.source.same_upload ? "this upload" : "the earlier upload"}
          {parsed.source.submission && !parsed.source.same_upload && (
            <>
              {" "}
              (<span className="font-mono">{parsed.source.submission.slice(0, 8)}</span>)
            </>
          )}{" "}
          with this upload's grader
          {parsed.harbor_version && <> on Harbor {parsed.harbor_version}</>}.
        </div>
      )}
      {sum && sum.total > 0 && (
        <div>
          {sum.total} trial(s): {sum.same} unchanged
          {sum.loosened > 0 && (
            <>
              , <b className="text-amber-700 dark:text-amber-400">{sum.loosened} loosened</b>
            </>
          )}
          {sum.tightened > 0 && (
            <>
              , <b className="text-red-600 dark:text-red-400">{sum.tightened} tightened</b>
            </>
          )}
          {parsed.classified && (
            <>
              ; model says <b className="text-emerald-600">{sum.expected ?? 0} expected</b>,{" "}
              <b className="text-red-600 dark:text-red-400">{sum.unexpected ?? 0} unexpected</b>,{" "}
              {sum.unclear ?? 0} unclear
            </>
          )}
          {sum.now_scored > 0 && <>, {sum.now_scored} now scored</>}
          {sum.now_no_score > 0 && <>, {sum.now_no_score} now without a score</>}
          {(sum.grader_errors_before > 0 || sum.grader_errors_after > 0) && (
            <>
              ; grader errors {sum.grader_errors_before} → {sum.grader_errors_after}
            </>
          )}
          .
        </div>
      )}
      {sum && sum.loosened > 0 && (
        <div className="flex items-start gap-1.5 rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-amber-700 dark:text-amber-400">
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          <span>
            A loosened score means the grader now accepts work it rejected before. Read what
            changed: the task may have got easier.
          </span>
        </div>
      )}
      {parsed.items.length > 0 && (
        <ul className="divide-y divide-border rounded-md border border-border">
          {parsed.items.map((it) => {
            const c = REGRADE_CHANGE[it.change] ?? { label: it.change, cls: "" };
            return (
              <li key={`${it.job}-${it.trial}`} className="px-3 py-1.5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-mono">{it.trial}</span>
                  <span>
                    {reward(it.old_reward)} → {reward(it.new_reward)}{" "}
                    <span className={c.cls}>{c.label}</span>
                  </span>
                </div>
                {it.new_grader_error && (
                  <div className="text-red-600 dark:text-red-400">
                    grader error: {it.new_grader_error.evidence}
                  </div>
                )}
                {it.failed_checks.length > 0 && (
                  <div className="text-muted-foreground">failed: {it.failed_checks.join(", ")}</div>
                )}
                {it.old_tests != null && it.new_tests != null && it.old_tests !== it.new_tests && (
                  <div className="text-muted-foreground">
                    tests reported: {it.old_tests} → {it.new_tests}
                  </div>
                )}
                {(it.tests_missing?.length ?? 0) > 0 && (
                  <div className="text-amber-700 dark:text-amber-400">
                    no longer reported: {it.tests_missing!.join(", ")}
                  </div>
                )}
                {it.classification && (
                  <div className={REGRADE_LABEL[it.classification.classification]}>
                    model: {it.classification.classification}
                    {it.classification.confidence === "low" && " (low confidence)"}
                    {it.classification.reason && <> - {it.classification.reason}</>}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {parsed.classify_note && <div className="text-muted-foreground">{parsed.classify_note}</div>}
      {parsed.errors.length > 0 && (
        <LogPanel logs={parsed.errors.join("\n\n")} title="Re-grade errors" />
      )}
    </div>
  );
}

function TestSummaryBlock({
  summary,
}: {
  summary: NonNullable<AgentTrialsResult["test_summary"]>;
}) {
  const c = summary.closest;
  return (
    <div className="flex flex-col gap-1 border-b border-border px-3 py-2 text-xs text-muted-foreground">
      <div>
        Closest failing trial: #{c.trial} passed{" "}
        <span className="font-mono">{c.passed}/{c.tests}</span> tests.
      </div>
      {summary.failing_tests.slice(0, 5).map((t) => (
        <div key={t.name}>
          <span className="font-mono">{t.name}</span> failed in {t.failed_in} of{" "}
          {summary.failing_trials} failing trials
        </div>
      ))}
      {summary.notes.map((n) => (
        <div
          key={n}
          className="flex items-start gap-1.5 rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-amber-700 dark:text-amber-400"
        >
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          <span>{n}</span>
        </div>
      ))}
    </div>
  );
}

function StageCard({
  title,
  description,
  status,
  reward,
  wide,
  children,
}: {
  title: string;
  description: string;
  status: StageStatus;
  reward?: 0 | 1 | null;
  wide?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div
      className={`flex flex-col gap-3 rounded-lg border border-border bg-card p-4${wide ? " md:col-span-2" : ""}`}
    >
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
      <>
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <span tabIndex={0}>{btn}</span>
            </TooltipTrigger>
            <TooltipContent>{disabledReason}</TooltipContent>
          </Tooltip>
        </TooltipProvider>
        <span className="text-xs text-muted-foreground">Waiting for: {disabledReason}</span>
      </>
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
