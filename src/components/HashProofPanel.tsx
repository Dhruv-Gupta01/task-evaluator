import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type HashProofResult } from "@/lib/api";
import { Button } from "@/components/ui/button";

const VERDICTS: Record<string, string> = {
  all_used: "Passed: the grader uses every stored digest",
  unused_hash: "Failed: the grader does not use at least one stored digest",
  needs_review: "Needs a person: at least one digest could not be proven either way",
  no_hashes: "Failed: no hashed answer was found under tests/",
  error: "The proof run itself failed",
};
const RESULTS: Record<string, string> = {
  used: "used",
  not_used: "NOT used",
  unconfirmed: "not confirmed",
  no_result: "no result",
  not_run: "not run",
};

export function HashProofPanel({
  id,
  data,
  oraclePassed,
  onChange,
}: {
  id: string;
  data: HashProofResult;
  oraclePassed: boolean;
  onChange: () => void;
}) {
  const run = useMutation({
    mutationFn: () => api.hashProof(id),
    onSuccess: () => {
      toast.success("Hash proof started");
      onChange();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const active = data.status === "running" || data.status === "pending";
  const ran = data.status !== "not-run";
  return (
    <div className="mt-4 space-y-2 border-t pt-3 text-sm">
      <p className="font-medium">Hash proof</p>
      <p className="text-muted-foreground">
        Damages one stored digest at a time and runs the reference solution: the score must fall.
        Needs a passing Oracle; one Oracle run or more per digest, no model.
      </p>
      <Button
        size="sm"
        onClick={() => run.mutate()}
        disabled={!oraclePassed || run.isPending || active}
      >
        {active || run.isPending ? "Verifying…" : ran ? "Verify again" : "Verify hashing"}
      </Button>
      {!oraclePassed && <p className="text-muted-foreground">Run the Oracle first.</p>}
      {active && <p className="text-muted-foreground">{data.runs} run(s) so far.</p>}
      {ran && !active && (
        <p
          className={
            data.status === "passed" ? "font-medium text-green-700" : "font-medium text-red-700"
          }
        >
          {VERDICTS[data.verdict] ?? data.verdict}
        </p>
      )}
      {data.entries.length > 0 && (
        <ul className="space-y-1">
          {data.entries.map((e) => (
            <li
              key={`${e.file}:${e.name}`}
              className={e.result === "used" ? "text-muted-foreground" : "text-red-700"}
            >
              {e.result === "used" ? "✓" : "✗"} {e.name}
              {e.must_match ? "" : " (must not match)"}: {RESULTS[e.result] ?? e.result}{" "}
              <span className="text-xs">
                ({e.file}
                {e.note ? `; ${e.note}` : ""})
              </span>
            </li>
          ))}
        </ul>
      )}
      {ran && data.note && <p className="text-amber-700">{data.note}</p>}
      <p className="text-xs text-muted-foreground">
        A pass shows the stored digests are used. It does not show every answer is hashed, or that a
        short answer cannot be guessed. This check cannot tell either.
      </p>
    </div>
  );
}
