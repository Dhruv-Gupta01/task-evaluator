import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type WorkerProbeResult } from "@/lib/api";
import { Button } from "@/components/ui/button";

const LABELS: Record<string, string> = {
  worker_started: "The Worker started",
  unprivileged_uid: "Runs as a non-root user",
  no_supplementary_groups: "No extra groups",
  no_new_privs: "Cannot gain privileges (no-new-privs)",
  no_capabilities: "No capabilities",
  tests_not_readable: "Cannot read /tests",
  reward_not_writable: "Cannot touch the reward file",
  reward_intact: "The reward file was left unchanged",
  env_not_inherited: "Does not inherit the grader's environment",
  leftovers_killed: "No process left running afterwards",
};

export function WorkerProbePanel({
  id,
  data,
  validated,
  onChange,
}: {
  id: string;
  data: WorkerProbeResult;
  validated: boolean;
  onChange: () => void;
}) {
  const run = useMutation({
    mutationFn: () => api.workerProbe(id),
    onSuccess: () => {
      toast.success("Worker probe started");
      onChange();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const active = data.status === "running" || data.status === "pending";
  const ran = data.status !== "not-run";
  const held = data.checks.filter((c) => c.ok).length;
  return (
    <div className="space-y-3 text-sm">
      <Button
        size="sm"
        onClick={() => run.mutate()}
        disabled={!validated || run.isPending || active}
      >
        {active || run.isPending ? "Probing…" : ran ? "Probe again" : "Run Worker probe"}
      </Button>
      {!validated && <p className="text-muted-foreground">Validate the submission first.</p>}
      {ran && !active && data.skipped && (
        <p className="text-muted-foreground">Skipped: {data.note}</p>
      )}
      {ran && !active && !data.skipped && data.checks.length > 0 && (
        <>
          <p
            className={
              data.status === "passed" ? "font-medium text-green-700" : "font-medium text-red-700"
            }
          >
            {data.status === "passed"
              ? `Passed: all ${data.checks.length} checks held`
              : `Failed: ${held} of ${data.checks.length} checks held`}
          </p>
          <ul className="space-y-1">
            {data.checks.map((c) => (
              <li key={c.name} className={c.ok ? "text-muted-foreground" : "text-red-700"}>
                {c.ok ? "✓" : "✗"} {LABELS[c.name] ?? c.name}{" "}
                <span className="text-xs">({c.detail})</span>
              </li>
            ))}
          </ul>
          {data.note && <p className="text-amber-700">{data.note}</p>}
        </>
      )}
      {ran && !active && !data.skipped && data.checks.length === 0 && (
        <p className="text-red-700">The probe did not report: {data.error ?? data.note}</p>
      )}
      <p className="text-xs text-muted-foreground">
        Advisory evidence only. It uses the default private paths and does not check whether the
        grader sends every piece of agent code through the Worker; the Static Check and Grader
        Isolation do.
      </p>
    </div>
  );
}
