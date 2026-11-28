import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type EvidenceSetResult } from "@/lib/api";
import { Button } from "@/components/ui/button";

type EvidenceLogs = {
  set?: string;
  second?: boolean;
  cost_cap?: number | null;
  pairs?: { agent: string | null; model: string | null; n: number; status: string }[];
  error?: string;
};

function parse(logs?: string | null): EvidenceLogs | null {
  if (!logs) return null;
  try {
    return JSON.parse(logs) as EvidenceLogs;
  } catch {
    return null;
  }
}

// Evidence sets (backend task_runner.run_evidence_set): fixed recipes of agent trials, appended to the
// existing ones. Standard: 3 trials of the main agent, then Failure Analysis; the second model family is
// optional. Cheap: 2 trials of a small model (a pass warns 15.36 in Results review).
export function EvidenceSetPanel({
  id,
  data,
  ready,
  notReadyReason,
  onChange,
}: {
  id: string;
  data: EvidenceSetResult;
  ready: boolean;
  notReadyReason: string;
  onChange: () => void;
}) {
  const [second, setSecond] = useState(false);
  const [cap, setCap] = useState("");
  const run = useMutation({
    mutationFn: (which: "standard" | "cheap") =>
      api.evidenceSet(id, which, Number(cap) > 0 ? Number(cap) : undefined, second),
    onSuccess: (_d, which) => {
      toast.success(
        `Evidence set (${which}${which === "standard" && second ? " + second model family" : ""}) started`,
      );
      onChange();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const active = data.status === "running" || data.status === "pending";
  const logs = parse(data.logs);
  const busy = active || run.isPending;
  return (
    <div className="space-y-2 rounded-md border border-border p-3 text-sm">
      <p className="font-medium">Evidence sets</p>
      <p className="text-xs text-muted-foreground">
        Fixed recipes of agent trials, added after the existing trials. Standard: 3 trials of the
        main agent, then Failure Analysis. Cheap: 2 trials of a small model; if it passes, Results
        review warns. These spend real credits. &quot;Run Agent Trials&quot; above replaces every
        trial, including these.
      </p>
      <label className="flex items-center gap-2 text-xs text-muted-foreground">
        <input type="checkbox" checked={second} onChange={(e) => setSecond(e.target.checked)} />
        Standard set: also 3 trials of a second model family (SECOND_AGENT, optional)
      </label>
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-xs text-muted-foreground">Set cost cap $ (optional)</label>
        <input
          className="h-8 w-24 rounded-md border border-input bg-background px-2 text-xs"
          inputMode="decimal"
          placeholder="none"
          value={cap}
          onChange={(e) => setCap(e.target.value)}
        />
        <Button size="sm" disabled={!ready || busy} onClick={() => run.mutate("standard")}>
          Run standard set
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={!ready || busy}
          onClick={() => run.mutate("cheap")}
        >
          Run cheap set
        </Button>
      </div>
      {!ready && <p className="text-xs text-muted-foreground">{notReadyReason}</p>}
      {logs?.error && (
        <p className="text-xs text-red-600 dark:text-red-400">The set failed: see the logs.</p>
      )}
      {logs?.pairs && logs.pairs.length > 0 && (
        <ul className="space-y-0.5 text-xs text-muted-foreground">
          {logs.pairs.map((p, i) => (
            <li
              key={i}
              className={
                p.status.startsWith("not run") ? "text-amber-700 dark:text-amber-400" : undefined
              }
            >
              {logs.set}: {p.agent ?? "default agent"} {p.model ?? "(.env model)"} × {p.n}:{" "}
              {p.status}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
