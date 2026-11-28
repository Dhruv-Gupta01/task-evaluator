"""Five rules that read stored results and say something a reviewer should know (advisory, never a gate).

Adapted from the post-fixes branch's `flag_results.py`, where they were Flags 15.6, 15.11, 15.12, 15.31 and 15.36.
This branch has no Flag framework or rulings, so each rule returns plain warning lines that start with the rule id;
`submission_service` puts them in `SubmissionSchema.result_warnings` (the page's Results review box) and the
evidence summary. Left out on purpose: 15.20 (no arm64 run; this platform runs every task on linux/arm64, so it
cannot fire, and its reverse would fire on every submission) and 15.27 (agent timeout far above the slowest trial;
Terminal-Bench tells authors to declare 8 hours, so it warned on nearly every conforming task)."""

import json
import math
import re

from app.config import get_settings

HARBOR_KINDS = ("oracle", "oracle_stress", "nop", "agent", "cheat_trial")
_TIMEOUT_RE = re.compile(r"TimeoutError|timed out|AgentTimeout")
_AGENT_RE = re.compile(r"harbor run\b.*?--agent\s+(\S+)")
_MODEL_RE = re.compile(r"harbor run\b.*?--model\s+(\S+)")


def _report(run) -> dict:
    try:
        data = json.loads(run.verifier_json) if getattr(run, "verifier_json", None) else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _ctrf(run) -> dict:
    return _report(run).get("ctrf") or {}


def _of(runs, *kinds):
    return [r for r in runs if r.kind in kinds]


def _finished(trials):
    return [t for t in trials if t.status in ("passed", "failed") and t.reward is not None]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n attempts."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def pass_rate_text(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k} of {n} passed (95% interval {lo:.0%} to {hi:.0%})"


def rule_duplicate_names(runs) -> list[str]:
    """15.6: ctrf.json gives several cases the same name, so the per-test view cannot tell which case failed.
    Found on real data: the ctrf plugin writes every `@pytest.mark.parametrize` case as `file::function`, dropping
    pytest's parameter ids, so unique ids in the test file do not help; the case is in `test-stdout.txt`."""
    evidence = sorted({
        f"{r.kind} {r.run_index}: {', '.join(_ctrf(r)['duplicate_names'][:5])}"
        for r in _of(runs, *HARBOR_KINDS) if _ctrf(r).get("duplicate_names")
    })
    if not evidence:
        return []
    return [f"15.6: ctrf.json lists several cases under one name ({'; '.join(evidence[:5])}): the ctrf plugin drops "
            "pytest's parameter ids, so the per-test view cannot tell which case failed. Read the trial's "
            "verifier/test-stdout.txt for the case."]


def rule_interval(k: int, n: int) -> list[str]:
    """15.11: too few scored trials for a pass rate; give counts and the interval, not a percentage."""
    minimum = get_settings().min_trials_for_rate
    if n == 0 or n >= minimum:
        return []
    return [f"15.11: {n} scored trial(s) cannot show a pass rate: {pass_rate_text(k, n)}. Report the counts and "
            f"the interval, not a percentage; the minimum here is {minimum}."]


def separating_tests(trials) -> tuple[list[str], int]:
    """Tests that failed in some trials and passed in others, and the total test count."""
    reports = [c for c in (_ctrf(t) for t in trials) if c.get("tests")]
    if len(reports) < 2:
        return [], max((c.get("tests") or 0 for c in reports), default=0)
    failed_in: dict[str, int] = {}
    for c in reports:
        for name in set(c.get("failed_names") or []):
            failed_in[name] = failed_in.get(name, 0) + 1
    return sorted(n for n, count in failed_in.items() if count < len(reports)), max(c["tests"] for c in reports)


def rule_separating(runs) -> list[str]:
    """15.12: very few tests ever separate the trials, so a trial's test count says little."""
    trials = _finished(_of(runs, "agent"))
    if len(trials) < 2 or not any(_ctrf(t).get("failed_names") for t in trials):
        return []
    sep, total = separating_tests(trials)
    if not total or len(sep) > max(3, total // 5):
        return []
    listed = f" ({', '.join(sep[:10])})" if sep else ""
    return [f"15.12: only {len(sep)} of {total} tests ever separate the trials{listed}, so a trial's test count "
            "is no measure of how close it came."]


def trial_agent_model(run) -> tuple[str | None, str | None]:
    """The agent and model a trial ran with, read from the `harbor run` command its logs record."""
    text = run.logs or ""
    agent, model = _AGENT_RE.search(text), _MODEL_RE.search(text)
    return (agent.group(1) if agent else None), (model.group(1) if model else None)


def model_breakdown(runs) -> list[dict]:
    """Finished agent trials (with a reward) grouped by agent and model, in first-seen order, so a pass rate made
    of several models (an evidence set appends trials) stays readable."""
    groups: dict[tuple, dict] = {}
    for t in sorted(_finished(_of(runs, "agent")), key=lambda r: r.run_index):
        agent, model = trial_agent_model(t)
        g = groups.setdefault((agent, model), {"agent": agent, "model": model, "passed": 0, "n": 0})
        g["n"] += 1
        g["passed"] += 1 if t.reward >= 1 else 0
    return list(groups.values())


def rule_cheap_model(runs) -> list[str]:
    """15.36: the cheap small model (CHEAP_AGENT_MODEL) solved the task, the loudest shortcut alarm."""
    cheap = get_settings().cheap_agent_model
    if not cheap:
        return []
    trials = [t for t in _finished(_of(runs, "agent")) if trial_agent_model(t)[1] == cheap]
    passed = sum(1 for t in trials if t.reward >= 1)
    if not passed:
        return []
    return [f"15.36: the cheap model {cheap} solved the task ({pass_rate_text(passed, len(trials))}): check for a "
            "shortcut before trusting the difficulty."]


def timed_out(runs) -> list:
    """Oracle and agent trials that ended with no reward because of a timeout, with known start and end."""
    return [t for t in _of(runs, "agent", "oracle")
            if t.reward is None and t.started_at and t.finished_at and _TIMEOUT_RE.search(t.logs or "")]


def rule_timeout_under_load(runs, other_runs) -> list[str]:
    """15.31: a trial timed out while another Harbor run was going, so the timeout says little about the task.
    `other_runs` are Harbor runs of this and other submissions (the caller looks them up)."""
    lines = []
    for t in timed_out(runs):
        overlap = [o for o in other_runs if o is not t and o.kind in HARBOR_KINDS and o.started_at and o.finished_at
                   and o.started_at < t.finished_at and o.finished_at > t.started_at]
        if overlap:
            lines.append(f"15.31: {t.kind} {t.run_index} timed out while {len(overlap)} other Harbor run(s) were "
                         "running; a timeout under load says little about the task, so rerun it alone.")
    return lines


def evaluate(runs, other_runs, scored: tuple[int, int]) -> list[str]:
    """All five rules. `scored` is (passed, scored trials) as the pass rate counts them. A rule that crashes
    reports "rule error" instead of hiding the others."""
    checks = (
        ("15.6", lambda: rule_duplicate_names(runs)),
        ("15.11", lambda: rule_interval(*scored)),
        ("15.12", lambda: rule_separating(runs)),
        ("15.31", lambda: rule_timeout_under_load(runs, other_runs)),
        ("15.36", lambda: rule_cheap_model(runs)),
    )
    out: list[str] = []
    for rule_id, check in checks:
        try:
            out.extend(check())
        except Exception as exc:  # noqa: BLE001 - one broken rule must not hide the others
            out.append(f"{rule_id}: rule error: {type(exc).__name__}: {exc}")
    return out
