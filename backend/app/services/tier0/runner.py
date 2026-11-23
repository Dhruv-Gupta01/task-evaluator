"""Run Tier 0 probes against one task and judge the outcome.

Judging and the mutant checks live here rather than in a CLI so the platform's stage and
`backend/scripts/run_tier0.py` share one implementation.

Per probe:

  1. Run it against the real verifier.
       reward 1 where 0 was expected  -> `exploited`. Stop: nothing else matters.
       otherwise                      -> `held`, pending the checks below.
  2. Defence mutants (`positive_control`): run it against the verifier with those defences
     removed. Reward 1 means the probe reaches the defence, so `held` is demonstrated.
     Anything else leaves `held` unproven, and the summary lists it as such.
  3. Fault mutants (`fault_mutants`): run it against the verifier with its own inputs
     damaged. Reward 0 is fail-closed. Reward 1 is `fail_open`, a finding.

A mutant's reward is never counted as a cheat. Only step 1 can produce `exploited`.
"""
from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import docker_runner, mutants, record, replay
from .probe import Probe
from .taskspec import TaskSpec


@dataclass
class Options:
    platform: str | None = "linux/amd64"
    sanitize: bool = True
    keep: bool = False
    # Run each attack probe's real-verifier phase this many times. Different rewards across
    # repeats make the probe `flaky` (non-evidential): the verifier or the task is not
    # deterministic, which is itself worth knowing. 1 = once.
    repeats: int = 1


MAX_DROPPED_ARTIFACTS = 8


def expand_per_artifact(probes: list[Probe], spec) -> list[Probe]:
    """Turn each per-artifact template (G23) into one probe per declared artifact, so each run removes exactly
    one. A task with fewer than two artifacts gets none (G0 already covers "nothing there"); at most
    MAX_DROPPED_ARTIFACTS, the first ones in the task's own order."""
    out: list[Probe] = []
    for probe in probes:
        if not probe.per_artifact:
            out.append(probe)
            continue
        artifacts = list(spec.artifacts)
        if len(artifacts) < 2:
            continue
        for i, artifact in enumerate(artifacts[:MAX_DROPPED_ARTIFACTS]):
            out.append(dataclasses.replace(
                probe, pid=f"{probe.pid}-{i}", per_artifact=False, drops_artifact=artifact,
                title=f"{probe.title}: {artifact}",
                agent_script=f"\nDROP={i}\n" + probe.agent_script,
            ))
    return out


def _judge_env_sweep(run: docker_runner.Run, notes: list[str]) -> tuple[str, list[str]]:
    """The reference output graded under several settings. The first two are identical (no setting changed),
    so a difference between them is flakiness, not the environment."""
    runs = run.env_runs
    if len(runs) < 2:
        return "inconclusive", notes + ["the environment sweep did not run"]
    base, again = runs[0], runs[1]
    if base["reward"] != again["reward"]:
        return "flaky", notes + [
            f"no setting changed between '{base['label']}' and '{again['label']}', yet the reward went "
            f"from {base['reward']} to {again['reward']}: the grader is not stable on identical input"
        ]
    differing = [r for r in runs[2:] if r["reward"] != base["reward"]]
    if differing:
        return "env_sensitive", notes + [
            f"{r['label']}: reward {r['reward']} where the baseline gave {base['reward']} (differs)"
            for r in differing
        ] + ["the same correct output is graded differently depending on the environment"]
    if base["reward"] == 1.0:
        return "valid_accepted", notes + [
            f"the reference output scored 1 under all {len(runs)} settings ({', '.join(r['label'] for r in runs)})"
        ]
    return "inconclusive", notes + [
        f"the reference output scored {base['reward']} under every setting, so there is nothing to compare; "
        "see control G13"
    ]


