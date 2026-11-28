import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { AGENT_CHOICES, api, type AgentChoice, type EvidenceSetResult } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

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
// existing ones. Standard: 3 trials of the agent and model picked on the Agent Trials card, then Failure
// Analysis; the second model family (its own agent and model pickers) is optional. Cheap: 2 trials of the
// small model from .env (a pass warns 15.36 in Results review).
export function EvidenceSetPanel({
  id,
  data,
  ready,
  notReadyReason,
  onChange,
  mainAgent,
  mainModel,
  agentModels,
  modelChoices,
}: {
  id: string;
  data: EvidenceSetResult;
  ready: boolean;
  notReadyReason: string;
  onChange: () => void;
  mainAgent: AgentChoice;
  mainModel: string;
  agentModels?: Record<string, string>;
  modelChoices?: Record<string, string[]>;
}) {
  const [second, setSecond] = useState(false);
  const [secondAgent, setSecondAgent] = useState<AgentChoice>(
    mainAgent === "codex" ? "claude-code" : "codex",
  );
  // "" = the server's default: SECOND_AGENT_MODEL for SECOND_AGENT, else that agent's .env model.
  const [secondModel, setSecondModel] = useState("");
  const mainLabel = AGENT_CHOICES.find((a) => a.value === mainAgent)?.label ?? mainAgent;
  const mainModelName = (mainModel || agentModels?.[mainAgent] || ".env model").split("/").pop();
  const [cap, setCap] = useState("");
  const run = useMutation({
    mutationFn: (which: "standard" | "cheap") =>
      api.evidenceSet(id, which, Number(cap) > 0 ? Number(cap) : undefined, {
        second,
        agent: mainAgent,
        model: mainModel || undefined,
        secondAgent,
        secondModel: secondModel || undefined,
      }),
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
      <p className="text-xs text-muted-foreground">
        Standard set, first family: {mainLabel} · {mainModelName} (the Agent and Model picked
        above).
      </p>
      <label className="flex items-center gap-2 text-xs text-muted-foreground">
        <input type="checkbox" checked={second} onChange={(e) => setSecond(e.target.checked)} />
        Standard set: also 3 trials of a second model family (optional)
      </label>
      {second && (
        <div className="flex flex-wrap items-center gap-2 pl-6">
          <label className="text-xs text-muted-foreground">Second agent</label>
          <Select
            value={secondAgent}
            onValueChange={(v) => {
              setSecondAgent(v as AgentChoice);
              setSecondModel("");
            }}
          >
            <SelectTrigger className="h-8 w-40 text-xs">
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
          <label className="text-xs text-muted-foreground">Model</label>
          <Select
            value={secondModel || "env"}
            onValueChange={(v) => setSecondModel(v === "env" ? "" : v)}
          >
            <SelectTrigger className="h-8 w-56 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="env">Default (SECOND_AGENT_MODEL or the .env model)</SelectItem>
              {(modelChoices?.[secondAgent] ?? []).map((m) => (
                <SelectItem key={m} value={m}>
                  {m.split("/").pop()}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}
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
