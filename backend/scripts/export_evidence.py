"""Writes the evidence for one already-run test submission: <out>/result.json (what the platform
returned) and <out>/issue.md (the GitHub issue or PR the test is about). Used to rebuild
docs/testing/ from the database. Example:

  uv run python scripts/export_evidence.py --id <submission id> --task kv-live-surgery \
      --issue 1389 --issue 1649 --out ../docs/testing/4.0/D/kv-live-surgery-1389-1649/before
"""

import argparse
import json
import subprocess
import urllib.request
from pathlib import Path

REPO = "harbor-framework/terminal-bench"


def issue_markdown(numbers: list[str]) -> str:
    parts = []
    for n in numbers:
        info = json.loads(subprocess.run(["gh", "api", f"repos/{REPO}/issues/{n}"], capture_output=True, text=True, check=True).stdout)
        kind = "PR" if info.get("pull_request") else "Issue"
        parts.append(f"## {kind} #{n}: {info['title']}\n\n{info['html_url']} ({info['state']})\n\n{(info.get('body') or '').strip()[:2500]}\n")
    return "\n".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8003")
    ap.add_argument("--id", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--issue", action="append", default=[])
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(f"{a.base}/submissions/{a.id}", timeout=120) as r:
        result = json.loads(r.read())
    result["_test"] = {"task": a.task, "issues": a.issue, "submission_id": a.id}
    (out / "result.json").write_text(json.dumps(result, indent=1))
    if a.issue:
        (out / "issue.md").write_text(issue_markdown(a.issue))
    print("saved", out)


if __name__ == "__main__":
    main()
