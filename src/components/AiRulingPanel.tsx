import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type AiDetectionResult } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export function AiRulingPanel({
  id,
  data,
  onChange,
}: {
  id: string;
  data: AiDetectionResult;
  onChange: () => void;
}) {
  const [name, setName] = useState("");
  const [reason, setReason] = useState("");
  const rule = useMutation({
    mutationFn: (ruling: "approved" | "disapproved") =>
      api.aiDetectionDecision(id, { ruling, adjudicator: name, reason }),
    onSuccess: () => {
      toast.success("Ruling saved");
      setReason("");
      onChange();
    },
    onError: (e: Error) => toast.error(e.message),
  });
  if (data.status !== "passed" && data.status !== "failed") return null;
  const d = data.decision;
  const ready = name.trim() !== "" && reason.trim() !== "" && !rule.isPending;
  return (
    <div className="space-y-2 rounded border p-3 text-sm">
      <p className="font-medium">Manual ruling</p>
      <p className="text-xs text-muted-foreground">
        Approve to pass this check, or disapprove to fail it, whatever the detector said. This is
        the only check on the platform a person can rule on.
      </p>
      {d && !data.decision_stale && (
        <p className={d.ruling === "approved" ? "text-green-700" : "text-red-700"}>
          {d.ruling === "approved" ? "Approved" : "Disapproved"} by {d.adjudicator} on{" "}
          {d.at.slice(0, 10)}: {d.reason}. The detector said {data.detected_status}.
        </p>
      )}
      {d && data.decision_stale && (
        <p className="text-amber-700">
          The ruling by {d.adjudicator} no longer applies: the instruction or solution changed.
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Input
          className="h-8 w-44"
          placeholder="Your name (required)"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <Input
          className="h-8 w-72"
          placeholder="Reason (required)"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
        <Button size="sm" disabled={!ready} onClick={() => rule.mutate("approved")}>
          Approve
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={!ready}
          onClick={() => rule.mutate("disapproved")}
        >
          Disapprove
        </Button>
      </div>
    </div>
  );
}
