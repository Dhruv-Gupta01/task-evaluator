import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type GraderIsolationResult } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const STATE_TEXT: Record<string, string> = {
  ok: "Passed: no unprotected agent code in the grader",
  missing: "Worker required, and missing",
  unclear: "Unclear: a person needs to decide",
};

export function GraderIsolationPanel({
  id,
  data,
  validated,
  onChange,
}: {
  id: string;
  data: GraderIsolationResult;
  validated: boolean;
  onChange: () => void;
}) {
  const [result, setResult] = useState<"not_required" | "equivalent" | "missing">("not_required");
  const [reason, setReason] = useState("");
  const [name, setName] = useState("");
  const run = useMutation({
    mutationFn: () => api.graderIsolation(id),
    onSuccess: () => {
      toast.success("Grader isolation checked");
      onChange();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const rule = useMutation({
    mutationFn: () => api.graderIsolationDecision(id, { result, reason, adjudicator: name }),
    onSuccess: () => {
      toast.success("Ruling saved");
      setReason("");
      onChange();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const ran = data.status !== "not-run";
  return (
    <div className="space-y-3 text-sm">
      <Button size="sm" onClick={() => run.mutate()} disabled={!validated || run.isPending}>
        {run.isPending ? "Checking…" : ran ? "Check again" : "Check grader isolation"}
      </Button>
      {!validated && <p className="text-muted-foreground">Validate the submission first.</p>}
      {ran && data.state && (
        <div
          className={
            data.state === "ok"
              ? "text-green-700"
              : data.state === "missing"
                ? "text-red-700"
                : "text-amber-700"
          }
        >
          <p className="font-medium">
            {STATE_TEXT[data.state]}
            {data.source === "reviewer" ? " (by a reviewer's ruling)" : ""}
          </p>
          <p className="text-muted-foreground">
            Detected: {data.analysis}.{" "}
            {data.enforced
              ? data.blocks
                ? "Enforced: agent stages are blocked until this passes or a reviewer rules."
                : "Enforced, and not blocking."
              : "Advisory: nothing is blocked."}
          </p>
          {data.evidence.length > 0 && <p>Where: {data.evidence.join(", ")}</p>}
          {data.absent.length > 0 && <p>Missing: {data.absent.join(", ")}</p>}
          {data.also_unprotected.length > 0 && (
            <p>Also runs outside the Worker: {data.also_unprotected.join(", ")}</p>
          )}
        </div>
      )}
      {data.override && (
        <p className={data.override_stale ? "text-amber-700" : "text-muted-foreground"}>
          {data.override_stale
            ? "The grader changed after the last ruling, so it no longer applies. "
            : "Ruling: "}
          {data.override.result} by {data.override.adjudicator}: {data.override.reason}
        </p>
      )}
      {ran && (
        <div className="space-y-2 rounded border p-3">
          <p className="font-medium">Reviewer ruling</p>
          <select
            className="rounded border bg-background px-2 py-1"
            value={result}
            onChange={(e) => setResult(e.target.value as typeof result)}
          >
            <option value="not_required">Not required (not agent code)</option>
            <option value="equivalent">Equivalent isolation</option>
            <option value="missing">Missing</option>
          </select>
          <Input
            placeholder="Reason (required)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <Input
            placeholder="Your name (required)"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <Button
            size="sm"
            variant="outline"
            onClick={() => rule.mutate()}
            disabled={rule.isPending || !reason.trim() || !name.trim()}
          >
            Save ruling
          </Button>
        </div>
      )}
    </div>
  );
}
