import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

export function LogPanel({
  logs,
  title = "Logs",
  defaultOpen = false,
}: {
  logs?: string | null;
  title?: string;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const hasLogs = !!logs && logs.trim().length > 0;

  return (
    <div className="rounded-md border border-border">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between px-3 py-2 text-sm font-medium hover:bg-muted/50"
      >
        <span className="flex items-center gap-2">
          {open ? (
            <ChevronDown className="h-4 w-4" />
          ) : (
            <ChevronRight className="h-4 w-4" />
          )}
          {title}
          {!hasLogs && (
            <span className="text-xs text-muted-foreground">(empty)</span>
          )}
        </span>
      </button>
      {open && (
        <pre className="max-h-96 overflow-auto border-t border-border bg-zinc-950 p-3 font-mono text-xs leading-relaxed text-zinc-100">
          {hasLogs ? logs : "No output yet."}
        </pre>
      )}
    </div>
  );
}
