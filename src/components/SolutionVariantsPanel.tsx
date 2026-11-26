import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type SolutionVariantsResult } from "@/lib/api";
import { Button } from "@/components/ui/button";

export function SolutionVariantsPanel({
  id,
  data,
  built,
  onChange,
}: {
  id: string;
  data: SolutionVariantsResult;
  built: boolean;
  onChange: () => void;
}) {
  const run = useMutation({
    mutationFn: () => api.solutionVariants(id),
    onSuccess: () => {
      toast.success("Running each correct solution");
      onChange();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const active = data.status === "running" || data.status === "pending";
  const ran = data.status !== "not-run";
  const enough = data.declared.length >= 2;
  return (
    <div className="space-y-2 text-sm">
      <Button
        size="sm"
        onClick={() => run.mutate()}
        disabled={!built || !enough || run.isPending || active}
      >
        {active || run.isPending ? "Running…" : ran ? "Run again" : "Run all solutions"}
      </Button>
      {!built && <p className="text-muted-foreground">Build must pass first.</p>}
      <p className={enough ? "text-muted-foreground" : "text-red-700"}>
        Declared correct solutions: {data.declared.length ? data.declared.join(", ") : "none"}
        {enough ? "" : ". At least 2 are required (docs/oracle-solutions.md)."}
      </p>
      {data.values.map((v) => (
        <p
          key={v.value}
          className={
            v.status === "passed"
              ? "text-green-700"
              : v.status === "failed"
                ? "text-red-700"
                : "text-muted-foreground"
          }
        >
          SOLUTION={v.value}: {v.status}
          {v.rewards.length > 0 && ` (rewards ${v.rewards.map((r) => r ?? "none").join(", ")})`}
        </p>
      ))}
      {data.note && <p className="whitespace-pre-wrap text-red-700">{data.note}</p>}
    </div>
  );
}