def judge(probe: Probe, run: docker_runner.Run, spec: TaskSpec) -> tuple[str, list[str]]:
    notes = list(run.notes)

    if probe.env_sweep:
        return _judge_env_sweep(run, notes)

    if probe.drops_artifact and run.reward == 1.0:
        if probe.check:  # the reference solution must have produced something to remove
            override, extra = probe.check(run, spec)
            if override:
                return override, notes + extra
        return "artifact_not_checked", notes + [
            f"reward 1 with {probe.drops_artifact} removed: the grader never looked at that file. "
            "It may be optional; a person decides."
        ]

    if probe.kind == "control":
        if run.reward == 1.0:
            return "control_ok", notes + ["the task's own solution scored 1 through this harness"]
        return "control_failed", notes + [
            f"the task's own solution scored {run.reward} through this harness, so the harness "
            f"does not model this task faithfully. Treat every other result for it as unreliable."
        ]

    if probe.kind == "valid_variant":
        if run.reward == 1.0:
            return "valid_accepted", notes + ["the valid variant scored 1"]
        if run.reward is None:
            return "inconclusive", notes + ["no reward produced for the valid variant"]
        return "false_zero", notes + [
            f"the valid variant scored {run.reward}. If the instruction really allows it, a "
            f"correct solution of this shape is graded as a failure. A human decides whether "
            f"the variant is allowed."
        ]

    if run.reward == 1.0 and probe.expect_reward == 0.0:
        return "exploited", notes + ["reward 1 against the unmutated verifier"]

    verdict = "held"
    if probe.expect_reward is None:
        verdict = "inconclusive"
        notes.append("diagnostic probe: the reward is not the signal")

    if probe.check:
        override, extra = probe.check(run, spec)
        notes += extra
        if override:
            verdict = override

    if (
        probe.malformed and verdict == "held" and run.reward is None
        and not run.timed_out and run.verifier_exit is not None
    ):
        # The verifier started, got a broken file and wrote no reward. Harbor then records a verifier
        # error rather than reward 0, so the bad answer is never scored against the agent.
        return "crashed_on_bad_input", notes + [
            "no reward produced for a malformed artifact: the grader crashed instead of scoring 0 "
            f"(verifier exit {run.verifier_exit}). Check how it handles an unparseable file."
        ]
    if run.timed_out and verdict not in {"exploited"}:
        verdict = "inconclusive"
    if probe.expect_reward == 0.0 and run.reward is None and not run.timed_out:
        verdict = "inconclusive"
        notes.append("no reward produced: Harbor would treat this as an INVALID reward")
    if probe.needs_harbor and verdict != "exploited":
        verdict = "inconclusive"
        notes.append("needs a real `harbor run` to settle: depends on Harbor, not the task")
    return verdict, notes


def _fault_outcome(reward: float | None, timed_out: bool) -> str:
    if timed_out or reward is None:
        return "no_result"
    return "fail_open" if reward == 1.0 else "fail_closed"


