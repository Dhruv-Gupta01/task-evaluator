"""Items 7, 6 and 4 of the Breakwater follow-up list, all on Tier 0.

7  G22  every digit of the reference solution's text output shifted by one: right shape, wrong numbers.
6  G23  the reference output with ONE declared artifact removed at a time: a grader that still scores 1 never
         looked at that file (`artifact_not_checked`; a person decides, some files may be optional).
4  G24  the reference output graded under different time zone / locale / hash-seed settings: a verdict that
         depends on one is `env_sensitive`; a verdict that changes with no setting changed is `flaky`.

No Docker: probe scripts run for real in a temp folder, verdicts run on fake results."""
import dataclasses
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.tier0 import docker_runner, record, runner, selftest
from app.services.tier0.probe import Probe
from app.services.tier0.probes import all_probes, generic


def by_id():
    return {p.pid: p for p in all_probes()}


def _probe(**kw) -> Probe:
    base = dict(pid="T", flaw="-", title="t", applies_to=("*",), agent_script="true")
    base.update(kw)
    return Probe(**base)


def _run(reward, *, exit_code=0, timed_out=False, env_runs=None):
    run = docker_runner.Run(reward=reward, verifier_exit=exit_code, timed_out=timed_out)
    if env_runs is not None:
        run.env_runs = env_runs
    return run


def script_with_fake_solution(pid: str, tmp_path: Path, solve_body: str, artifacts: list[str]):
    """Run a probe script that calls /solution/solve.sh against a fake solution in tmp_path."""
    solve = tmp_path / "solve.sh"
    solve.write_text("#!/bin/bash\n" + solve_body)
    script = tmp_path / f"{pid}.sh"
    script.write_text(by_id()[pid].agent_script.replace("/solution/solve.sh", str(solve)))
    return subprocess.run(["bash", str(script), *artifacts], capture_output=True, text=True, timeout=60)


# ---- 7: digits shifted ----------------------------------------------------------------------

def test_g22_exists_and_expects_zero():
    p = by_id()["G22"]
    assert p.applies_to == ("*",) and p.expect_reward == 0.0 and p.malformed is False
    assert p.check is not None and p.inject == {"/solution": "solution"}  # reference ran AND something was shifted


def test_g22_shifts_every_digit_of_text_outputs_and_leaves_binaries(tmp_path):
    txt, js, binary = tmp_path / "out.txt", tmp_path / "r.json", tmp_path / "blob.bin"
    body = (f'printf "total 0123456789 end\\n" > {txt}\n'
            f'printf \'{{"n": 90}}\\n\' > {js}\n'
            f'printf "\\000\\001 123" > {binary}\n')
    r = script_with_fake_solution("G22", tmp_path, body, [str(txt), str(js), str(binary)])
    assert r.returncode == 0, r.stderr
    assert txt.read_text() == "total 1234567890 end\n"
    assert js.read_text() == '{"n": 01}\n'
    assert binary.read_bytes() == b"\x00\x01 123"
    assert "RESULT oracle_artifacts:3" in r.stdout


def test_g22_reports_zero_artifacts_when_the_solution_made_nothing(tmp_path):
    r = script_with_fake_solution("G22", tmp_path, "true\n", [str(tmp_path / "missing.txt")])
    assert "RESULT oracle_artifacts:0" in r.stdout


# ---- 6: one artifact dropped at a time ------------------------------------------------------

def test_g23_is_a_per_artifact_template():
    p = by_id()["G23"]
    assert p.per_artifact is True and p.applies_to == ("*",) and p.expect_reward == 0.0
    assert p.check is generic._oracle_ran and p.inject == {"/solution": "solution"}


def spec_with(*artifacts):
    return SimpleNamespace(artifacts=list(artifacts))


def test_expansion_makes_one_probe_per_artifact():
    tmpl = by_id()["G23"]
    out = runner.expand_per_artifact([tmpl], spec_with("/app/a.json", "/app/b.csv", "/app/out/"))
    assert [p.pid for p in out] == ["G23-0", "G23-1", "G23-2"]
    assert [p.drops_artifact for p in out] == ["/app/a.json", "/app/b.csv", "/app/out/"]
    assert all(p.per_artifact is False for p in out)
    assert "/app/b.csv" in out[1].title
    assert "DROP=1\n" in out[1].agent_script


