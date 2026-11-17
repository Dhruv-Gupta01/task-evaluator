"""Flag rules that read stored results (batches 4, 6, 7): probes, verifier reports, agent
trials, failure analysis and cheat trials. Each takes the submission's runs and returns the
same RuleResult as the static rules, so one Flag run covers both."""

import json
import math
import re

from app.config import get_settings
from app.services.flag_rules import Finding, RuleResult

JSON_KINDS = {
    "static_checks", "tb_static_checks", "answer_exposure", "rubric_check", "failure_analysis",
    "cheat_trial", "edge_cases", "variant_probe", "weak_probe", "image_inventory", "fresh_build",
    "probes", "flags", "ai_detection", "evidence_runs",
}
HARBOR_KINDS = ("oracle", "oracle_stress", "nop", "agent", "cheat_trial")


def _json(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def _report(run) -> dict:
    return _json(getattr(run, "verifier_json", None)) or {}


def _of(runs, *kinds):
    return [r for r in runs if r.kind in kinds]


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
    return f"{k} successes in {n} attempts (95% interval {lo:.0%} to {hi:.0%}; one model family)"


# ---- probes (batch 4) ---------------------------------------------------------------

def _probes(runs) -> dict | None:
    run = next(iter(_of(runs, "probes")), None)
    data = _json(run.logs) if run else None
    return data if data and not data.get("in_progress") else None


def _probe_rule(runs, rule: str, kinds: tuple[str, ...], verdicts: tuple[str, ...], detail: str) -> RuleResult:
    data = _probes(runs)
    if data is None:
        return RuleResult([], "probes not run yet")
    hits = [p for p in data.get("probes", []) if p["kind"] in kinds and p.get("verdict") in verdicts]
    evidence = [
        f"{p['name']}: {p['verdict']} (reward {p.get('reward')}, {p.get('tests')} test(s) collected, "
        f"control collected {data.get('expected_tests')})"
        for p in hits
    ]
    ran = [p for p in data.get("probes", []) if p["kind"] in kinds]
    return RuleResult([Finding(rule, detail, evidence)] if evidence else [], f"{len(ran)} probe(s), {len(hits)} finding(s)")


def rule_fail_open(runs, ctx=None) -> RuleResult:
    """15.1 (CK-A): removing a data folder the tests read still scores 1."""
    return _probe_rule(runs, "15.1", ("data_removal",), ("failed_open",),
                       "With its test data removed the verifier still gives reward 1, so a broken image "
                       "or a missing folder passes everyone. Assert the expected case count at import.")


def rule_case_count(runs, ctx=None) -> RuleResult:
    """15.8 (CK-A): a data-removal probe collected fewer cases than the control."""
    data = _probes(runs)
    if data is None:
        return RuleResult([], "probes not run yet")
    expected = data.get("expected_tests")
    evidence = [
        f"{p['name']}: {p.get('tests')} test(s) collected, control {expected}"
        for p in data.get("probes", [])
        if p["kind"] == "data_removal" and expected and p.get("tests") is not None and p["tests"] < expected
    ]
    findings = [Finding("15.8", "The verifier collects fewer cases when its data is missing and does not "
                        "notice. Compare the collected count with the expected count.", evidence)] if evidence else []
    return RuleResult(findings, f"control collected {expected} test(s)")


def rule_hostile(runs, ctx=None) -> RuleResult:
    """15.3 (CK-C): a FIFO, symlink, directory or oversized artifact did not score 0 in time."""
    return _probe_rule(runs, "15.3", ("hostile",), ("failed_open", "invalid_reward"),
                       "A hostile artifact did not get reward 0 inside the verifier timeout. An invalid "
                       "reward is worse than 0: Harbor drops the trial. Stat the path, require a regular "
                       "file, bound its size.")


def rule_positive_control(runs, ctx=None) -> RuleResult:
    """15.33 (CK-C): a probe cannot count as held because the control did not reach 1."""
    data = _probes(runs)
    if data is None:
        return RuleResult([], "probes not run yet")
    unverified = [p["name"] for p in data.get("probes", []) if p.get("verdict") == "unverified"]
    findings = [Finding("15.33", "The untouched Oracle did not reach 1 in this probe run, so a probe that "
                        "scored 0 proves nothing.", [f"control reward {data.get('control_reward')}; "
                        f"unverified: {', '.join(unverified)}"])] if unverified else []
    return RuleResult(findings, f"control reward {data.get('control_reward')}")


def rule_null_ladder(runs, ctx=None) -> RuleResult:
    """15.37 (CK-H): a null-ladder rung (plain Nop, empty artifacts) scored 1."""
    return _probe_rule(runs, "15.37", ("null_rung1", "null_rung2"), ("failed_open",),
                       "Doing nothing, or leaving empty files of the right names, passes the verifier.")


# ---- CK-F: reports and logs are valid and honest (batch 4) ---------------------------

def rule_duplicate_names(runs, ctx=None) -> RuleResult:
    """15.6 (S7): ctrf.json gives several parametrized cases the same name."""
    evidence = sorted({
        f"{r.kind} {r.run_index}: {', '.join(_report(r).get('ctrf', {}).get('duplicate_names') or [])}"
        for r in _of(runs, *HARBOR_KINDS) if (_report(r).get("ctrf") or {}).get("duplicate_names")
    })
    findings = [Finding("15.6", "ctrf.json reports several cases under one name, so a failure cannot be traced "
                        "to its case. Give the cases unique ids in the test file (do not bump "
                        "pytest-json-ctrf: the official check pins 0.5.2).", evidence[:5])] if evidence else []
    return RuleResult(findings, f"{len(_of(runs, *HARBOR_KINDS))} Harbor run(s) checked")


def rule_not_run_counted(runs, ctx=None) -> RuleResult:
    """15.9 (H5): a check that did not run is shown as a pass."""
    evidence = []
    for r in _of(runs, "tb_static_checks"):
        data = _json(r.logs) or {}
        if data.get("skip_count"):
            evidence.append(f"TB Static Checks: {data['skip_count']} check(s) not run, stage {r.status}")
    for r in _of(runs, "ai_detection"):
        data = _json(r.logs) or {}
        if data.get("skipped") and r.status == "passed":
            evidence.append("AI Detection: skipped (no GPTZERO_API_KEY) but its stage status is passed")
    findings = [Finding("15.9", "A check that never ran must not read as a pass.", evidence)] if evidence else []
    return RuleResult(findings, f"{len(evidence)} not-run check(s)")


def rule_logs_parse(runs, ctx=None) -> RuleResult:
    """15.10 (P4, S8): a stored result that should be JSON does not parse, or an Oracle/Nop run
    has no valid ctrf.json (the verifier report the official validation requires)."""
    evidence = []
    for r in runs:
        if r.kind in JSON_KINDS and r.logs and r.status in ("passed", "failed") and _json(r.logs) is None:
            evidence.append(f"{r.kind} {r.run_index}: stored result does not parse ({r.logs[:80]!r})")
        if r.kind in ("oracle", "nop") and r.status in ("passed", "failed") and r.reward is not None:
            if not (_report(r).get("ctrf")):
                evidence.append(f"{r.kind} {r.run_index}: no valid ctrf.json (verifier report missing or invalid)")
    findings = [Finding("15.10", "Evidence that cannot be parsed backs up nothing; Terminal-Bench's validation "
                        "fails a run without a valid ctrf.json.", evidence[:10])] if evidence else []
    return RuleResult(findings, f"{len(runs)} stored result(s) checked")


# ---- CK-H, CK-D, CK-G: agent trials and evidence (batch 6) -------------------------------

def _model(run) -> str | None:
    m = re.search(r"(?:-m|--model)\s+(\S+)", run.logs or "")
    return m.group(1) if m else None


def _finished(trials):
    return [t for t in trials if t.status in ("passed", "failed") and t.reward is not None]


def rule_interval(runs, ctx=None) -> RuleResult:
    """15.11 (S6, E11): too few trials for a pass rate; always reported with its interval."""
    trials = _finished(_of(runs, "agent"))
    if not trials:
        return RuleResult([], "no finished agent trials")
    k, n = sum(1 for t in trials if t.reward >= 1), len(trials)
    minimum = get_settings().min_trials_for_rate
    note = pass_rate_text(k, n)
    findings = [Finding("15.11", f"{n} trial(s) cannot show a pass rate: report counts and the interval, "
                        f"not a percentage. The minimum here is {minimum}.", [note])] if n < minimum else []
    return RuleResult(findings, note)


def separating_tests(trials) -> tuple[list[str], int]:
    """Tests that failed in some trials and passed in others, and the total test count."""
    reports = [(_report(t).get("ctrf") or {}) for t in trials]
    reports = [r for r in reports if r.get("tests")]
    if len(reports) < 2:
        return [], max((r.get("tests") or 0 for r in reports), default=0)
    failed_in = {}
    for r in reports:
        for name in r.get("failed_names") or []:
            failed_in[name] = failed_in.get(name, 0) + 1
    return sorted(n for n, c in failed_in.items() if c < len(reports)), max(r["tests"] for r in reports)


def rule_separating(runs, ctx=None) -> RuleResult:
    """15.12 (H7, P6): very few tests separate the trials."""
    trials = _finished(_of(runs, "agent"))
    sep, total = separating_tests(trials)
    if not total or len(trials) < 2:
        return RuleResult([], "fewer than two trials with test results")
    if not any((_report(t).get("ctrf") or {}).get("failed_names") for t in trials):
        return RuleResult([], f"no trial failed a test ({len(trials)} trials, {total} tests), so nothing separates them")
    note = f"{len(sep)} of {total} tests separate the trials"
    findings = []
    if len(sep) <= max(3, total // 5):
        findings.append(Finding("15.12", f"Only {len(sep)} of {total} tests ever separate the trials, so a "
                                "trial's test count is no measure of how close it came.", [note] + sep[:10]))
    return RuleResult(findings, note)


def rule_nobody_solved(runs, ctx=None) -> RuleResult:
    """15.13 (H2): every agent trial scored 0. Hard is not a defect; a person confirms it."""
    trials = _finished(_of(runs, "agent"))
    if not trials or any(t.reward >= 1 for t in trials):
        return RuleResult([], f"{len(trials)} finished trial(s)")
    return RuleResult([Finding("15.13", "No trial solved the task. Confirm it is hard but fair (accept with a "
                               "reason); the platform never pushes a task to be easier.",
                               [pass_rate_text(0, len(trials))])], f"0 of {len(trials)} solved")


def rule_documentation_gap(runs, ctx=None) -> RuleResult:
    """15.28 (I6): failure analysis calls a miss a documentation gap while other trials passed."""
    fa = next(iter(_of(runs, "failure_analysis")), None)
    data = _json(fa.logs) if fa else None
    trials = _finished(_of(runs, "agent"))
    if not data or not any(t.reward >= 1 for t in trials):
        return RuleResult([], "no failure analysis, or no trial passed")
    text = json.dumps(data.get("audit") or data.get("classification") or {})
    gaps = re.findall(r"documentation[ _]gap|instruction[ _]gap", text, re.I)
    findings = [Finding("15.28", "Failure analysis calls a miss a documentation gap although other trials "
                        "passed the same task. Do not act on the analysis alone.",
                        [f"{len(gaps)} documentation-gap verdict(s); {sum(t.reward >= 1 for t in trials)} trial(s) passed"])] if gaps else []
    return RuleResult(findings, f"{len(gaps)} documentation-gap verdict(s)")


def rule_no_arm64(runs, ctx=None) -> RuleResult:
    """15.20 (I9): no run on arm64 exists."""
    harbor = [r for r in _of(runs, *HARBOR_KINDS) if _report(r).get("facts")]
    if not harbor:
        return RuleResult([], "no Harbor run with recorded facts")
    archs = {str(_report(r)["facts"].get("platform") or _report(r)["facts"].get("host_arch") or "") for r in harbor}
    if any("arm64" in a or "aarch64" in a for a in archs):
        return RuleResult([], f"platforms seen: {', '.join(sorted(archs))}")
    return RuleResult([Finding("15.20", "Every run used amd64; the task was never run on arm64.",
                               [f"platforms seen: {', '.join(sorted(a for a in archs if a)) or 'unknown'}"])],
                      f"platforms seen: {', '.join(sorted(archs))}")


def rule_timeout_headroom(runs, ctx=None) -> RuleResult:
    """15.27 (I8): the agent timeout is far above the slowest finished trial."""
    trials = [t for t in _finished(_of(runs, "agent")) if (_report(t).get("facts") or {}).get("agent_seconds")]
    if not trials:
        return RuleResult([], "no finished trial with a recorded agent time")
    slowest = max(_report(t)["facts"]["agent_seconds"] for t in trials)
    limit = max((_report(t)["facts"].get("agent_limit_sec") or 0) for t in trials)
    note = f"slowest trial {slowest:.0f} s, timeout {limit:.0f} s"
    findings = []
    if limit and slowest and limit / slowest > get_settings().timeout_headroom_ratio:
        findings.append(Finding("15.27", f"The agent timeout is {limit / slowest:.1f}x the slowest finished trial: "
                                "cost and runaway risk. Changing it is an environment change (Major) and voids "
                                "saved rollouts, so decide before the next rerun.", [note]))
    return RuleResult(findings, note)


def _span(run):
    return (run.started_at, run.finished_at) if run.started_at and run.finished_at else None


def rule_timeout_under_load(runs, ctx=None, all_runs=None) -> RuleResult:
    """15.31 (E13): a trial timed out while another Harbor job was running."""
    timed_out = [t for t in _of(runs, "agent", "oracle") if t.reward is None and re.search(
        r"TimeoutError|timed out|AgentTimeout", t.logs or "")]
    if not timed_out:
        return RuleResult([], "no timed-out trial")
    others = [r for r in (all_runs or runs) if r.kind in HARBOR_KINDS and _span(r)]
    evidence = []
    for t in timed_out:
        s = _span(t)
        if not s:
            continue
        overlap = [o for o in others if o is not t and o.started_at < s[1] and o.finished_at > s[0]]
        if overlap:
            evidence.append(f"{t.kind} {t.run_index} timed out while {len(overlap)} other Harbor run(s) were running")
    findings = [Finding("15.31", "A timeout under load says little about the task; rerun it alone.", evidence)] if evidence else []
    return RuleResult(findings, f"{len(timed_out)} timed-out trial(s)")


def rule_cheap_model(runs, ctx=None) -> RuleResult:
    """15.36 (E4): a cheap small model passed, the loudest shortcut alarm."""
    cheap = get_settings().cheap_agent_model
    trials = [t for t in _finished(_of(runs, "agent")) if cheap and _model(t) == cheap]
    if not trials:
        return RuleResult([], f"no finished trial on the cheap model ({cheap or 'not set'})")
    passed = [t for t in trials if t.reward >= 1]
    findings = [Finding("15.36", f"The cheap model {cheap} solved the task: check for a shortcut before "
                        "trusting the difficulty.", [pass_rate_text(len(passed), len(trials))])] if passed else []
    return RuleResult(findings, f"cheap model: {pass_rate_text(len(passed), len(trials))}")


# ---- CK-I: cheat and refusal coverage (batch 7) ------------------------------------------

def rule_cheat_coverage(runs, ctx=None) -> RuleResult:
    """15.19 (S1, I10): no usable cheat evidence. Refused or inconclusive trials are not 'held'."""
    trials = _of(runs, "cheat_trial")
    if not trials:
        return RuleResult([Finding("15.19", "No cheat trial has run, so nothing shows the verifier resists "
                                   "a deliberate attempt.", ["no cheat trial"])], "no cheat trial")
    usable, refused, tiers = 0, 0, set()
    for t in trials:
        data = _json(t.logs) or {}
        tiers.add(str(data.get("tier") or "2"))
        if data.get("inconclusive") or data.get("refused"):
            refused += 1
        elif t.status in ("passed", "failed"):
            usable += 1
    note = f"{len(trials)} cheat trial(s): {usable} usable, {refused} refused or inconclusive; tiers {sorted(tiers)}"
    if usable == 0:
        return RuleResult([Finding("15.19", "Every cheat trial was refused or inconclusive. That is not evidence the "
                                   "trap held; run a tier with a provider that does not refuse.", [note])], note)
    return RuleResult([], note)


RESULT_RULES = {
    "15.1": rule_fail_open,
    "15.3": rule_hostile,
    "15.6": rule_duplicate_names,
    "15.8": rule_case_count,
    "15.9": rule_not_run_counted,
    "15.10": rule_logs_parse,
    "15.11": rule_interval,
    "15.12": rule_separating,
    "15.13": rule_nobody_solved,
    "15.19": rule_cheat_coverage,
    "15.20": rule_no_arm64,
    "15.27": rule_timeout_headroom,
    "15.28": rule_documentation_gap,
    "15.31": rule_timeout_under_load,
    "15.33": rule_positive_control,
    "15.36": rule_cheap_model,
    "15.37": rule_null_ladder,
}


def evaluate(runs, all_runs=None) -> dict[str, RuleResult]:
    out = {}
    for rule_id, fn in RESULT_RULES.items():
        try:
            out[rule_id] = fn(runs, all_runs=all_runs) if rule_id == "15.31" else fn(runs)
        except Exception as exc:  # noqa: BLE001 - one broken rule must not hide the others
            out[rule_id] = RuleResult([], f"rule error: {type(exc).__name__}: {exc}")
    return out

