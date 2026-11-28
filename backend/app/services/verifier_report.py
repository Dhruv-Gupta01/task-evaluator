"""What the verifier actually reported for one trial, beyond the final 0 or 1.

Harbor's result.json carries only the reward. The tests' own report lives in
the trial's verifier/ folder: ctrf.json (per-test pass/fail/skip, written by
the pytest CTRF plugin) and, for tasks that compute a detailed score, a
sidecar such as reward-details.json. Reading them lets us show near misses
and notice a reward that disagrees with the tests behind it.

Everything here is advisory evidence for a human; nothing gates a stage."""

import json
import platform
import re
import tomllib
from datetime import datetime
from pathlib import Path

MAX_NAMES = 50
MAX_ALL_NAMES = 2000  # every test name, for comparing two runs (regrade's tests_missing)
MAX_NAME_CHARS = 200
MAX_DETAILS_CHARS = 4000

# Sidecar names tasks have invented for a detailed score (TB 4.1 issue: ~10
# tasks, no agreed name yet). Collected as-is, never interpreted.
DETAIL_FILES = (
    "reward-details.json",
    "reward_details.json",
    "score_breakdown.json",
    "breakdown.json",
    "metrics.json",
    # #1390 lists these too (freight-dispatch-shift, heat-pump-warranty, legacy-utility-triage, vba-userform-port).
    "trace_results.json",
    "trace_summary.json",
)


def _short(name: object) -> str:
    return str(name)[:MAX_NAME_CHARS]


