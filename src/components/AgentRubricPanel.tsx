import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type AgentRubricResult } from "@/lib/api";
import { Button } from "@/components/ui/button";

type Evidence = { trial: string; quote: string };
type Item = {
  criterion: string;
  pass_when?: string;
  weight?: number;
  evidence: Evidence[];
  unverified_evidence?: Evidence[];
};
type RubricLogs = {
  items?: Item[];
  dropped?: (Item & { reason?: string })[];
  model?: string;
  trials?: string[];
  transcript_chars?: number;
  error?: string;
};

function parse(logs?: string | null): RubricLogs | null {
  if (!logs) return null;
  try {
    return JSON.parse(logs) as RubricLogs;
  } catch {
    return null;
  }
}

// Agent rubric draft (backend generators.agent_rubric): rubric items about how the agents worked,
// each backed by a quote the platform found word for word in the trial it names. Reads every
// trial's full transcript. One judge-model call; a person reviews it.
export function AgentRubricPanel({
  id,
  data,
  hasTrials,
  onChange,
}: {
  id: string;
  data: AgentRubricResult;
  hasTrials: boolean;
  onChange: () => void;
}) {
  const run = useMutation({
    mutationFn: () => api.agentRubric(id),
    onSuccess: () => {
      toast.success("Agent rubric drafted");
      onChange();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const logs = parse(data.logs);
  return (
    <div className="space-y-2 rounded-md border border-border p-3 text-sm">
      <p className="font-medium">Agent rubric (draft)</p>
      <p className="text-xs text-muted-foreground">
        The judge model reads every trial&apos;s full transcript and proposes rubric items about how
        the agents worked. Each item must quote a trial; quotes not found word for word are dropped.
        One model call, counted in the monthly budget. A draft for a person to review.
      </p>
      <Button size="sm" disabled={!hasTrials || run.isPending} onClick={() => run.mutate()}>
        {run.isPending ? "Drafting…" : logs ? "Draft again" : "Draft agent rubric"}
      </Button>
      {!hasTrials && <p className="text-xs text-muted-foreground">Run agent trials first.</p>}
      {logs?.error && (
        <p className="text-xs text-red-600 dark:text-red-400">The draft failed: {logs.error}</p>
      )}
      {logs && !logs.error && (
        <p className="text-xs text-muted-foreground">
          {logs.items?.length ?? 0} item(s) kept, {logs.dropped?.length ?? 0} dropped (no
          word-for-word quote) · {logs.trials?.length ?? 0} trial(s) read · {logs.model}
        </p>
      )}
      {logs?.items && logs.items.length > 0 && (
        <ol className="list-decimal space-y-2 pl-5 text-xs">
          {logs.items.map((item, i) => (
            <li key={i}>
              <span className="font-medium">{item.criterion}</span>
              {item.weight ? ` (weight ${item.weight})` : ""}
              {item.pass_when && (
                <div className="text-muted-foreground">Pass when: {item.pass_when}</div>
              )}
              {item.evidence.map((e, j) => (
                <div
                  key={j}
                  className="mt-0.5 border-l-2 border-border pl-2 italic text-muted-foreground"
                >
                  {e.trial}: &ldquo;{e.quote}&rdquo;
                </div>
              ))}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
