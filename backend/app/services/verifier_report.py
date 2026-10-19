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
        "flags": _flags(reward, ctrf) + _details_flags(details, reward),
        "facts": None,
    }


def summary_lines(report: dict) -> str:
    """Plain text for the trial log (the Review Report reads log tails)."""
    lines = [f"raw reward: {report['reward_raw']}"]
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
        lines.append(
            f"users: agent {facts['agent_user'] or 'image default'}, verifier "
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
                "grader or a rule the instruction does not state (contested, needs a human: check that test "
                "and the instruction behind it; the later failures may just follow from it)"
            )
    return {
        "failing_trials": len(failing),
        "failing_tests": failing_tests,
        "closest": {"trial": best_i, "passed": best["passed"], "tests": best["tests"]},
        "notes": notes,
    }