def _read_ctrf(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        results = data["results"]
        summary = results["summary"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    tests = results.get("tests") or []
    names = [str(t.get("name")) for t in tests]
    failed_names = [_short(t.get("name")) for t in tests if t.get("status") == "failed"]
    skipped_names = [_short(t.get("name")) for t in tests if t.get("status") == "skipped"]
    return {
        "tests": int(summary.get("tests") or len(tests)),
        "passed": int(summary.get("passed") or 0),
        "failed": int(summary.get("failed") or 0),
        "skipped": int(summary.get("skipped") or 0),
        "other": int(summary.get("other") or 0) + int(summary.get("pending") or 0),
        "failed_names": failed_names[:MAX_NAMES],
        "skipped_names": skipped_names[:MAX_NAMES],
        "names": sorted(set(names))[:MAX_ALL_NAMES],
    }


def _read_details(verifier_dir: Path) -> dict | None:
    for name in DETAIL_FILES:
        path = verifier_dir / name
        if path.is_file():
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            return {"file": name, "content": text[:MAX_DETAILS_CHARS]}
    return None


# Keys a score-details file uses for the final reward or verdict. Other numbers
# in it (an F1, a speedup) are raw measurements and are not compared.
_DETAIL_REWARD_KEYS = ("reward", "final_reward", "total_reward")
_DETAIL_VERDICT_KEYS = ("passed", "pass", "success")


def _details_flags(details: dict | None, reward: float | None) -> list[str]:
    """A.3: the sidecar's own final reward or verdict should agree with the
    reward Harbor recorded."""
    if not details or reward is None:
        return []
    try:
        data = json.loads(details["content"])
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    for key in _DETAIL_REWARD_KEYS:
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value - reward) > 1e-9:
            return [f"{details['file']} says {key} = {value:g} but the recorded reward is {reward:g}"]
    for key in _DETAIL_VERDICT_KEYS:
        value = data.get(key)
        if isinstance(value, bool) and value != (reward >= 1.0):
            return [f"{details['file']} says {key} = {str(value).lower()} but the recorded reward is {reward:g}"]
    return []


def _reward_file_value(path: Path) -> float | None:
    text = path.read_text(errors="replace").strip()
    if path.suffix == ".json":
        data = json.loads(text)
        value = data.get("reward") if isinstance(data, dict) else None
        return float(value) if isinstance(value, (int, float)) else None
    return float(text)


def reward_file_flags(verifier_dir: Path, reward: float | None) -> list[str]:
    """B.3: what the reward files say once the trial is over. Harbor reads
    reward.json if it exists, else reward.txt, right after test.sh exits, and
    in shared mode it does not empty /logs/verifier first. Read again here,
    after the container is gone, a file that disagrees with the recorded
    reward was rewritten after grading (a process left running), and a
    reward.json next to a different reward.txt was probably not written by
    test.sh at all."""
    values: dict[str, float | None] = {}
    for name in ("reward.json", "reward.txt"):
        path = verifier_dir / name
        if path.is_file():
            try:
                values[name] = _reward_file_value(path)
            except (OSError, ValueError):
                values[name] = None
    flags: list[str] = []
    used = "reward.json" if "reward.json" in values else "reward.txt" if "reward.txt" in values else None
    if used and reward is not None and values[used] is not None and abs(values[used] - reward) > 1e-9:
        flags.append(
            f"{used} now says {values[used]:g} but Harbor recorded {reward:g}: the file changed after "
            "grading (was a process still running?)"
        )
    json_v, txt_v = values.get("reward.json"), values.get("reward.txt")
    if json_v is not None and txt_v is not None and abs(json_v - txt_v) > 1e-9:
        flags.append(
            f"reward.json ({json_v:g}) and reward.txt ({txt_v:g}) disagree; Harbor uses reward.json. If "
            "test.sh writes only reward.txt, the reward.json came from somewhere else: review this trial"
        )
    return flags


# What a verifier-side failure looks like in the tests' own output: the tests
# ran, but something around them (fixture teardown, a privilege drop, writing
# the report) broke.
_HARNESS_ERRORS = (
    (re.compile(r"ERROR at teardown of (\S+)"), "an error at teardown of `{0}`"),
    (re.compile(r"PermissionError[^\n]*"), "a PermissionError (`{0}`)"),
    (re.compile(r"[^\n]*/logs/verifier[^\n]*Permission denied|Permission denied[^\n]*/logs/verifier"),
     "the verifier could not write to /logs/verifier"),
    (re.compile(r"INTERNALERROR>"), "a pytest INTERNALERROR"),
)


def harness_error_flags(trial_dir: Path, reward: float | None) -> list[str]:
    """G.5: a non-passing trial whose test output shows a teardown or
    permission error may have been failed by the grader or the harness, not
    by the agent. Works with the "all tests passed but reward 0" flag."""
    if reward is not None and reward >= 1.0:
        return []
    try:
        text = (trial_dir / "verifier" / "test-stdout.txt").read_text(errors="replace")
    except OSError:
        return []
    found: list[str] = []
    for pattern, label in _HARNESS_ERRORS:
        match = pattern.search(text)
        if match:
            found.append(label.format(match.group(1) if match.groups() else match.group(0).strip()[:120]))
    if not found:
        return []
    return [
        "the test output shows " + "; ".join(found)
        + ": possibly a grader or harness error rather than the agent's failure (check the verifier output)"
    ]


def count_flag(report: dict | None, expected_tests: int | None) -> str | None:
    """A.2: a run whose report lists a different number of tests than the
    passing Oracle runs did. Tests can go missing (not collected, skipped at
    import, removed by the submission) or be added; either way the reward
    no longer rests on the same checks. Computed when shown, so older runs
    get it too."""
    ctrf = (report or {}).get("ctrf")
    if not ctrf or not expected_tests or ctrf.get("tests") == expected_tests:
        return None
    return (
        f"{ctrf.get('tests')} tests in this run's report, but the passing Oracle runs had {expected_tests}: "
        "tests were not collected, were removed, or were added"
    )


def _flags(reward: float | None, ctrf: dict | None) -> list[str]:
    flags: list[str] = []
    if reward is not None and reward not in (0.0, 1.0):
        flags.append(
            f"non-binary reward {reward:g}: Terminal-Bench requires exactly 0 or 1 "
            "(the platform counts anything below 1 as a failure)"
        )
    if ctrf is None:
        flags.append("no per-test record: verifier/ctrf.json is missing or unreadable")
        return flags
    if reward is not None and reward >= 1.0:
        if ctrf["tests"] == 0:
            flags.append("reward 1 but the test report lists no tests")
        elif ctrf["passed"] == 0:
            flags.append(f"reward 1 but no test passed ({ctrf['skipped']} skipped, {ctrf['failed']} failed)")
        if ctrf["skipped"] and ctrf["passed"]:
            flags.append(f"reward 1 but {ctrf['skipped']} test(s) were skipped")
        if ctrf["failed"] and ctrf["passed"]:
            flags.append(f"reward 1 but {ctrf['failed']} test(s) failed")
    elif reward is not None and ctrf["tests"] > 0 and ctrf["passed"] == ctrf["tests"]:
        flags.append(
            f"all {ctrf['tests']} tests passed but the reward is {reward:g}: likely a grader or "
            "harness problem, not the agent"
        )
    return flags


# Harbor's own defaults when task.toml names no limit.
DEFAULT_LIMIT_SEC = 600.0
# Share of a limit that counts as "close to it".
NEAR_LIMIT = 0.8
# What the trial's own output says when the CPU architecture is the problem.
ARCH_SIGNATURES = ("exec format error", "illegal instruction", "cannot execute binary file")


def _seconds(span: object) -> float | None:
    """Seconds between a result.json {started_at, finished_at} pair."""
    if not isinstance(span, dict) or not span.get("started_at") or not span.get("finished_at"):
        return None
    try:
        start = datetime.fromisoformat(span["started_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(span["finished_at"].replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((end - start).total_seconds(), 1)


def read_run_facts(result: dict, task_toml: Path | None, docker_platform: str) -> dict:
    """How the trial was run: limits, time used, resources, architecture and
    which users ran the agent and the verifier. Facts only; `flags` judges them."""
    try:
        task = tomllib.loads(task_toml.read_text()) if task_toml else {}
    except (OSError, ValueError):
        task = {}
    env = task.get("environment") or {}
    cfg = result.get("config") or {}
    cfg_env = cfg.get("environment") or {}
    return {
        "agent_seconds": _seconds(result.get("agent_execution")),
        "agent_limit_sec": float((task.get("agent") or {}).get("timeout_sec") or DEFAULT_LIMIT_SEC),
        "verifier_seconds": _seconds(result.get("verifier")),
        "verifier_limit_sec": float((task.get("verifier") or {}).get("timeout_sec") or DEFAULT_LIMIT_SEC),
        "cpus": cfg_env.get("override_cpus") or env.get("cpus"),
        "memory_mb": cfg_env.get("override_memory_mb") or env.get("memory_mb"),
        "platform": docker_platform or None,
        "host_arch": platform.machine(),
        # None means Harbor used the image's own USER (root unless the Dockerfile sets one).
        "agent_user": (task.get("agent") or {}).get("user") or cfg.get("user_agent"),
        "agent_uid": None,  # filled from the live container by harbor_runner._apply_probe
        "verifier_user": (task.get("verifier") or {}).get("user"),
        "verifier_mode": result.get("verifier_environment_mode"),
    }


def facts_flags(facts: dict, trial_dir: Path) -> list[str]:
    flags: list[str] = []
    for label, used, limit in (
        ("agent", facts["agent_seconds"], facts["agent_limit_sec"]),
        ("verifier", facts["verifier_seconds"], facts["verifier_limit_sec"]),
    ):
        if used is not None and limit and used >= NEAR_LIMIT * limit:
            flags.append(
                f"the {label} used {used:g}s of its {limit:g}s limit: a slower machine could "
                f"push this trial over the limit"
            )
    for name in ("verifier/test-stdout.txt", "trial.log"):
        try:
            text = (trial_dir / name).read_text(errors="replace").lower()
        except OSError:
            continue
        hit = next((sig for sig in ARCH_SIGNATURES if sig in text), None)
        if hit:
            flags.append(
                f"`{hit}` in {name}: a binary built for another CPU architecture "
                f"(this run used {facts['platform'] or 'the default platform'} on a {facts['host_arch']} host)"
            )
            break
    return flags


MAX_CHECKS = 200
MAX_CHECK_MESSAGE_CHARS = 300

# Reward Kit's own file: {group: {"criteria": [{name, value, error?, reasoning?}],
# "components": [{"detail": {...}}]}}. Other sidecars have their own shapes and
# are only collected as text, never interpreted.
REWARD_DETAILS_FILE = "reward-details.json"
# Written by a task's own grader when Reward Kit's Worker could not set up its
# isolation (the 4.1 tasks delete partial score files and exit with code 2).
WORKER_SETUP_MARKER = "worker-setup-error"
# Harbor raises these after the verifier ran but left no usable reward.
_NO_REWARD_EXCEPTIONS = ("RewardFileNotFoundError", "RewardFileEmptyError", "VerifierOutputParseError")
_SETUP_ERROR_RE = re.compile(
    r"WorkerSetupError|worker setup or cleanup failed|Worker could not install required protections"
)
_WORKER_ERROR_RE = re.compile(r"\bWorkerError\b")


def _walk_criteria(node: object, group: str, out: list[dict]) -> None:
    if not isinstance(node, dict):
        return
    for item in node.get("criteria") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        value = item.get("value")
        number = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        message = item.get("error") or item.get("reasoning") or ""
        out.append(
            {
                "name": _short(item["name"]),
                "group": group,
                "passed": number is not None and number >= 1.0,
                "value": number,
                "message": str(message)[:MAX_CHECK_MESSAGE_CHARS],
            }
        )
    for component in node.get("components") or []:
        if isinstance(component, dict):
            _walk_criteria(component.get("detail"), group, out)


def read_checks(verifier_dir: Path) -> list[dict] | None:
    """Reward Kit's per-check results (name, pass or fail, message) from
    reward-details.json; None when the file is missing or not in its format."""
    path = verifier_dir / REWARD_DETAILS_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    out: list[dict] = []
    for group, node in data.items():
        _walk_criteria(node, str(group), out)
    return out[:MAX_CHECKS] if out else None


def _text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _line_with(text: str, pattern: re.Pattern) -> str:
    match = pattern.search(text)
    if not match:
        return ""
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    return text[start : end if end != -1 else len(text)].strip()[:200]


def detect_grader_error(trial_dir: Path, result: dict, reward: float | None) -> dict | None:
    """The verifier itself broke, which says nothing about the agent: Reward
    Kit's Worker could not set up its isolation (marker file, WorkerSetupError
    in the output), or the verifier ran but left no usable reward file. A trial
    that never reached grading (build failure, agent crash, timeout) is not one.
    Harbor 0.23 does not record the grader's exit code, so exit code 2 shows up
    as the missing reward file Reward Kit's fail-closed handling leaves behind.
    Returns {reason, evidence} or None. A trial with a reward is never one."""
    if reward is not None:
        return None
    verifier_dir = trial_dir / "verifier"
    if (verifier_dir / WORKER_SETUP_MARKER).exists():
        return {
            "reason": "worker_setup_error",
            "evidence": f"the verifier left a {WORKER_SETUP_MARKER} marker file",
        }
    output = _text(verifier_dir / "test-stdout.txt") + "\n" + _text(verifier_dir / "test-stderr.txt")
    line = _line_with(output, _SETUP_ERROR_RE)
    if line:
        return {"reason": "worker_setup_error", "evidence": line}
    exc = result.get("exception_info") or {}
    if exc.get("exception_type") in _NO_REWARD_EXCEPTIONS:
        return {
            "reason": "no_reward_file",
            "evidence": f"{exc['exception_type']}: {str(exc.get('exception_message') or '')[:200]}",
        }
    return None


def worker_error_seen(verifier_dir: Path) -> bool:
    """Reward Kit's WorkerError: the agent's own code failed (crashed, errored
    or timed out) while the grader ran it. A fair failure, not a grader fault."""
    output = _text(verifier_dir / "test-stdout.txt") + "\n" + _text(verifier_dir / "test-stderr.txt")
    return bool(_WORKER_ERROR_RE.search(output))


def suggest_verdict(reward: float | None, report: dict | None) -> dict:
    """A pre-filled genuine-or-fake call for one finished trial (a reviewer
    confirms or changes it). `reward` is Harbor's exact reward. Fake means the
    failure is the grader's, not the agent's; likely_genuine means the agent's
    own work failed named checks; unclear means the evidence does not say."""
    report = report or {}
    if report.get("grader_error"):
        ge = report["grader_error"]
        return {
            "verdict": "fake",
            "reason": f"grader problem ({ge['reason'].replace('_', ' ')}): {ge['evidence']}",
            "action": "Rerun; report the task if it repeats.",
        }
    if reward is not None and reward >= 1.0:
        return {"verdict": "pass", "reason": "reward 1", "action": "Spot-check only."}
    if reward is None:
        return {
            "verdict": "unclear",
            "reason": "no reward and no grader-error sign: the trial crashed, timed out or never reached grading",
            "action": "Read the infrastructure flags and the trial log before counting it.",
        }
    failed = [c for c in (report.get("checks") or []) if not c.get("passed")]
    ctrf_failed = ((report.get("ctrf") or {}).get("failed_names")) or []
    names = [c["name"] for c in failed] or list(ctrf_failed)
    if report.get("worker_error"):
        return {
            "verdict": "likely_genuine",
            "reason": "the agent's own code failed while the grader ran it (WorkerError)"
            + (f"; failed checks: {', '.join(names[:5])}" if names else ""),
            "action": "Read the error and confirm.",
        }
    if names:
        return {
            "verdict": "likely_genuine",
            "reason": f"reward {reward:g} with failed checks: {', '.join(names[:5])}"
            + (f" (+{len(names) - 5} more)" if len(names) > 5 else ""),
            "action": "Read the failed checks against the instruction and confirm the tests are fair.",
        }
    return {
        "verdict": "unclear",
        "reason": f"reward {reward:g} but no per-check record says which check failed",
        "action": "Read the trial log; the grader gives no per-check detail.",
    }


def read_verifier_report(verifier_dir: Path, reward: float | None) -> dict | None:
    """None when the trial has no verifier folder at all (crash before grading)."""
    if not verifier_dir.is_dir():
        return None
    ctrf = _read_ctrf(verifier_dir / "ctrf.json")
    details = _read_details(verifier_dir)
    return {
        "reward_raw": reward,
        "ctrf": ctrf,
        "details": details,
        "checks": read_checks(verifier_dir),
        "worker_error": worker_error_seen(verifier_dir),
        "grader_error": None,  # filled by the caller, which has result.json
        "flags": _flags(reward, ctrf) + _details_flags(details, reward),
        "facts": None,
    }


def summary_lines(report: dict) -> str:
    """Plain text for the trial log (the Review Report reads log tails)."""
    lines = [f"raw reward: {report['reward_raw']}"]
    grader_error = report.get("grader_error")
    if grader_error:
        lines.append(
            f"GRADER ERROR ({grader_error['reason'].replace('_', ' ')}): {grader_error['evidence']}. "
            "The grader broke, not the agent: no score, rerun this trial."
        )
    checks = report.get("checks")
    if checks:
        failed_checks = [c["name"] for c in checks if not c["passed"]]
        lines.append(f"named checks: {len(checks) - len(failed_checks)} of {len(checks)} passed")
        if failed_checks:
            lines.append("failed checks: " + ", ".join(failed_checks[:10]))
    ctrf = report["ctrf"]
    if ctrf:
        lines.append(
            f"tests: {ctrf['passed']} of {ctrf['tests']} passed, "
            f"{ctrf['failed']} failed, {ctrf['skipped']} skipped"
        )
        if ctrf["failed_names"]:
            lines.append("failed: " + ", ".join(ctrf["failed_names"][:10]))
    facts = report.get("facts")
    if facts:
        lines.append(
            f"time: agent {facts['agent_seconds']}s of {facts['agent_limit_sec']:g}s, "
            f"verifier {facts['verifier_seconds']}s of {facts['verifier_limit_sec']:g}s"
        )
        lines.append(
            f"resources: cpus {facts['cpus']}, memory {facts['memory_mb']} MB, platform "
            f"{facts['platform']}, host {facts['host_arch']}"
        )
        uid_note = "" if facts.get("agent_uid") is None else f" (uid {facts['agent_uid']})"
        lines.append(
            f"users: agent {facts['agent_user'] or 'image default'}{uid_note}, verifier "
            f"{facts['verifier_user'] or 'image default'}, verifier mode {facts['verifier_mode']}"
        )
    if report["details"]:
        lines.append(f"score details file: {report['details']['file']}")
    for flag in report["flags"]:
        lines.append(f"FLAG: {flag}")
    return "\n".join(lines)


def cluster_failures(reports: list[tuple[int, float | None, dict | None]]) -> dict | None:
    """Across one agent job's trials: which tests the failing trials failed,
    and which trial came closest. `reports` is (trial index, reward, report).

    One test failed by every failing trial while all other tests passed is the
    typical shape of an over-strict grader or an unstated rule, so it is called
    out; whether it really is one is a human call."""
    failing = [
        (i, rep["ctrf"])
        for i, reward, rep in reports
        if reward is not None and reward < 1.0 and rep and rep["ctrf"] and rep["ctrf"]["tests"]
    ]
    if not failing:
        return None

    counts: dict[str, int] = {}
    for _, ctrf in failing:
        for name in set(ctrf["failed_names"]):  # parametrised tests can repeat a name
            counts[name] = counts.get(name, 0) + 1
    failing_tests = [
        {"name": n, "failed_in": c}
        for n, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_NAMES]
    ]

    best_i, best = max(failing, key=lambda t: t[1]["passed"] / t[1]["tests"])
    notes: list[str] = []
    k = len(failing)
    if k >= 2:
        # ctrf lists tests in run order, so the first failed name is where the
        # trial first went wrong; later failures may simply follow from it.
        # Only a shared *first* failure is flagged: tests that merely fail in
        # several trials are listed in failing_tests, not called out.
        firsts = {ctrf["failed_names"][0] for _, ctrf in failing if ctrf["failed_names"]}
        if len(firsts) == 1 and all(ctrf["failed_names"] for _, ctrf in failing):
            notes.append(
                f"all {k} failing trials first failed at `{next(iter(firsts))}`: possibly an over-strict "
                "grader or a rule the instruction does not state (check that test "
                "and the instruction behind it; the later failures may just follow from it)"
            )
    return {
        "failing_trials": len(failing),
        "failing_tests": failing_tests,
        "closest": {"trial": best_i, "passed": best["passed"], "tests": best["tests"]},
        "notes": notes,
    }
