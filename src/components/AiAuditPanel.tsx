import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type AiAuditResult } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const TARGETS: { key: string; label: string }[] = [
  { key: "instruction.md", label: "instruction.md" },
  { key: "solution/solve.sh", label: "solution/solve.sh" },
  { key: "overall", label: "Overall" },
];

const RULING_TEXT: Record<string, string> = {
  approved: "Approved (human-written or acceptable)",
  disputed: "Disputed (AI-written or not acceptable)",
  unsure: "Unsure",
};

export function AiAuditPanel({
  id,
  data,
  validated,
  onChange,
}: {
  id: string;
  data: AiAuditResult;
  validated: boolean;
  onChange: () => void;
}) {
  const [target, setTarget] = useState("instruction.md");
  const [reason, setReason] = useState("");
  const [name, setName] = useState("");
  const rule = useMutation({
    mutationFn: (ruling: "approved" | "disputed" | "unsure") =>
      api.aiDetectionDecision(id, { target, ruling, adjudicator: name, reason }),
    onSuccess: () => {
      toast.success("Ruling saved");
      setReason("");
      onChange();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  return (
    <div className="space-y-3 rounded-md border p-3 text-sm">
      <div>
        <p className="font-medium">Human audit</p>
        <p className="text-xs text-muted-foreground">
          GPTZero and the rubric are machine opinions. A named person approves or disputes the AI
          use here. Advisory: it never changes a gate. A ruling is kept against the current
          instruction.md and solution/solve.sh, so a rewrite needs a new look.
        </p>
      </div>
      <p className="text-xs">{data.summary}</p>
      {TARGETS.map(({ key, label }) => {
        const d = data.decisions[key];
        if (!d) return null;
        return (
          <p
            key={key}
            className={d.stale ? "text-xs text-amber-700" : "text-xs text-muted-foreground"}
          >
            <span className="font-medium">{label}:</span> {RULING_TEXT[d.ruling] ?? d.ruling} by{" "}
            {d.adjudicator}
            {d.reason ? ` — ${d.reason}` : ""}
            {d.stale ? " (the file changed after this ruling; look again)" : ""}
          </p>
        );
      })}
      <div className="space-y-2">
        <select
          className="rounded border bg-background px-2 py-1"
          value={target}
          onChange={(e) => setTarget(e.target.value)}
        >
          {TARGETS.map((t) => (
            <option key={t.key} value={t.key}>
              {t.label}
            </option>
          ))}
        </select>
        <Input
          placeholder="Reason (required to approve or dispute)"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
        <Input
          placeholder="Your name (required)"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <div className="flex gap-2">
          {(["approved", "disputed", "unsure"] as const).map((r) => (
            <Button
              key={r}
              size="sm"
              variant={r === "approved" ? "default" : "outline"}
              disabled={
                !validated || rule.isPending || !name.trim() || (r !== "unsure" && !reason.trim())
              }
              onClick={() => rule.mutate(r)}
            >
              {r === "approved" ? "Approve" : r === "disputed" ? "Dispute" : "Unsure"}
            </Button>
          ))}
        </div>
      </div>
    </div>
  );
}
