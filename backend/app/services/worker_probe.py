"""Worker probe (NR-10): does a Reward Kit Worker really drop privileges in the task's own verifier image?

Reward Kit 0.2.4's Worker verifies its own boundary before it runs a command (a non-root uid and gid, no
supplementary groups, NoNewPrivs, empty capability sets, private paths unreachable) and raises
WorkerSetupError when it cannot. This probe goes further than that self-check: it builds the task's
`tests/Dockerfile`, runs a Worker in it as root (as the verifier does) and has the Worker run a script that
tries to read /tests, write the reward file, keep a background process and read the environment. The
script's output and what is left afterwards are judged. Advisory evidence, never a gate; static analysis
(`verifier_isolation.analyze_worker_requirement`) decides whether the task uses a Worker at all.

What it does not cover: a task that passes its own `private_paths` (the probe uses the default
/tests and /logs/verifier), a Worker given extra `files` or `env`, and a grader that runs agent code
outside the Worker (that is the Static Check's job).
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from app.services import harborignore
from app.services.verifier_isolation import analyze_worker_requirement

SENTINEL = "SENTINEL"
# the leftover is `sleep 600` either as `sh -c "sleep 600"` (one argument with a space) or `sleep`, `600`
LEFTOVER_PATTERN = rb"sleep[ \x00]600"
MARKER = "WORKER_PROBE_JSON="
_ALLOWED_ENV = {"HOME", "LANG", "LOGNAME", "PATH", "PWD", "OLDPWD", "SHLVL", "TMPDIR", "USER", "_"}

# What the Worker runs. Output is KEY=value lines; the driver adds what it sees from outside afterwards.
PROBE_SCRIPT = r"""
echo "UID=$(id -u)"
echo "GID=$(id -g)"
echo "GROUPS=$(id -G)"
grep -E '^(NoNewPrivs|CapPrm|CapEff|CapAmb):' /proc/self/status | tr -d '\t ' | sed 's/:/=/'
ls /tests >/dev/null 2>&1 && echo "TESTS_LISTABLE=1" || echo "TESTS_LISTABLE=0"
cat /tests/.worker-probe-canary >/dev/null 2>&1 && echo "TESTS_READABLE=1" || echo "TESTS_READABLE=0"
( echo HACKED >> /logs/verifier/reward.txt ) 2>/dev/null && echo "REWARD_WRITABLE=1" || echo "REWARD_WRITABLE=0"
( echo HACKED > /logs/verifier/reward.json ) 2>/dev/null && echo "REWARD_JSON_CREATABLE=1" || echo "REWARD_JSON_CREATABLE=0"
cat /logs/verifier/reward.txt >/dev/null 2>&1 && echo "REWARD_READABLE=1" || echo "REWARD_READABLE=0"
echo "ENV=$(env | cut -d= -f1 | sort | tr '\n' ' ')"
setsid sh -c 'sleep 600' >/dev/null 2>&1 &
echo "SPAWNED=1"
"""

# Runs inside the verifier image as root, with the venv's python that has rewardkit.
DRIVER = r'''
import json
import re
from pathlib import Path
from rewardkit import Worker, WorkerError, WorkerSetupError

SCRIPT = __SCRIPT__
Path("/tests").mkdir(parents=True, exist_ok=True)
Path("/tests/.worker-probe-canary").write_text("canary")
Path("/logs/verifier").mkdir(parents=True, exist_ok=True)
Path("/logs/verifier/reward.txt").write_text("__SENTINEL__")
out = {}
try:
    result = Worker().run(SCRIPT, timeout_sec=60)
    out["returncode"] = result.returncode
    out["stdout"] = result.stdout.decode(errors="replace") if isinstance(result.stdout, bytes) else result.stdout
    out["stderr"] = result.stderr
except WorkerSetupError as exc:
    out["setup_error"] = str(exc)
except WorkerError as exc:
    out["worker_error"] = str(exc)
left = []
for cmd in Path("/proc").glob("[0-9]*/cmdline"):
    try:
        if re.search(__LEFTOVER__, cmd.read_bytes()):
            left.append(cmd.parent.name)
    except OSError:
        pass
out["leftover_pids"] = left
try:
    out["reward_after"] = Path("/logs/verifier/reward.txt").read_text()
except OSError as exc:
    out["reward_after"] = f"unreadable: {exc}"
print("__MARKER__" + json.dumps(out))
'''


def parse_driver_output(text: str) -> dict | None:
    for line in reversed(text.splitlines()):
        if line.startswith(MARKER):
            try:
                return json.loads(line[len(MARKER):])
            except ValueError:
                return None
    return None


def _kv(stdout: str) -> dict[str, str]:
    out = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _caps_empty(kv: dict[str, str]) -> bool:
    try:
        return all(int(kv[k], 16) == 0 for k in ("CapPrm", "CapEff", "CapAmb"))
    except (KeyError, ValueError):
        return False


def judge(raw: dict) -> dict:
    """{status, checks: [{name, ok, detail}], note} from the driver's output."""
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    failure = raw.get("setup_error") or raw.get("worker_error")
    if failure:
        add("worker_started", False, failure[:300])
        return {
            "status": "failed", "checks": checks,
            "note": "The Worker could not start or install its protections here. That is the environment (kernel, "
                    "image or user), not the agent: a real grader would end with no score (a grader error). "
                    "On Docker Desktop this can mean a missing kernel feature.",
        }
    kv = _kv(raw.get("stdout", ""))
    add("worker_started", raw.get("returncode") == 0 and bool(kv), f"exit {raw.get('returncode')}")
    uid, gid = kv.get("UID", ""), kv.get("GID", "")
    add("unprivileged_uid", uid.isdigit() and uid != "0" and uid == gid, f"uid {uid}, gid {gid}")
    add("no_supplementary_groups", kv.get("GROUPS", "") == gid and gid != "", f"groups {kv.get('GROUPS', '?')}")
    add("no_new_privs", kv.get("NoNewPrivs") == "1", f"NoNewPrivs {kv.get('NoNewPrivs', '?')}")
    add("no_capabilities", _caps_empty(kv), f"CapPrm {kv.get('CapPrm', '?')}, CapEff {kv.get('CapEff', '?')}")
    add("tests_not_readable", kv.get("TESTS_LISTABLE") == "0" and kv.get("TESTS_READABLE") == "0",
        f"listable {kv.get('TESTS_LISTABLE', '?')}, readable {kv.get('TESTS_READABLE', '?')}")
    add("reward_not_writable",
        kv.get("REWARD_WRITABLE") == "0" and kv.get("REWARD_JSON_CREATABLE") == "0" and kv.get("REWARD_READABLE") == "0",
        f"txt writable {kv.get('REWARD_WRITABLE', '?')}, json creatable {kv.get('REWARD_JSON_CREATABLE', '?')}, "
        f"readable {kv.get('REWARD_READABLE', '?')}")
    add("reward_intact", raw.get("reward_after") == SENTINEL, f"reward.txt afterwards: {raw.get('reward_after')!r}")
    env = set(kv.get("ENV", "").split())
    extra = sorted(env - _ALLOWED_ENV)
    add("env_not_inherited", not extra, f"unexpected: {', '.join(extra)}" if extra else "only the Worker's own variables")
    left = raw.get("leftover_pids") or []
    add("leftovers_killed", not left, f"left running: {', '.join(left)}" if left else "none")
    return {"status": "passed" if all(c["ok"] for c in checks) else "failed", "checks": checks, "note": ""}


