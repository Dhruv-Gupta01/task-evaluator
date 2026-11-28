import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type IdeaReview } from "@/lib/api";
import { Button } from "@/components/ui/button";

// Idea review (backend generators.idea_review): a task idea checked before it is built, with
// Terminal-Bench's own proposal prompt, its task guidelines and the list of accepted tasks.
// One judge-model call per review; a person reads the result, nothing acts on it.
export function IdeaReviewCard() {
  const qc = useQueryClient();
  const [idea, setIdea] = useState("");
  const [shown, setShown] = useState<IdeaReview | null>(null);
  const { data: past } = useQuery({ queryKey: ["idea-reviews"], queryFn: api.listIdeaReviews });
  const review = useMutation({
    mutationFn: () => api.ideaReview(idea),
    onSuccess: (r) => {
      setShown(r);
      qc.invalidateQueries({ queryKey: ["idea-reviews"] });
      toast.success("Idea reviewed");
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const latest = shown ?? past?.[0] ?? null;
  const length = idea.trim().length;
  return (
    <div className="mb-8 space-y-3 rounded-lg border border-border p-4">
      <div>
        <p className="text-sm font-medium">Idea review</p>
        <p className="text-xs text-muted-foreground">
          Before building a task, describe the idea. The judge model reviews it with
          Terminal-Bench&apos;s own proposal prompt, task guidelines and list of accepted tasks (to
          catch duplicates). One model call, counted in the monthly budget. A draft for a person to
          read.
        </p>
      </div>
      <textarea
        className="min-h-24 w-full rounded-md border border-input bg-background p-2 text-sm"
        placeholder="What the agent must do, what the environment holds, how it is graded, why it is hard…"
        value={idea}
        onChange={(e) => setIdea(e.target.value)}
      />
      <div className="flex items-center gap-3">
        <Button
          size="sm"
          disabled={review.isPending || length < 20 || length > 20000}
          onClick={() => review.mutate()}
        >
          {review.isPending ? "Reviewing…" : "Review idea"}
        </Button>
        <span className="text-xs text-muted-foreground">
          {length < 20 ? "At least 20 characters." : `${length} characters`}
        </span>
      </div>
      {latest && (
        <div className="space-y-1 rounded-md bg-muted/40 p-3">
          <p className="text-xs text-muted-foreground">
            {shown ? "This review" : "Latest review"} · {latest.model} ·{" "}
            {new Date(latest.created_at).toLocaleString()}
          </p>
          <p className="text-xs italic text-muted-foreground line-clamp-2">{latest.idea}</p>
          <div className="max-h-[28rem] overflow-y-auto whitespace-pre-wrap text-sm">
            {latest.review}
          </div>
        </div>
      )}
    </div>
  );
}