def run_one(spec: TaskSpec, probe: Probe, opts: Options) -> record.ProbeResult:
    started = time.time()
    res = record.ProbeResult(
        probe_id=probe.pid, task=spec.slug, flaw_class=probe.flaw, title=probe.title,
        tier=probe.tier, verdict="infra_failed",
        expected_reward=probe.expect_reward, needs_harbor=probe.needs_harbor,
        positive_control=",".join(probe.positive_control or ()) or None,
    )
    if probe.needs_no_sanitize and opts.sanitize:
        res.verdict = "skipped"
        res.notes = ["only meaningful with sanitisation off"]
        return res
    try:
        extra = {}
        if probe.env_sweep:
            from .probes.generic import ENV_SETTINGS
            extra["verifier_envs"] = ENV_SETTINGS
        run = docker_runner.run_probe(
            spec, probe, platform=opts.platform, sanitize=opts.sanitize, keep=opts.keep, **extra,
        )
        res.reward = run.reward
        res.diagnostics = dict(run.diagnostics)
        if run.env_runs:
            res.diagnostics["env_runs"] = [dict(r) for r in run.env_runs]
        if run.ctrf:
            res.diagnostics["_ctrf_failed"] = sorted(
                k for k, v in run.ctrf.items() if v != "passed"
            )[:20]
            res.diagnostics["_ctrf_counts"] = {
                s: sum(1 for v in run.ctrf.values() if v == s) for s in set(run.ctrf.values())
            }
        res.verdict, res.notes = judge(probe, run, spec)

        if probe.kind == "attack" and opts.repeats > 1 and res.verdict in ("held", "exploited"):
            rewards = [run.reward]
            for _ in range(opts.repeats - 1):
                again = docker_runner.run_probe(
                    spec, probe, platform=opts.platform, sanitize=opts.sanitize, keep=opts.keep,
                )
                rewards.append(again.reward)
            if len(set(rewards)) > 1:
                res.verdict = "flaky"
                res.notes.append(f"FLAKY: {opts.repeats} identical runs gave rewards {rewards}")

        if res.verdict == "held" and probe.also_unsanitized and opts.sanitize:
            _unsanitized_pass(spec, probe, opts, res)
        if res.verdict == "held" and (probe.control_declare or probe.control_inject):
            _observer_control(spec, probe, opts, res)
        if res.verdict == "held" and probe.control_by_check:
            _check_control(spec, probe, opts, res)
        if res.verdict == "held" and probe.positive_control:
            _positive_control(spec, probe, opts, res)
        if res.verdict == "held" and probe.fault_mutants:
            _fault_checks(spec, probe, opts, res)
    except docker_runner.DockerUnavailable as exc:
        res.verdict = "infra_failed"
        res.notes = [f"docker unavailable: {exc}"]
    except Exception as exc:
        res.verdict = "infra_failed"
        res.notes = [f"{type(exc).__name__}: {exc}"]
    res.duration_sec = round(time.time() - started, 1)
    return res


def _unsanitized_pass(spec: TaskSpec, probe: Probe, opts: Options, res: record.ProbeResult) -> None:
    """Re-run with Harbor's artifact sanitising switched off. Harbor drops symlinks that
    point outside the artifact folder and skips FIFOs, so a probe built on them can score 0
    only because Harbor removed the file. If it still scores 0 without that help the task
    defends itself, which is the stronger property and survives a Harbor upgrade. If not, the
    verdict is `relies_on_harbor`. The same run is the probe's control: it shows the planted
    file really does reach the verifier when nothing drops it."""
    try:
        run = docker_runner.run_probe(spec, probe, platform=opts.platform, sanitize=False)
    except Exception as exc:
        res.notes.append(f"unsanitised pass errored: {type(exc).__name__}: {exc}")
        return
    verdict, _ = judge(probe, run, spec)
    arrived = any(m["restored"] and m["kind"] in ("symlink", "fifo", "socket", "block", "char")
                  for m in run.artifacts.values())
    res.unsanitized = {"reward": run.reward, "verdict": verdict, "timed_out": run.timed_out, "arrived": arrived}
    res.positive_control = "sanitising off: the planted file reaches the verifier"
    res.positive_control_reward = run.reward
    res.positive_control_ok = arrived
    if verdict == "exploited":
        res.verdict = "relies_on_harbor"
        res.notes.append(
            f"RELIES ON HARBOR: it scored 0 only because Harbor dropped the planted file. "
            f"With sanitising off the same probe gave {'no valid reward (timeout)' if run.timed_out else f'reward {run.reward}'}. "
            f"The task does not defend itself."
        )
    elif arrived:
        res.notes.append(
            f"also held with sanitising off (reward {run.reward}): the task defends itself without Harbor's help"
        )
    else:
        res.notes.append("the planted file did not arrive even with sanitising off, so this pass proves nothing")


