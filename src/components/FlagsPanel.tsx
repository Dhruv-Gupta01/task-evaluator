import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type FlagItem, type FlagRecord, type FlagStatus } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

// The reviewer's name is a per-browser convenience; rulings are stored on the server.
const NAME_KEY = "taskeval.reviewerName";

function savedName(): string {
  try {
    return localStorage.getItem(NAME_KEY) ?? "";
  } catch {
    return "";
  }
}

function saveName(name: string) {
  try {
    localStorage.setItem(NAME_KEY, name);
  } catch {
    // storage blocked: the name just is not remembered
  }
}

const STATUS_STYLE: Record<FlagStatus, string> = {
  open: "bg-muted text-foreground",
  accepted: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
  disputed: "bg-blue-500/15 text-blue-700 dark:text-blue-400",
};

export function FlagsPanel({ id }: { id: string }) {
  const qc = useQueryClient();
  const [name, setName] = useState(savedName);
  const { data } = useQuery({ queryKey: ["flags", id], queryFn: () => api.flags(id) });
  const setRecord = (rec: FlagRecord) => qc.setQueryData(["flags", id], rec);
  const run = useMutation({
    mutationFn: () => api.runFlags(id),
    onSuccess: setRecord,
    onError: (e: Error) => toast.error(e.message),
  });

  // A tests/-only change is a Minor bump: the saved trials stay valid and only need re-scoring.
  const regrade = useMutation({
    mutationFn: (kind: "agent" | "oracle") => api.regrade(id, kind, "previous"),
    onSuccess: () => {
      toast.success(
        "Re-grade started. See the Re-grade card; run the Flag checks again when it finishes.",
      );
      qc.invalidateQueries({ queryKey: ["submission", id] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const live = data?.flags.filter((f) => f.present) ?? [];
  const gone = data?.flags.filter((f) => !f.present) ?? [];
  const c = data?.counts;
  const ran = data?.rules.filter((r) => r.ran).length ?? 0;

  return (
    <section className="mb-6 rounded-lg border border-border p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Flags</h2>
          <p className="text-xs text-muted-foreground">
            Rules that read the task files (no Docker, no LLM). A Blocker needs a ruling from a
            named person before the task is trusted; the platform never edits the task.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Input
            className="w-44"
            placeholder="Your name"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              saveName(e.target.value);
            }}
          />
          <Button onClick={() => run.mutate()} disabled={run.isPending}>
            {run.isPending ? "Checking…" : data?.last_run ? "Run again" : "Run Flag checks"}
          </Button>
        </div>
      </div>

      {data?.last_run && c && (
        <div
          className={`mt-3 rounded-md border p-2.5 text-xs ${
            c.open_blockers
              ? "border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-400"
              : "border-border bg-muted/40"
          }`}
        >
          {c.open_blockers} open Blocker{c.open_blockers === 1 ? "" : "s"}, {c.open_advisories} open
          advisor{c.open_advisories === 1 ? "y" : "ies"}, {c.accepted} accepted, {c.disputed}{" "}
          disputed
          {c.no_longer_found ? `, ${c.no_longer_found} no longer found` : ""} · {ran} of{" "}
          {data.rules.length} rules ran · last run {new Date(data.last_run).toLocaleString()}
        </div>
      )}

      {data?.version && (
        <div className="mt-2 rounded-md border border-border bg-muted/40 p-2.5 text-xs">
          Version against the earlier upload {data.version.against.slice(0, 8)}:{" "}
          {data.version.old_version ?? "none"} → {data.version.new_version ?? "none"}, declared{" "}
          {data.version.declared_class ?? "unknown"}, the {data.version.changed.length} changed
          file(s) call for {data.version.suggested_class ?? "nothing"}. {data.version.redo}
          {data.version.suggested_class === "minor" && (
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={regrade.isPending}
                onClick={() => regrade.mutate("agent")}
              >
                Regrade saved agent trials from {data.version.against.slice(0, 8)}
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={regrade.isPending}
                onClick={() => regrade.mutate("oracle")}
              >
                Regrade saved Oracle runs
              </Button>
              <span className="text-muted-foreground">
                Scores the earlier upload's trials with this upload's grader; no agent runs. The
                solution is not re-run, so run Oracle too if solution/ also changed.
              </span>
            </div>
          )}
        </div>
      )}

      <div className="mt-3 space-y-2">
        {live.map((f) => (
          <FlagRow key={f.id} id={id} flag={f} name={name} onRecord={setRecord} />
        ))}
        {gone.length > 0 && (
          <details className="text-xs">
            <summary className="cursor-pointer text-muted-foreground">
              {gone.length} no longer found by the latest run (rulings kept)
            </summary>
            <div className="mt-2 space-y-2">
              {gone.map((f) => (
                <FlagRow key={f.id} id={id} flag={f} name={name} onRecord={setRecord} />
              ))}
            </div>
          </details>
        )}
      </div>

      {data && data.rules.length > 0 && (
        <details className="mt-3 text-xs">
          <summary className="cursor-pointer text-muted-foreground">
            All {data.rules.length} rules and what each looked at
          </summary>
          <table className="mt-2 w-full text-left">
            <tbody>
              {data.rules.map((r) => (
                <tr key={r.id} className="border-t border-border align-top">
                  <td className="py-1 pr-2 font-mono">{r.id}</td>
                  <td className="py-1 pr-2 font-mono">{r.check}</td>
                  <td className="py-1 pr-2">
                    {r.title}
                    {r.severity === "blocker" && (
                      <span className="text-red-600 dark:text-red-400"> (Blocker)</span>
                    )}
                  </td>
                  <td className="py-1 text-muted-foreground">
                    {r.ran ? r.note : `${r.method} rule, comes with batch ${r.batch}`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </section>
  );
}

function FlagRow({
  id,
  flag,
  name,
  onRecord,
}: {
  id: string;
  flag: FlagItem;
  name: string;
  onRecord: (rec: FlagRecord) => void;
}) {
  const [reason, setReason] = useState("");
  const rule = useMutation({
    mutationFn: (status: FlagStatus) => api.ruleOnFlag(id, flag.id, { status, by: name, reason }),
    onSuccess: (rec) => {
      setReason("");
      onRecord(rec);
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const blocker = flag.severity === "blocker";

  return (
    <div
      className={`rounded-md border p-3 text-sm ${
        !flag.present
          ? "border-border opacity-70"
          : blocker && flag.status === "open"
            ? "border-red-500/40"
            : "border-border"
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={`rounded px-1.5 py-0.5 text-xs font-medium ${
            blocker
              ? "bg-red-500/15 text-red-700 dark:text-red-400"
              : "bg-amber-500/15 text-amber-800 dark:text-amber-300"
          }`}
        >
          {blocker ? "Blocker" : "Advisory"}
        </span>
        <span className="font-mono text-xs text-muted-foreground">
          {flag.rule} · {flag.check}
        </span>
        <span className="font-medium">{flag.title}</span>
        <span className={`ml-auto rounded px-1.5 py-0.5 text-xs ${STATUS_STYLE[flag.status]}`}>
          {flag.status}
          {flag.status_by ? ` by ${flag.status_by}` : ""}
        </span>
      </div>
      <p className="mt-1 text-xs text-muted-foreground">{flag.detail}</p>
      {flag.suggested_status === "disputed" && flag.status === "open" && (
        <p className="mt-1 text-xs text-blue-700 dark:text-blue-400">
          The platform has proof this finding is probably wrong (below). Dispute it rather than
          change the task.
        </p>
      )}
      <ul className="mt-2 space-y-0.5 font-mono text-xs">
        {flag.evidence.map((e, i) => (
          <li key={i} className="break-all">
            {e}
          </li>
        ))}
      </ul>
      {flag.status_reason && (
        <p className="mt-2 text-xs">
          <span className="text-muted-foreground">Reason:</span> {flag.status_reason}
        </p>
      )}
      <div className="mt-2 flex flex-wrap items-start gap-2">
        <Textarea
          className="min-h-9 flex-1 text-xs"
          rows={1}
          placeholder="Reason (required to accept or dispute)"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
        {flag.status !== "accepted" && (
          <Button
            size="sm"
            variant="outline"
            disabled={rule.isPending}
            onClick={() => rule.mutate("accepted")}
          >
            Accept
          </Button>
        )}
        {flag.status !== "disputed" && (
          <Button
            size="sm"
            variant="outline"
            disabled={rule.isPending}
            onClick={() => rule.mutate("disputed")}
          >
            Dispute
          </Button>
        )}
        {flag.status !== "open" && (
          <Button
            size="sm"
            variant="ghost"
            disabled={rule.isPending}
            onClick={() => rule.mutate("open")}
          >
            Reopen
          </Button>
        )}
      </div>
      {flag.history.length > 0 && (
        <details className="mt-2 text-xs text-muted-foreground">
          <summary className="cursor-pointer">History ({flag.history.length})</summary>
          <ul className="mt-1 space-y-0.5">
            {flag.history.map((h, i) => (
              <li key={i}>
                {new Date(h.at).toLocaleString()}: {h.from} → {h.status} by {h.by}
                {h.reason ? `: ${h.reason}` : ""}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