def test_expansion_is_capped_and_skipped_for_a_single_artifact():
    tmpl = by_id()["G23"]
    assert len(runner.expand_per_artifact([tmpl], spec_with(*[f"/a/{i}.txt" for i in range(12)]))) == 8
    assert runner.expand_per_artifact([tmpl], spec_with("/app/only.json")) == []  # G0 already covers "nothing there"
    assert runner.expand_per_artifact([tmpl], spec_with()) == []


def test_other_probes_pass_through_expansion_untouched():
    g14 = by_id()["G14"]
    out = runner.expand_per_artifact([g14, by_id()["G23"]], spec_with("/a.txt", "/b.txt"))
    assert out[0] is g14 and [p.pid for p in out] == ["G14", "G23-0", "G23-1"]


def test_expanded_probes_get_unique_ids_that_dont_clash_with_the_registry():
    out = runner.expand_per_artifact([by_id()["G23"]], spec_with("/a.txt", "/b.txt", "/c.txt"))
    ids = [p.pid for p in out]
    assert len(set(ids)) == len(ids) and not set(ids) & {p.pid for p in all_probes()}


def test_g23_removes_only_the_chosen_artifact(tmp_path):
    a, b, c = tmp_path / "a.txt", tmp_path / "b.txt", tmp_path / "c.txt"
    body = f"echo A > {a}; echo B > {b}; echo C > {c}\n"
    probe = runner.expand_per_artifact([by_id()["G23"]], spec_with(str(a), str(b), str(c)))[1]
    script = tmp_path / "p.sh"
    solve = tmp_path / "solve.sh"
    solve.write_text("#!/bin/bash\n" + body)
    script.write_text(probe.agent_script.replace("/solution/solve.sh", str(solve)))
    r = subprocess.run(["bash", str(script), str(a), str(b), str(c)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert a.exists() and c.exists() and not b.exists()
    assert "RESULT oracle_artifacts:3" in r.stdout


def test_dropping_a_directory_artifact_works(tmp_path):
    d, f = tmp_path / "out", tmp_path / "x.txt"
    body = f"mkdir -p {d}; echo 1 > {d}/part.txt; echo 2 > {f}\n"
    probe = runner.expand_per_artifact([by_id()["G23"]], spec_with(str(d), str(f)))[0]
    script, solve = tmp_path / "p.sh", tmp_path / "solve.sh"
    solve.write_text("#!/bin/bash\n" + body)
    script.write_text(probe.agent_script.replace("/solution/solve.sh", str(solve)))
    subprocess.run(["bash", str(script), str(d), str(f)], capture_output=True, text=True, timeout=60)
    assert not d.exists() and f.exists()


def test_a_grader_that_still_passes_without_the_artifact_is_a_finding():
    probe = _probe(drops_artifact="/app/b.csv")
    verdict, notes = runner.judge(probe, _run(1.0), spec=None)
    assert verdict == "artifact_not_checked"
    assert any("/app/b.csv" in n for n in notes)


def test_a_grader_that_fails_without_the_artifact_holds():
    assert runner.judge(_probe(drops_artifact="/app/b.csv"), _run(0.0), spec=None)[0] == "held"


def test_artifact_not_checked_is_evidence_but_not_an_attack_and_needs_a_human():
    from app.services import task_runner
    assert "artifact_not_checked" in record.EVIDENTIAL and "artifact_not_checked" in record._NOT_ATTACKS
    assert "artifact_not_checked" in task_runner._TIER0_ATTENTION
    results = [record.ProbeResult(probe_id=p, task="t", flaw_class="-", title=p, tier=0, verdict=v)
               for p, v in (("G23-1", "artifact_not_checked"), ("G14", "held"))]
    s = record.summarise(results)
    assert s["artifact_not_checked"] == 1 and s["artifact_not_checked_ids"] == ["G23-1"]
    assert s["attack_success_rate"] == 0.0


def test_run_suite_expands_the_template_before_running(monkeypatch):
    seen = []
    monkeypatch.setattr(runner, "run_one", lambda spec, probe, opts: seen.append(probe.pid) or
                        record.ProbeResult(probe_id=probe.pid, task="t", flaw_class="-", title="", tier=0, verdict="held"))
    out = runner.run_suite(spec_with("/a.txt", "/b.txt"), [by_id()["G23"]], runner.Options())
    assert seen == ["G23-0", "G23-1"] and [r.probe_id for r in out] == seen


# ---- 4: environment sweep -------------------------------------------------------------------

def sweep(*pairs):
    return [{"label": label, "reward": reward} for label, reward in pairs]


def test_g24_is_a_valid_variant_sweep_over_the_reference_output():
    p = by_id()["G24"]
    assert p.kind == "valid_variant" and p.env_sweep is True and p.applies_to == ("*",)
    assert p.inject == {"/solution": "solution"}
    labels = [label for label, _ in generic.ENV_SETTINGS]
    assert labels[:2] == ["baseline", "baseline-again"]
    assert {"TZ=Pacific/Kiritimati", "LC_ALL=C", "LC_ALL=en_US.UTF-8", "PYTHONHASHSEED=0", "PYTHONHASHSEED=1"} <= set(labels)
    assert dict(generic.ENV_SETTINGS)["baseline"] == {} and dict(generic.ENV_SETTINGS)["LC_ALL=C"] == {"LC_ALL": "C"}


def test_a_verdict_that_follows_one_setting_is_env_sensitive():
    probe = by_id()["G24"]
    runs = sweep(("baseline", 1.0), ("baseline-again", 1.0), ("TZ=Pacific/Kiritimati", 0.0), ("LC_ALL=C", 1.0))
    verdict, notes = runner.judge(probe, _run(1.0, env_runs=runs), spec=None)
    assert verdict == "env_sensitive"
    assert any("TZ=Pacific/Kiritimati" in n for n in notes)
    assert not any("LC_ALL=C" in n and "differs" in n for n in notes)


def test_a_crash_under_one_setting_counts_as_a_difference():
    runs = sweep(("baseline", 1.0), ("baseline-again", 1.0), ("LC_ALL=C", None))
    assert runner.judge(by_id()["G24"], _run(1.0, env_runs=runs), spec=None)[0] == "env_sensitive"


def test_a_verdict_that_changes_with_nothing_changed_is_flaky_not_env_sensitive():
    runs = sweep(("baseline", 1.0), ("baseline-again", 0.0), ("TZ=Pacific/Kiritimati", 0.0))
    verdict, notes = runner.judge(by_id()["G24"], _run(1.0, env_runs=runs), spec=None)
    assert verdict == "flaky"
    assert any("no setting changed" in n for n in notes)


def test_a_stable_passing_sweep_is_a_valid_solution_accepted():
    runs = sweep(("baseline", 1.0), ("baseline-again", 1.0), ("TZ=Pacific/Kiritimati", 1.0), ("LC_ALL=C", 1.0))
    assert runner.judge(by_id()["G24"], _run(1.0, env_runs=runs), spec=None)[0] == "valid_accepted"


def test_a_sweep_where_the_reference_never_passes_is_not_blamed_on_the_environment():
    runs = sweep(("baseline", 0.0), ("baseline-again", 0.0), ("LC_ALL=C", 0.0))
    verdict, notes = runner.judge(by_id()["G24"], _run(0.0, env_runs=runs), spec=None)
    assert verdict == "inconclusive" and any("G13" in n for n in notes)


def test_a_sweep_that_never_ran_is_inconclusive():
    assert runner.judge(by_id()["G24"], _run(1.0, env_runs=[]), spec=None)[0] == "inconclusive"


def test_env_sensitive_is_evidence_but_not_an_attack_and_needs_a_human():
    from app.services import task_runner
    assert "env_sensitive" in record.EVIDENTIAL and "env_sensitive" in record._NOT_ATTACKS
    assert "env_sensitive" in task_runner._TIER0_ATTENTION
    s = record.summarise([record.ProbeResult(probe_id="G24", task="t", flaw_class="-", title="", tier=0, verdict="env_sensitive")])
    assert s["env_sensitive"] == 1 and s["env_sensitive_ids"] == ["G24"]


def test_run_one_hands_the_settings_to_the_docker_runner(monkeypatch):
    got = {}

    def fake_run_probe(spec, probe, **kw):
        got.update(kw)
        return _run(1.0, env_runs=sweep(("baseline", 1.0), ("baseline-again", 1.0), ("LC_ALL=C", 0.0)))

    monkeypatch.setattr(docker_runner, "run_probe", fake_run_probe)
    spec = SimpleNamespace(slug="t")
    res = runner.run_one(spec, by_id()["G24"], runner.Options())
    assert got["verifier_envs"] == generic.ENV_SETTINGS
    assert res.verdict == "env_sensitive"
    assert res.diagnostics["env_runs"][2] == {"label": "LC_ALL=C", "reward": 0.0}


def test_other_probes_get_no_settings(monkeypatch):
    got = {}
    monkeypatch.setattr(docker_runner, "run_probe", lambda spec, probe, **kw: got.update(kw) or _run(0.0))
    runner.run_one(SimpleNamespace(slug="t"), by_id()["G14"], runner.Options())
    assert not got.get("verifier_envs")


def test_the_verifier_exec_carries_the_environment_variables():
    assert docker_runner._exec_env_args({}) == []
    assert docker_runner._exec_env_args({"TZ": "Pacific/Kiritimati", "LC_ALL": "C"}) == [
        "-e", "TZ=Pacific/Kiritimati", "-e", "LC_ALL=C"]


# ---- the new probes keep the suite healthy --------------------------------------------------

def test_registry_ids_unique_and_selftest_clean():
    ids = [p.pid for p in all_probes()]
    assert len(ids) == len(set(ids))
    assert {"G22", "G23", "G24"} <= set(ids)
    assert selftest.run() == [], selftest.run()


def test_g22_g23_g24_scripts_parse_under_bash():
    for pid in ("G22", "G23", "G24"):
        r = subprocess.run(["bash", "-n"], input=by_id()[pid].agent_script, capture_output=True, text=True)
        assert r.returncode == 0, (pid, r.stderr)


def test_probe_is_a_frozen_dataclass_so_expansion_copies_it():
    p = by_id()["G23"]
    q = dataclasses.replace(p, pid="X")
    assert q.pid == "X" and p.pid == "G23"


# ---- the glue that grades once per setting (Docker itself is faked) -------------------------

def test_verify_under_settings_grades_once_per_setting_and_keeps_the_baseline(monkeypatch, tmp_path):
    calls = []
    rewards = {"baseline": 1.0, "baseline-again": 1.0, "LC_ALL=C": 0.0}

    def fake_verify(spec, one, staged, platform, mutant, keep, diagnostics, verifier_env=None):
        label = next(l for l, e in generic.ENV_SETTINGS if e == (verifier_env or {}) and l in rewards and l not in calls)
        calls.append(label)
        one.reward, one.verifier_exit, one.verifier_stdout = rewards[label], 0, f"ran {label}"
        one.notes.append(f"note from {label}")

    monkeypatch.setattr(docker_runner, "_verify", fake_verify)
    run = docker_runner.Run(artifacts={"/app/out.json": {"restored": True}})
    settings = [s for s in generic.ENV_SETTINGS if s[0] in rewards]
    docker_runner._verify_under_settings(SimpleNamespace(slug="t"), run, tmp_path, None, None, False, settings)

    assert calls == ["baseline", "baseline-again", "LC_ALL=C"]
    assert [(r["label"], r["reward"]) for r in run.env_runs] == [("baseline", 1.0), ("baseline-again", 1.0), ("LC_ALL=C", 0.0)]
    assert run.reward == 1.0 and run.verifier_stdout == "ran baseline"  # the first setting is the run's own result
    assert any("[LC_ALL=C] note from LC_ALL=C" in n for n in run.notes)
    assert runner.judge(by_id()["G24"], run, spec=None)[0] == "env_sensitive"


def test_each_setting_gets_its_own_copy_of_the_artifacts(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(docker_runner, "_verify", lambda spec, one, *a, **k: seen.append(id(one.artifacts)) or setattr(one, "reward", 1.0))
    run = docker_runner.Run(artifacts={"/a": {"restored": True}})
    docker_runner._verify_under_settings(SimpleNamespace(slug="t"), run, tmp_path, None, None, False, [("a", {}), ("b", {})])
    assert len(set(seen)) == 2 and id(run.artifacts) not in seen
