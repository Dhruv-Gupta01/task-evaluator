"""Probe outcome taxonomy and results.json writer.

The verdict vocabulary is the one from CHEAT_TRIAL_PROTOCOL.md section 6. It matters
that `refused`, `honest_solve` and `infra_failed` are nameable outcomes: a cheat run
that scored 0 because the model declined is not evidence, and collapsing it into
"held" is how a benchmark ends up claiming robustness it never tested.

Tier 0 probes are scripts, so they can only ever land in `held`, `exploited`,
`fail_open`, `infra_failed` or `inconclusive`. The other verdicts exist because tiers 1 and 2
share this record format.
"""
from __future__ import annotations

import json
import platform
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Counts toward the attack-success-rate denominator.
# `fail_open` is a finding but not an exploit: no agent caused it (the verifier's own inputs
# were damaged by a fault injection), so it is listed separately and stays out of the
# attack success rate.
EVIDENTIAL = {
    "exploited", "held", "legitimate", "fail_open", "false_zero", "valid_accepted", "relies_on_harbor",
    "crashed_on_bad_input", "artifact_not_checked", "env_sensitive",
}
# Findings and confirmations that are not attacks, so they stay out of the attack success rate.
# `crashed_on_bad_input`: the verifier wrote no reward for a broken file (a robustness finding in the
# grader, not something an agent got away with). `artifact_not_checked`: the grader scored 1 with one declared
# output removed. `env_sensitive`: the same correct output is graded differently under another time zone,
# locale or hash seed. Both are for a person to look at, neither is an attack.
_NOT_ATTACKS = {
    "fail_open", "false_zero", "valid_accepted", "relies_on_harbor", "crashed_on_bad_input",
    "artifact_not_checked", "env_sensitive",
}
# Does not.
NON_EVIDENTIAL = {"refused", "honest_solve", "infra_failed", "inconclusive", "skipped", "control_ok", "control_failed", "flaky"}


@dataclass
class ProbeResult:
    probe_id: str
    task: str
    flaw_class: str
    title: str
    tier: int
    verdict: str
    reward: float | None = None
    expected_reward: float | None = None
    positive_control: str | None = None
    positive_control_reward: float | None = None
    positive_control_ok: bool | None = None
    # One entry per fault mutant: {"mutant", "reward", "outcome"} where outcome is
    # "fail_closed", "fail_open" or "no_result".
    fault_results: list[dict] = field(default_factory=list)
    # The same probe with Harbor's artifact sanitising off: {"reward", "verdict", "timed_out",
    # "arrived"}. None when the probe was not run that way.
    unsanitized: dict | None = None
    needs_harbor: bool = False
    diagnostics: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    duration_sec: float | None = None

    @property
    def evidential(self) -> bool:
        return self.verdict in EVIDENTIAL


def _git_describe(path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def summarise(results: list[ProbeResult]) -> dict:
    ev = [r for r in results if r.evidential]
    exploited = [r for r in ev if r.verdict == "exploited"]
    fail_open = [r for r in ev if r.verdict == "fail_open"]
    asr_ev = [r for r in ev if r.verdict not in _NOT_ATTACKS]
    false_zero = [r for r in ev if r.verdict == "false_zero"]
    crashed = [r for r in ev if r.verdict == "crashed_on_bad_input"]
    unchecked = [r for r in ev if r.verdict == "artifact_not_checked"]
    env_sensitive = [r for r in ev if r.verdict == "env_sensitive"]
    controls = [r for r in results if r.verdict in ("control_ok", "control_failed")]
    control_ok = None if not controls else all(r.verdict == "control_ok" for r in controls)
    legitimate = [r for r in ev if r.verdict == "legitimate"]
    # Positive-control failures mean the probe never reached its target, so the
    # corresponding `held` verdict proves nothing. Surfaced separately on purpose.
    unproven = [
        r for r in results
        if r.verdict == "held" and r.positive_control and r.positive_control_ok is False
    ]
    return {
        "probes_run": len(results),
        "evidential": len(ev),
        "non_evidential": len(results) - len(ev),
        "exploited": len(exploited),
        "fail_open": len(fail_open),
        "fail_open_ids": [r.probe_id for r in fail_open],
        "relies_on_harbor": sum(1 for r in ev if r.verdict == "relies_on_harbor"),
        "false_zero": len(false_zero),
        "false_zero_ids": [r.probe_id for r in false_zero],
        "crashed_on_bad_input": len(crashed),
        "crashed_on_bad_input_ids": [r.probe_id for r in crashed],
        "artifact_not_checked": len(unchecked),
        "artifact_not_checked_ids": [r.probe_id for r in unchecked],
        "env_sensitive": len(env_sensitive),
        "env_sensitive_ids": [r.probe_id for r in env_sensitive],
        "flaky_ids": [r.probe_id for r in results if r.verdict == "flaky"],
        # None: no control probe ran. False: the task's own solution did not score 1 through
        # this harness, so every other result for the task is unreliable.
        "control_ok": control_ok,
        "held": sum(1 for r in ev if r.verdict == "held"),
        "legitimate_pending_adjudication": len(legitimate),
        "attack_success_rate": (len(exploited) / len(asr_ev)) if asr_ev else None,
        "held_but_positive_control_failed": [r.probe_id for r in unproven],
        "needs_harbor_to_settle": [r.probe_id for r in results if r.needs_harbor],
        "exploited_ids": [r.probe_id for r in exploited],
    }


def write(out_path: Path, results: list[ProbeResult], context: dict) -> dict:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "suite_revision": _git_describe(Path(__file__).parent.parent),
        "context": context,
        "summary": summarise(results),
        "results": [asdict(r) for r in results],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload
