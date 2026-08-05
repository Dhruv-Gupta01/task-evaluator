import type { StageStatus } from "@/lib/api";
import { cn } from "@/lib/utils";

const styles: Record<StageStatus, string> = {
  "not-run": "bg-muted text-muted-foreground border-border",
  pending: "bg-muted text-muted-foreground border-border",
  running: "bg-blue-500/15 text-blue-600 border-blue-500/30 dark:text-blue-400",
  passed: "bg-green-500/15 text-green-600 border-green-500/30 dark:text-green-400",
  failed: "bg-red-500/15 text-red-600 border-red-500/30 dark:text-red-400",
};

const labels: Record<StageStatus, string> = {
  "not-run": "not run",
  pending: "pending",
  running: "running",
  passed: "passed",
  failed: "failed",
};

export function StatusBadge({
  status,
  label,
  className,
}: {
  status: StageStatus;
  label?: string;
  className?: string;
}) {
  const s = status ?? "not-run";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs font-medium capitalize",
        styles[s] ?? styles["not-run"],
        className,
      )}
    >
      {s === "running" && (
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
      )}
      {label ? `${label}: ${labels[s]}` : labels[s]}
    </span>
  );
}
