"""What the verifier actually reported for one trial, beyond the final 0 or 1.

Harbor's result.json carries only the reward. The tests' own report lives in
the trial's verifier/ folder: ctrf.json (per-test pass/fail/skip, written by
the pytest CTRF plugin) and, for tasks that compute a detailed score, a
sidecar such as reward-details.json. Reading them lets us show near misses
and notice a reward that disagrees with the tests behind it.

Everything here is advisory evidence for a human; nothing gates a stage."""

import json
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


def read_verifier_report(verifier_dir: Path, reward: float | None) -> dict | None:
    """None when the trial has no verifier folder at all (crash before grading)."""
    if not verifier_dir.is_dir():
        return None
    ctrf = _read_ctrf(verifier_dir / "ctrf.json")
    return {
        "reward_raw": reward,
        "ctrf": ctrf,
        "details": _read_details(verifier_dir),
        "flags": _flags(reward, ctrf),
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
                f"all {k} failing trials first failed at `{next(iter(firsts))}`: check that test and "
                "the instruction behind it (the later failures may just follow from it)"
            )
    return {
        "failing_trials": len(failing),
        "failing_tests": failing_tests,
        "closest": {"trial": best_i, "passed": best["passed"], "tests": best["tests"]},
        "notes": notes,
    }
