"""Runs stages on one real Terminal-Bench task through a running backend and records the evidence.

Writes <out>/issue.md (the GitHub issue the test is about), <out>/result.json (the stage results the
platform returned) and <out>/run.log. Used for docs/testing/. Example:

  uv run python scripts/run_tb_test.py --tasks-dir ~/terminal-bench/tasks --task legacy-utility-triage \
      --issue 1573 --stages static-checks,tb-static-checks,sufficiency?runs=3 --out ../docs/testing/4.0/A/legacy-utility-triage-1573
"""

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = "harbor-framework/terminal-bench"
# Endpoint name -> key in the submission JSON
KEYS = {"static-checks": "static_checks", "tb-static-checks": "tb_static_checks", "agent-trials": "agent_trials",
        "variant-probe": "variant_probe", "weak-probe": "weak_probe", "edge-cases": "edge_cases",
        "failure-analysis": "failure_analysis", "cheat-trial": "cheat_trial", "rubric-check": "rubric_check",
        "answer-exposure": "answer_exposure", "image-inventory": "image_inventory", "review-report": "review_report"}


def call(base, method, path, data=None, headers=None):
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read() or b"{}")


def upload(base, task_dir: Path) -> str:
    tmp = Path(tempfile.mkdtemp()) / f"{task_dir.name}.zip"
    subprocess.run(["zip", "-qr", str(tmp), "."], cwd=task_dir, check=True)
    boundary = "----tb"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{tmp.name}\"\r\n"
            "Content-Type: application/zip\r\n\r\n").encode() + tmp.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    return call(base, "POST", "/submissions", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})["id"]


def wait(base, sid, key, log, timeout):
    end = time.time() + timeout
    while time.time() < end:
        status = (call(base, "GET", f"/submissions/{sid}").get(key) or {}).get("status")
        if status not in ("pending", "running"):
            return status
        time.sleep(5)
    log(f"timed out waiting for {key}")
    return "timeout"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8003")
    ap.add_argument("--tasks-dir", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--ref", help="git commit in the terminal-bench clone to take the task from (default: the working tree)")
    ap.add_argument("--repo-dir", help="the terminal-bench clone (parent of --tasks-dir); needed with --ref")
    ap.add_argument("--issue", action="append", default=[])
    ap.add_argument("--stages", required=True, help="comma list, query string allowed, e.g. sufficiency?runs=3")
    ap.add_argument("--out", required=True)
    ap.add_argument("--build", action="store_true", help="validate and build first (needed for Docker stages)")
    ap.add_argument("--timeout", type=int, default=1800)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    logf = open(out / "run.log", "w")

    def log(msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    parts = []
    for n in a.issue:
        info = json.loads(subprocess.run(["gh", "api", f"repos/{REPO}/issues/{n}"], capture_output=True, text=True, check=True).stdout)
        kind = "PR" if info.get("pull_request") else "Issue"
        body = (info.get("body") or "").strip()
        parts.append(f"## {kind} #{n}: {info['title']}\n\n{info['html_url']} ({info['state']})\n\n{body[:2500]}\n")
    (out / "issue.md").write_text("\n".join(parts))

    task_dir = Path(a.tasks_dir) / a.task
    if a.ref:
        task_dir = Path(tempfile.mkdtemp()) / a.task
        task_dir.mkdir()
        archive = subprocess.run(["git", "-C", a.repo_dir, "archive", a.ref, f"tasks/{a.task}"], capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "--strip-components=2", "-C", str(task_dir)], input=archive, check=True)
        log(f"task taken from commit {a.ref[:9]}")
    sid = upload(a.base, task_dir)
    log(f"uploaded {a.task} as {sid}")
    # Without --build the image is not built: the Docker-free stages only need the extracted files.
    call(a.base, "POST", f"/submissions/{sid}/validate" + ("" if a.build else "?build=false"))
    if a.build:
        log(f"build: {wait(a.base, sid, 'build', log, a.timeout)}")
    stage_status = {}
    for spec in a.stages.split(","):
        name = spec.split("?")[0]
        key = KEYS.get(name, name)
        started = None
        for _ in range(24):  # the files may not be extracted yet
            try:
                call(a.base, "POST", f"/submissions/{sid}/{spec}")
                started = True
                break
            except Exception as e:  # noqa: BLE001
                started = str(e)
                time.sleep(5)
        if started is not True:
            stage_status[name] = f"not started: {started}"
            log(f"{name}: not started: {started}")
            continue
        stage_status[name] = wait(a.base, sid, key, log, a.timeout)
        log(f"{name}: {stage_status[name]}")
    result = call(a.base, "GET", f"/submissions/{sid}")
    result["_test"] = {"task": a.task, "issues": a.issue, "stages": stage_status, "submission_id": sid}
    (out / "result.json").write_text(json.dumps(result, indent=1))
    log("saved result.json")


if __name__ == "__main__":
    sys.exit(main())
