import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { LogPanel } from "@/components/LogPanel";

interface Probe {
  name: string;
  verdict: string;
  reward: number | null;
  tests: number | null;
  verifier_seconds: number | null;
}

const VERDICT_STYLE: Record<string, string> = {
  held: "text-emerald-700 dark:text-emerald-400",
  reached_1: "text-emerald-700 dark:text-emerald-400",
  failed_open: "text-red-700 dark:text-red-400",
  invalid_reward: "text-red-700 dark:text-red-400",
  did_not_reach_1: "text-red-700 dark:text-red-400",
  unverified: "text-amber-800 dark:text-amber-300",
};

function parse<T>(text: string | null | undefined): T | null {
  if (!text) return null;
  try {
    return JSON.parse(text) as T;
  } catch {
    return null;
  }
}

// Post-fix stages: Oracle/Nop probes and repeated Oracle (batch 4), evidence sets (batch 6),
// agent rubric and deliverable (batch 8). Run Flag checks afterwards to turn results into Flags.
export function PostFixPanel({
  id,
  buildReady,
  filesReady,
  hasTrials,
}: {
  id: string;
  buildReady: boolean;
  // the zip is unpacked (Validate & Build was started)
  filesReady: boolean;
  // at least one agent trial has finished
  hasTrials: boolean;
}) {
  const qc = useQueryClient();
  const [repeat, setRepeat] = useState(50);
  const [cap, setCap] = useState(10);
  const { data } = useQuery({
    queryKey: ["extras", id],
    queryFn: () => api.extras(id),
    refetchInterval: 10_000,
  });
  const after = () => qc.invalidateQueries({ queryKey: ["extras", id] });
  const onError = (e: Error) => toast.error(e.message);
  const probes = useMutation({
    mutationFn: (kinds: string) => api.probes(id, kinds, "fifo,symlink,directory,oversized"),
    onSuccess: after,
    onError,
  });
  const oracle = useMutation({
    mutationFn: () => api.oracle(id, repeat),
    onSuccess: after,
    onError,
  });
  const evidence = useMutation({
    mutationFn: (which: "standard" | "cheap") => api.evidenceSet(id, which, cap),
    onSuccess: after,
    onError,
  });
  const rubric = useMutation({ mutationFn: () => api.agentRubric(id), onSuccess: after, onError });
  const deliverable = useMutation({
    mutationFn: () => api.deliverable(id),
    onSuccess: after,
    onError,
  });

  const probeLogs = parse<{ in_progress?: boolean; control_reward?: number; probes?: Probe[] }>(
    data?.probes?.logs,
  );
  const evidenceLogs = parse<{
    set?: string;
    pairs?: { agent: string; model: string | null; n: number; status: string }[];
  }>(data?.evidence_runs?.logs);
  const rubricLogs = parse<{
    items?: { criterion: string; pass_when?: string }[];
    dropped?: unknown[];
    error?: string;
  }>(data?.agent_rubric?.logs);
  const deliverableLogs = parse<{ markdown?: string }>(data?.deliverable?.logs);
  const waiting = !buildReady;
  const waitingFor = "Build the image first (Validate & Build)";

  return (
    <section className="mb-6 rounded-lg border border-border p-4 text-sm">
      <h2 className="text-lg font-semibold">Probes, evidence runs and drafts</h2>
      <p className="text-xs text-muted-foreground">
        One Harbor job at a time. Run Flag checks above afterwards to turn the results into Flags.
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium">Probes</span>
        {[
          ["control,data_removal,hostile,null", "All"],
          ["control,data_removal", "Remove test data"],
          ["control,hostile", "Hostile artifacts"],
          ["control,null", "Null ladder"],
        ].map(([kinds, label]) => (
          <Button
            key={kinds}
            size="sm"
            variant="outline"
            disabled={waiting || probes.isPending}
            title={waiting ? waitingFor : undefined}
            onClick={() => probes.mutate(kinds)}
          >
            {label}
          </Button>
        ))}
        <span className="ml-4 text-xs font-medium">Repeat Oracle</span>
        <Input
          className="w-20"
          type="number"
          min={1}
          max={50}
          value={repeat}
          onChange={(e) => setRepeat(Number(e.target.value))}
        />
        <Button
          size="sm"
          variant="outline"
          disabled={waiting || oracle.isPending}
          title={waiting ? waitingFor : undefined}
          onClick={() => oracle.mutate()}
        >
          Run
        </Button>
      </div>
      {probeLogs?.probes && (
        <table className="mt-2 w-full text-left text-xs">
          <tbody>
            {probeLogs.probes.map((p, i) => (
              <tr key={i} className="border-t border-border">
                <td className="py-1 pr-2">{p.name}</td>
                <td className={`py-1 pr-2 font-medium ${VERDICT_STYLE[p.verdict] ?? ""}`}>
                  {p.verdict}
                </td>
                <td className="py-1 pr-2 text-muted-foreground">
                  reward {p.reward ?? "none"} · {p.tests ?? "?"} tests · verifier{" "}
                  {p.verifier_seconds ?? "?"} s
                </td>
              </tr>
            ))}
            {probeLogs.in_progress && (
              <tr>
                <td className="py-1 text-muted-foreground">running…</td>
              </tr>
            )}
          </tbody>
        </table>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium">Evidence sets</span>
        <span className="text-xs text-muted-foreground">cost cap $</span>
        <Input
          className="w-20"
          type="number"
          min={1}
          value={cap}
          onChange={(e) => setCap(Number(e.target.value))}
        />
        <Button
          size="sm"
          variant="outline"
          disabled={waiting || evidence.isPending}
          title={waiting ? waitingFor : undefined}
          onClick={() => evidence.mutate("standard")}
        >
          Standard set (2 pairs x 3)
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={waiting || evidence.isPending}
          title={waiting ? waitingFor : undefined}
          onClick={() => evidence.mutate("cheap")}
        >
          Cheap model (x 2)
        </Button>
      </div>
      {evidenceLogs?.pairs && (
        <ul className="mt-1 text-xs text-muted-foreground">
          {evidenceLogs.pairs.map((p, i) => (
            <li key={i}>
              {evidenceLogs.set}: {p.agent} {p.model ?? "(.env model)"} x {p.n}: {p.status}
            </li>
          ))}
        </ul>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium">Drafts (a person reviews them)</span>
        <Button
          size="sm"
          variant="outline"
          disabled={rubric.isPending || !hasTrials}
          title={hasTrials ? undefined : "Run the agent trials first: the rubric is written from their transcripts"}
          onClick={() => rubric.mutate()}
        >
          {rubric.isPending ? "Writing…" : "Agent rubric"}
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={deliverable.isPending || !filesReady}
          title={filesReady ? undefined : "Click Validate & Build first"}
          onClick={() => deliverable.mutate()}
        >
          {deliverable.isPending ? "Writing…" : "Task deliverable"}
        </Button>
      </div>
      {rubricLogs && (
        <div className="mt-2 text-xs">
          {rubricLogs.error ? (
            <span className="text-red-700 dark:text-red-400">{rubricLogs.error}</span>
          ) : (
            <>
              <ul className="list-disc pl-5">
                {(rubricLogs.items ?? []).map((it, i) => (
                  <li key={i}>
                    {it.criterion}
                    {it.pass_when ? `: ${it.pass_when}` : ""}
                  </li>
                ))}
              </ul>
              <p className="text-muted-foreground">
                {rubricLogs.dropped?.length ?? 0} item(s) dropped: their quotes were not found in
                the trajectories.
              </p>
            </>
          )}
        </div>
      )}
      {deliverableLogs?.markdown && (
        <LogPanel logs={deliverableLogs.markdown} title="Task deliverable (draft)" />
      )}
    </section>
  );
}

export function IdeaReviewCard() {
  const [idea, setIdea] = useState("");
  const review = useMutation({
    mutationFn: () => api.ideaReview(idea),
    onError: (e: Error) => toast.error(e.message),
  });
  return (
    <section className="mb-6 rounded-lg border border-border p-4 text-sm">
      <h2 className="text-lg font-semibold">Idea Review</h2>
      <p className="text-xs text-muted-foreground">
        Before building a task: Terminal-Bench&apos;s own proposal prompt reviews the idea. A person
        reads the result; nothing acts on it.
      </p>
      <Textarea
        className="mt-2"
        rows={4}
        placeholder="Describe the task idea: what the agent must do, why it is hard, how it is verified."
        value={idea}
        onChange={(e) => setIdea(e.target.value)}
      />
      <Button
        className="mt-2"
        size="sm"
        disabled={review.isPending || idea.trim().length < 20}
        onClick={() => review.mutate()}
      >
        {review.isPending ? "Reviewing…" : "Review idea"}
      </Button>
      {review.data && (
        <LogPanel logs={review.data.review} title={`Review (${review.data.model})`} />
      )}
    </section>
  );
}
