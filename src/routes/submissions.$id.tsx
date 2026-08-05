import { useState } from "react";
import { createFileRoute, Link } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, AlertTriangle } from "lucide-react";
import { api, type Submission, type StageStatus } from "@/lib/api";
import { StatusBadge } from "@/components/StatusBadge";
import { LogPanel } from "@/components/LogPanel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
  const mOracle = useMutation({
    mutationFn: () => api.oracle(id),
    onSuccess: () => {
      toast.success("Oracle started");
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
  const [n, setN] = useState(5);
  const mAgent = useMutation({
    mutationFn: () => api.agentTrials(id, n),
    onSuccess: () => {
      toast.success(`Agent trials (n=${n}) started`);
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
  const oracleNopPassed =
    data.oracle.status === "passed" && data.nop.status === "passed";

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
          </div>
        </div>
      </div>

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
          description="Run the oracle solution against tests."
          status={data.oracle.status}
          reward={data.oracle.reward}
        >
          <RunButton
            label="Run Oracle"
            runningLabel="Running…"
            onClick={() => mOracle.mutate()}
            running={data.oracle.status === "running"}
            pending={mOracle.isPending}
            disabled={!buildReady}
            disabledReason="Build the image first"
          />
          <LogPanel logs={data.oracle.logs} title="Oracle logs" />
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
          <div className="flex items-center gap-2">
            <label className="text-sm text-muted-foreground">N =</label>
            <Input
              type="number"
              min={1}
              max={100}
              value={n}
              onChange={(e) => setN(Math.max(1, Number(e.target.value) || 1))}
              className="w-20"
            />
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
      </div>
    </PageShell>
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