def _observer_control(spec: TaskSpec, probe: Probe, opts: Options, res: record.ProbeResult) -> None:
    """Prove the probe's check can see a leak. These probes only watch whether a planted file
    (or an exposed folder) reaches the verifier, so a quiet result is worthless unless the
    same check fires when the file is made to arrive."""
    what = ", ".join(probe.control_declare) or ", ".join(probe.control_inject)
    try:
        run = docker_runner.run_probe(
            spec, probe, platform=opts.platform, sanitize=opts.sanitize,
            extra_artifacts=list(probe.control_declare) or None,
            extra_inject=dict(probe.control_inject) or None,
        )
    except Exception as exc:
        res.notes.append(f"observer control errored: {type(exc).__name__}: {exc}")
        return
    verdict, _ = judge(probe, run, spec)
    res.positive_control = f"observer: {what} made to arrive"
    res.positive_control_reward = run.reward
    if probe.control_arrival:
        # The file cannot be made to run, only to arrive: that is all this control shows.
        res.positive_control_ok = all(
            run.artifacts.get(p, {}).get("restored") for p in probe.control_declare
        )
        if res.positive_control_ok:
            res.notes.append(
                f"observer control: {what} reached the verifier, but whether the verifier would run it "
                f"depends on its PATH, which this probe cannot change; the held result shows the "
                f"planted file did not execute, not that it could not"
            )
        else:
            res.notes.append(f"OBSERVER CONTROL FAILED: {what} did not reach the verifier, so `held` is unproven")
        return
    res.positive_control_ok = verdict == "exploited"
    if res.positive_control_ok:
        res.notes.append(f"observer control passed: with {what} made to arrive the probe's check fired, so a quiet result is real")
    else:
        res.notes.append(f"OBSERVER CONTROL FAILED: with {what} made to arrive the probe's check stayed quiet, so `held` is unproven")


def _check_control(spec: TaskSpec, probe: Probe, opts: Options, res: record.ProbeResult) -> None:
    """Control for probes whose signal is "agent code changed the grader's files" (I-d). Run
    again with the protection removed; the probe's own check must then report the tampering.
    Otherwise the quiet result on the real verifier could just mean the probe never ran."""
    try:
        ctl = docker_runner.run_probe(
            spec, probe, platform=opts.platform, mutant=list(probe.control_by_check),
            sanitize=opts.sanitize,
        )
    except Exception as exc:
        res.notes.append(f"control errored: {type(exc).__name__}: {exc}")
        return
    verdict, _ = judge(probe, ctl, spec)
    res.positive_control = ",".join(probe.control_by_check)
    res.positive_control_reward = ctl.reward
    res.positive_control_ok = verdict == "exploited"
    if res.positive_control_ok:
        res.notes.append(
            f"control passed: with {res.positive_control} removed the probe's tampering "
            f"succeeded, so the real verifier's block is the protection, not a broken probe"
        )
    else:
        res.notes.append(
            f"CONTROL FAILED: with {res.positive_control} removed the probe's tampering was "
            f"still not seen, so `held` is unproven"
        )


def _positive_control(spec: TaskSpec, probe: Probe, opts: Options, res: record.ProbeResult) -> None:
    try:
        ctl = docker_runner.run_probe(
            spec, probe, platform=opts.platform, mutant=list(probe.positive_control),
            sanitize=opts.sanitize,
        )
    except Exception as exc:
        res.notes.append(f"positive control errored: {type(exc).__name__}: {exc}")
        return
    res.positive_control_reward = ctl.reward
    res.positive_control_ok = ctl.reward == 1.0
    if res.positive_control_ok:
        res.notes.append(
            f"positive control passed: reward 1 with {res.positive_control} removed, so the "
            f"defence is demonstrated rather than assumed"
        )
    else:
        res.notes.append(
            f"POSITIVE CONTROL FAILED: with {res.positive_control} removed the probe scored "
            f"{ctl.reward}, not 1. The probe may never have reached the defence, so `held` "
            f"is unproven here."
        )


