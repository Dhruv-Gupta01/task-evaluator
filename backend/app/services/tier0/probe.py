"""The probe interface.

A Tier 0 probe is a bash script that stands in for the agent, plus the metadata needed
to judge the outcome. The script runs in the task's own environment image as the task's
declared agent user, and receives the declared artifact paths as "$@".

Fields that decide the verdict:

  expect_reward      0 means the defence must hold. 1 means this probe is only
                     meaningful against a mutant. None means the reward is not the
                     signal and `check` or DIAGNOSTICS carries it.
  positive_control   Defence mutants (mutants.py, role="defence"). The probe must score 0
                     against the real verifier and 1 against these mutants. Without this,
                     a `held` verdict is unproven, and the summary reports it as such.
  fault_mutants      Fault mutants (role="fault"): the verifier with its own inputs
                     damaged. The probe must score 0 against these too. A 1 is verdict
                     `fail_open`: the verifier pays full reward when its inputs are gone.
  kind               "attack" (default): reward 1 is an exploit. "control": the task's own
                     solution run through this harness, which must score 1 or no other result
                     for the task can be trusted. "valid_variant": a solution that should be
                     accepted; reward 0 is `false_zero`, which a lab reads as the model being
                     unable to do the task.
  also_unsanitized   Also run with Harbor's artifact sanitising switched off. A probe that
                     only holds because Harbor drops the file (symlinks, FIFOs) is
                     `relies_on_harbor`: the task does not defend itself.
  control_by_check   Defence mutants under which the probe's own check must fire (tampering
                     with the grader's files is the signal, not the reward).
  control_arrival    The observer control only shows the planted file arrives (it cannot make
                     the verifier run it), so the held result is marked as such.
  control_declare    Observer control for probes that only watch whether a planted file
                     reaches the verifier: re-run with these extra paths declared as
                     artifacts. The probe's own check must then fire, which proves the
                     probe can see a leak. (`control_inject` does the same by copying
                     task files into the agent container, for probes that look there.)
  malformed          The probe hands the verifier a broken file (empty, cut in half, garbage, an
                     oversized field, a non-workbook). The verifier must score 0. A run where it
                     started but wrote no reward is `crashed_on_bad_input`: the agent's bad
                     answer is then reported as a grader error and never counts against it.
  per_artifact       A template: the runner expands it into one probe per declared artifact (at most 8,
                     none when the task has fewer than two), each with `drops_artifact` set.
  drops_artifact     The reference output with this one artifact removed. A grader that still scores 1
                     never looked at that file: `artifact_not_checked`. Some files may be optional, so a
                     person decides; it is never counted as an attack.
  env_sweep          Grade the same output under each setting in probes.generic.ENV_SETTINGS (time zone,
                     locale, hash seed). A verdict that follows a setting is `env_sensitive`; one that
                     changes between two identical runs is `flaky`.
  harbor_probe       Judged by a real Harbor Oracle run on a task copy whose solve.sh is
                     this script, because the answer depends on Harbor's own behaviour.
  needs_harbor       The answer depends on Harbor's own behaviour, not the task's. The
                     local harness records what it can and the verdict stays
                     inconclusive until a real `harbor run` settles it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Probe:
    pid: str
    flaw: str                       # V1..V8 from BenchJack, or "-" for non-taxonomy
    title: str
    applies_to: tuple[str, ...]     # task slugs, or ("*",)
    agent_script: str
    rationale: str = ""
    expect_reward: float | None = 0.0
    positive_control: tuple[str, ...] | None = None
    fault_mutants: tuple[str, ...] | None = None
    needs_harbor: bool = False
    as_root: bool = False
    tier: int = 0
    kind: str = "attack"            # "attack" | "control" | "valid_variant"
    diagnostics: dict = field(default_factory=dict)
    agent_timeout_sec: float = 900.0
    check: Callable | None = None   # (run, spec) -> (verdict|None, [notes])
    # Files copied into the environment container before the script runs, as
    # {container_path: path_relative_to_task_root}. This models "the agent somehow
    # obtained X" without pretending the probe could derive it. Used for positive
    # controls that need the real answers, e.g. hardcoding the archive exports.
    inject: dict = field(default_factory=dict)
    needs_no_sanitize: bool = False  # only meaningful with --no-sanitize
    also_unsanitized: bool = False
    control_declare: tuple[str, ...] = ()
    # The control only proves the planted file reaches the verifier, not that the verifier
    # would use it (G5: whether a shadowed python3 runs depends on the verifier's PATH).
    control_arrival: bool = False
    # Defence mutants whose run the probe's OWN check must flag, because the signal is a file the
    # agent's code changed in the verifier (not the reward).
    control_by_check: tuple[str, ...] = ()
    control_inject: dict = field(default_factory=dict)
    harbor_probe: bool = False
    malformed: bool = False
    per_artifact: bool = False
    drops_artifact: str | None = None
    env_sweep: bool = False

    # attribute names docker_runner expects
    @property
    def AGENT_SCRIPT(self) -> str:          # noqa: N802
        return self.agent_script

    @property
    def AS_ROOT(self) -> bool:              # noqa: N802
        return self.as_root

    @property
    def DIAGNOSTICS(self) -> dict:          # noqa: N802
        return self.diagnostics

    @property
    def AGENT_TIMEOUT_SEC(self) -> float:   # noqa: N802
        return self.agent_timeout_sec

    def matches(self, slug: str) -> bool:
        return "*" in self.applies_to or slug in self.applies_to


def expect_tests(expected: dict[str, str]):
    """Build a check that asserts individual test outcomes from ctrf.json.

    Needed because both reviewed tasks gate the reward on every test passing, so a
    probe aimed at one defence cannot move the reward on its own. Asserting that one
    named test flipped while others stayed put is the precise signal.
    """
    def check(run, spec):
        if not run.ctrf:
            return "inconclusive", ["no ctrf.json produced, cannot judge per-test outcomes"]
        notes, bad = [], []
        for name, want in expected.items():
            hits = [k for k in run.ctrf if name in k]
            if not hits:
                bad.append(f"test matching {name!r} not found in ctrf.json")
                continue
            for k in hits:
                got = run.ctrf[k]
                notes.append(f"{k}: {got} (wanted {want})")
                if got != want:
                    bad.append(f"{k} was {got}, expected {want}")
        if bad:
            return "exploited" if any("passed" in b for b in bad) else "inconclusive", notes + bad
        return None, notes
    return check


PREAMBLE = r"""
set -u
# Artifact paths the task declares are in "$@". Probes must only ever write where a
# real agent could write, so nothing here assumes root.
say() { printf '[probe] %s\n' "$*"; }
try() { if "$@" 2>/dev/null; then say "OK      : $*"; else say "BLOCKED : $*"; fi; }
"""