def _python_for_rewardkit(test_sh: str) -> str:
    m = re.search(r"(\S*python[\d.]*)\s+(?:-\w+\s+)*-m\s+rewardkit", test_sh)
    return m.group(1) if m else "python3"


# Positive control: the same script as plain root, no Worker. Every boundary check must then fail, which
# shows the probe can tell a protected run from an unprotected one.
_CONTROL_RUN = (
    'import subprocess\n    result = type("R", (), {})()\n'
    '    p = subprocess.run(["/bin/sh", "-c", SCRIPT], capture_output=True, timeout=60)\n'
    '    result.returncode, result.stdout, result.stderr = p.returncode, p.stdout, p.stderr.decode(errors="replace")'
)


def run_worker_probe(task_root: Path, platform: str | None = None, build_timeout: float = 1800,
                     unsandboxed_control: bool = False) -> dict:
    """Skipped (with a note) unless the task uses a Worker and has a separate verifier image.
    `unsandboxed_control` runs the probe script as plain root instead of through a Worker (a self-test)."""
    analysis = analyze_worker_requirement(task_root)
    if analysis["result"] != "present":
        return {"skipped": True, "status": "passed", "checks": [],
                "note": f"No Worker in this task's grader (classified {analysis['result']}); nothing to probe."}
    tests = task_root / "tests"
    if not (tests / "Dockerfile").is_file():
        return {"skipped": True, "status": "passed", "checks": [],
                "note": "The grader uses a Worker but there is no tests/Dockerfile (a shared verifier runs in the "
                        "agent's container); the probe needs the verifier image."}
    from app.services.tier0 import docker_runner as dr  # imported late: needs Docker only when it runs

    dr.require_docker()
    suffix = uuid.uuid4().hex[:8]
    tag, name = f"probe-worker-{suffix}:latest", f"probe-worker-{suffix}"
    workdir = Path(tempfile.mkdtemp(prefix="worker-probe-"))
    cid = None
    try:
        context = workdir / "tests"
        # What Harbor builds and uploads: tests/ minus the files a .harborignore rule keeps out (as Tier 0's replica).
        harborignore.copy_tests(task_root, context)
        dr._build(context / "Dockerfile", context, tag, platform, build_timeout)
        cid = dr._start(tag, platform, name)
        python = _python_for_rewardkit((tests / "test.sh").read_text() if (tests / "test.sh").is_file() else "")
        driver = DRIVER
        if unsandboxed_control:
            driver = driver.replace("result = Worker().run(SCRIPT, timeout_sec=60)", _CONTROL_RUN)
        driver = driver.replace("__SCRIPT__", repr(PROBE_SCRIPT)).replace("__LEFTOVER__", repr(LEFTOVER_PATTERN)).replace("__SENTINEL__", SENTINEL).replace("__MARKER__", MARKER)
        res = dr._run(["docker", "exec", "-i", cid, python, "-I", "-"], timeout=300, check=False, stdin=driver)
        raw = parse_driver_output(res.stdout)
        if raw is None:
            return {"skipped": False, "status": "failed", "checks": [],
                    "note": f"The probe did not report. exit {res.returncode}; stderr: {res.stderr.strip()[-600:]}"}
        result = judge(raw)
        result["skipped"] = False
        result["python"] = python
        return result
    finally:
        if cid:
            dr._rm(cid)
        dr._run(["docker", "rmi", "-f", tag], timeout=120, check=False)
        shutil.rmtree(workdir, ignore_errors=True)