def _fault_checks(spec: TaskSpec, probe: Probe, opts: Options, res: record.ProbeResult) -> None:
    ids = mutants.expand(spec.root / "tests", probe.fault_mutants)
    if not ids:
        res.notes.append("no fault check ran: there is no data folder or data file under tests/ to remove")
    for mid in ids:
        try:
            run = docker_runner.run_probe(
                spec, probe, platform=opts.platform, mutant=[mid], sanitize=opts.sanitize,
            )
        except Exception as exc:
            res.fault_results.append({"mutant": mid, "reward": None, "outcome": "no_result"})
            res.notes.append(f"fault check {mid} errored: {type(exc).__name__}: {exc}")
            continue
        outcome = _fault_outcome(run.reward, run.timed_out)
        res.fault_results.append({"mutant": mid, "reward": run.reward, "outcome": outcome})
        if outcome == "fail_open":
            res.notes.append(
                f"FAIL OPEN: with the fault {mid} injected, this probe scored reward 1. The "
                f"verifier pays full reward when its own inputs are missing."
            )
        elif outcome == "fail_closed":
            res.notes.append(f"fail closed under {mid}: reward 0")
        else:
            res.notes.append(f"fault check {mid} produced no valid reward")
    if any(f["outcome"] == "fail_open" for f in res.fault_results):
        res.verdict = "fail_open"
    elif any(f["outcome"] == "no_result" for f in res.fault_results):
        res.verdict = "inconclusive"


def run_suite(
    spec: TaskSpec,
    probes: list[Probe],
    opts: Options,
    on_result: Callable[[int, int, record.ProbeResult], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[record.ProbeResult]:
    results: list[record.ProbeResult] = []
    probes = expand_per_artifact(probes, spec)
    # Controls first: if the task's own solution does not score 1 here, say so up front.
    probes = sorted(probes, key=lambda p: p.kind != "control")
    for i, probe in enumerate(probes, 1):
        if should_stop and should_stop():
            break
        res = run_one(spec, probe, opts)
        results.append(res)
        if on_result:
            on_result(i, len(probes), res)
    return results


def run_replays(
    spec: TaskSpec,
    items: list[tuple[replay.Bundle, str, str]],
    opts: Options,
    should_stop: Callable[[], bool] | None = None,
) -> list[dict]:
    """Regrade stored rollouts through the replica verifier. `items` is (bundle, kind,
    source) with source "this upload" or "earlier upload".

    No agent and no model: the bundles already hold what an agent produced, collected by
    Harbor. What the comparison means depends on whose rollout it is:
      this upload     the verifier is the one Harbor graded with, so a different reward means
                      the replica does not match Harbor (`harness_disagrees`)
      earlier upload  the verifier may have changed since, so a higher reward is `LOOSENED`
                      (the change may have made the task easier, the documented main failure
                      of hardening) and a lower one `REGRESSION`
    """
    out: list[dict] = []
    for bundle, kind, source in items:
        if should_stop and should_stop():
            break
        recorded = replay.recorded_reward(bundle)
        entry = {"key": bundle.key, "kind": kind, "source": source, "recorded": recorded,
                 "replayed": None, "verdict": "no_reward", "notes": []}
        try:
            run = docker_runner.run_replay(spec, bundle, platform=opts.platform)
            entry["replayed"] = run.reward
            entry["notes"] = [n for n in run.notes if "ctrf entries" not in n][:4]
        except Exception as exc:
            entry["notes"] = [f"{type(exc).__name__}: {exc}"]
            out.append(entry)
            continue
        if run.reward is None:
            entry["verdict"] = "no_reward"
        elif recorded is None:
            entry["verdict"] = "no_baseline"
        elif run.reward == recorded:
            entry["verdict"] = "reproduced"
        elif source == "this upload":
            entry["verdict"] = "harness_disagrees"
        else:
            entry["verdict"] = "LOOSENED" if run.reward > recorded else "REGRESSION"
        out.append(entry)
    return out
