import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type GraderIsolationResult } from "@/lib/api";
import { Button } from "@/components/ui/button";

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
  const run = useMutation({
    mutationFn: () => api.graderIsolation(id),
    onSuccess: () => {
      toast.success("Grader isolation checked");
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
          <p className="font-medium">{STATE_TEXT[data.state]}</p>
          <p className="text-muted-foreground">
            Detected: {data.analysis}.{" "}
            {data.enforced
              ? data.blocks
                ? "Enforced: agent stages are blocked until the task is revised and passes."
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
    </div>
  );
}
