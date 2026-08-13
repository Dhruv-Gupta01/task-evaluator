import { useRef, useState } from "react";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Upload, RefreshCw } from "lucide-react";
import { api, type SubmissionListItem } from "@/lib/api";
import { StatusBadge } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Submissions — Task Eval Dashboard" },
      { name: "description", content: "Upload tasks and run evaluation stages." },
      { property: "og:title", content: "Submissions — Task Eval Dashboard" },
      {
        property: "og:description",
        content: "Upload tasks and run evaluation stages.",
      },
    ],
  }),
  component: SubmissionsPage,
});

function SubmissionsPage() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const fileRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ["submissions"],
    queryFn: api.listSubmissions,
    refetchInterval: 5000,
  });

  const upload = useMutation({
    mutationFn: (file: File) => api.uploadSubmission(file),
    onSuccess: ({ id }) => {
      toast.success("Uploaded");
      qc.invalidateQueries({ queryKey: ["submissions"] });
      navigate({ to: "/submissions/$id", params: { id } });
    },
    onError: (e: Error) => toast.error(`Upload failed: ${e.message}`),
  });

  const handleFiles = (files: FileList | null) => {
    const file = files?.[0];
    if (!file) return;
    if (!file.name.endsWith(".zip")) {
      toast.error("Please upload a .zip file");
      return;
    }
    upload.mutate(file);
  };

  return (
    <div className="min-h-screen bg-background">
      <div className="mx-auto max-w-6xl px-6 py-8">
        <div className="mb-8 flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">Submissions</h1>
            <p className="text-sm text-muted-foreground">
              Upload a task zip and run evaluation stages.
            </p>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => refetch()}
            disabled={isFetching}
          >
            <RefreshCw className={`mr-2 h-4 w-4 ${isFetching ? "animate-spin" : ""}`} />
            Refresh
          </Button>
        </div>

        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            handleFiles(e.dataTransfer.files);
          }}
          className={`mb-8 flex flex-col items-center justify-center rounded-lg border-2 border-dashed p-10 transition-colors ${
            dragging ? "border-primary bg-primary/5" : "border-border"
          }`}
        >
          <Upload className="mb-3 h-8 w-8 text-muted-foreground" />
          <p className="mb-1 text-sm font-medium">Drop task zip here</p>
          <p className="mb-4 text-xs text-muted-foreground">
            or click to browse — .zip containing env/solution/tests/config
          </p>
          <input
            ref={fileRef}
            type="file"
            accept=".zip,application/zip"
            className="hidden"
            onChange={(e) => handleFiles(e.target.files)}
          />
          <Button
            onClick={() => fileRef.current?.click()}
            disabled={upload.isPending}
          >
            {upload.isPending ? "Uploading…" : "Upload Zip"}
          </Button>
        </div>

        <div className="rounded-lg border border-border">
          <div className="border-b border-border px-4 py-3 text-sm font-medium">
            Past submissions
          </div>
          {isLoading ? (
            <div className="p-8 text-center text-sm text-muted-foreground">
              Loading…
            </div>
          ) : error ? (
            <div className="p-8 text-center text-sm text-red-600">
              Failed to load: {(error as Error).message}
            </div>
          ) : !data || data.length === 0 ? (
            <div className="p-8 text-center text-sm text-muted-foreground">
              No submissions yet.
            </div>
          ) : (
            <div className="divide-y divide-border">
              {data.map((s) => (
                <SubmissionRow key={s.id} s={s} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function SubmissionRow({ s }: { s: SubmissionListItem }) {
  return (
    <Link
      to="/submissions/$id"
      params={{ id: s.id }}
      className="flex flex-wrap items-center gap-4 px-4 py-3 hover:bg-muted/40"
    >
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium">{s.task_name || s.id}</div>
        <div className="text-xs text-muted-foreground">
          <span className="font-mono">{s.id.slice(0, 12)}</span> ·{" "}
          {formatDate(s.uploaded_at)}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        <StatusBadge status={s.build_status} label="build" />
        <StatusBadge status={s.oracle_status} label="oracle" />
        <StatusBadge status={s.nop_status} label="nop" />
        <StatusBadge status={s.agent_status} label="agent" />
        <StatusBadge status={s.sufficiency_status} label="sufficiency" />
        <StatusBadge status={s.leakage_scan_status} label="leakage" />
        <StatusBadge status={s.code_smell_status} label="code-smell" />
        <StatusBadge status={s.review_report_status} label="report" />
      </div>
    </Link>
  );
}

function formatDate(iso: string) {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}
