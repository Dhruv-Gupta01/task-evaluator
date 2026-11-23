"""Item 1 (GitHub Breakwater finding, 3 real Terminal-Bench tasks): a broken output file must score 0.
A verifier that crashes and writes no reward turns an agent's bad answer into a "grader error" that never
counts against it. Tier 0 gets a nameable verdict for that, `crashed_on_bad_input`, and malformed-file probes.

No Docker here: the verdict rules run on a fake Run and the probe scripts run for real in a temp folder."""
import json
import subprocess
from pathlib import Path

import pytest

from app.services.tier0 import docker_runner, record, runner, selftest
from app.services.tier0.probe import Probe
from app.services.tier0.probes import all_probes, generic

MALFORMED_IDS = ["G17", "G18", "G19", "G20", "G21"]


def _probe(**kw) -> Probe:
    base = dict(pid="T1", flaw="-", title="t", applies_to=("*",), agent_script="true")
    base.update(kw)
    return Probe(**base)


def _run(reward, exit_code=0, timed_out=False) -> docker_runner.Run:
    return docker_runner.Run(reward=reward, verifier_exit=exit_code, timed_out=timed_out)


# ---- the verdict ----------------------------------------------------------------------------

def test_malformed_probe_with_no_reward_is_a_crash():
    verdict, notes = runner.judge(_probe(malformed=True), _run(None, exit_code=2), spec=None)
    assert verdict == "crashed_on_bad_input"
    assert any("no reward" in n.lower() for n in notes)


def test_malformed_probe_scoring_zero_holds():
    assert runner.judge(_probe(malformed=True), _run(0.0), spec=None)[0] == "held"


def test_malformed_probe_scoring_one_is_still_an_exploit():
    assert runner.judge(_probe(malformed=True), _run(1.0), spec=None)[0] == "exploited"


def test_a_timeout_is_not_called_a_crash():
    verdict, _ = runner.judge(_probe(malformed=True), _run(None, exit_code=None, timed_out=True), spec=None)
    assert verdict == "inconclusive"


def test_verifier_that_never_ran_is_not_called_a_crash():
    """No exit code means the verifier never started (infrastructure), not that it crashed on the file."""
    verdict, _ = runner.judge(_probe(malformed=True), _run(None, exit_code=None), spec=None)
    assert verdict == "inconclusive"


def test_ordinary_probe_with_no_reward_stays_inconclusive():
    """Unchanged behaviour: only malformed-file probes get the new verdict."""
    verdict, _ = runner.judge(_probe(), _run(None, exit_code=2), spec=None)
    assert verdict == "inconclusive"


# ---- the record -----------------------------------------------------------------------------

def _result(pid, verdict):
    return record.ProbeResult(probe_id=pid, task="t", flaw_class="-", title=pid, tier=0, verdict=verdict)


def test_crash_verdict_is_evidence_but_not_an_attack():
    assert "crashed_on_bad_input" in record.EVIDENTIAL
    assert "crashed_on_bad_input" in record._NOT_ATTACKS


def test_summary_counts_crashes_and_keeps_them_out_of_the_attack_rate():
    results = [_result("G0", "held"), _result("G17", "crashed_on_bad_input"), _result("G14", "exploited")]
    s = record.summarise(results)
    assert s["crashed_on_bad_input"] == 1
    assert s["crashed_on_bad_input_ids"] == ["G17"]
    assert s["attack_success_rate"] == pytest.approx(1 / 2)  # G0 held + G14 exploited; the crash is not an attack


def test_crash_needs_a_human():
    from app.services import task_runner
    assert "crashed_on_bad_input" in task_runner._TIER0_ATTENTION
    assert "crashed_on_bad_input" in task_runner._TIER0_BAD


# ---- the probes -----------------------------------------------------------------------------

def _by_id():
    return {p.pid: p for p in all_probes()}


def test_new_probes_exist_and_apply_everywhere():
    probes = _by_id()
    for pid in MALFORMED_IDS:
        assert pid in probes, pid
        p = probes[pid]
        assert p.applies_to == ("*",)
        assert p.malformed is True
        assert p.expect_reward == 0.0


def test_empty_and_half_probes_are_malformed_too():
    """G0 (empty artifacts) and G16 (half of the reference output) are exactly what crashed glycan-ms2."""
    probes = _by_id()
    assert probes["G0"].malformed and probes["G16"].malformed
    assert not probes["G14"].malformed  # well-formed placeholders: a crash there is a different problem


def test_probe_ids_stay_unique():
    ids = [p.pid for p in all_probes()]
    assert len(ids) == len(set(ids))


def test_selftest_still_clean():
    assert selftest.run() == [], selftest.run()


def _run_script(pid: str, tmp_path: Path, artifacts: list[str]) -> subprocess.CompletedProcess:
    script = tmp_path / f"{pid}.sh"
    script.write_text(_by_id()[pid].agent_script)
    return subprocess.run(["bash", str(script), *artifacts], capture_output=True, text=True, timeout=60)


def test_g17_writes_garbage_and_invalid_utf8(tmp_path):
    a, b = tmp_path / "out.json", tmp_path / "data.csv"
    r = _run_script("G17", tmp_path, [str(a), str(b)])
    assert r.returncode == 0, r.stderr
    for f in (a, b):
        data = f.read_bytes()
        assert data, f"{f.name} is empty"
        with pytest.raises(UnicodeDecodeError):
            data.decode("utf-8")


def test_g18_writes_one_oversized_field_into_text_artifacts(tmp_path):
    csv, js = tmp_path / "r.csv", tmp_path / "r.json"
    r = _run_script("G18", tmp_path, [str(csv), str(js)])
    assert r.returncode == 0, r.stderr
    assert max(len(x) for x in csv.read_text().splitlines()) > 131072  # Python's csv field limit
    assert max(len(x) for x in js.read_text().splitlines()) > 131072


def test_g19_writes_deeply_nested_json(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.jsonl"
    r = _run_script("G19", tmp_path, [str(a), str(b)])
    assert r.returncode == 0, r.stderr
    for f in (a, b):
        text = f.read_text().strip()
        assert text.startswith("[" * 10000) and text.endswith("]" * 10000), f.name


def test_g20_writes_a_bare_nan(tmp_path):
    a = tmp_path / "a.json"
    r = _run_script("G20", tmp_path, [str(a)])
    assert r.returncode == 0, r.stderr
    assert a.read_text().strip() == "NaN"


def test_g21_writes_a_non_workbook_into_a_spreadsheet_or_zip(tmp_path):
    x, z = tmp_path / "book.xlsx", tmp_path / "pack.zip"
    r = _run_script("G21", tmp_path, [str(x), str(z)])
    assert r.returncode == 0, r.stderr
    import zipfile
    for f in (x, z):
        assert zipfile.is_zipfile(f), f"{f.name} should be a real zip container"
        with zipfile.ZipFile(f) as zf:
            assert "[Content_Types].xml" not in zf.namelist()  # a zip, but not a workbook
            assert "junk.txt" in zf.namelist()


def test_scripts_leave_artifacts_with_unrelated_extensions_alone(tmp_path):
    other = tmp_path / "notes.bin"
    other.write_bytes(b"keep")
    for pid in MALFORMED_IDS:
        _run_script(pid, tmp_path, [str(other)])
    # G17 is the general garbage probe and may overwrite anything; the type-specific ones must not.
    for pid in ("G18", "G19", "G20", "G21"):
        other.write_bytes(b"keep")
        _run_script(pid, tmp_path, [str(other)])
        assert other.read_bytes() == b"keep", pid


# ---- a probe that changed nothing proves nothing (found live on pg-wal: G21 "held" with no workbook to damage) ----

def _planted(pid, tmp_path, names):
    files = [tmp_path / n for n in names]
    r = _run_script(pid, tmp_path, [str(f) for f in files])
    assert r.returncode == 0, r.stderr
    line = next((l for l in r.stdout.splitlines() if "RESULT planted:" in l), None)
    assert line is not None, f"{pid} never reported what it planted"
    return int(line.split("RESULT planted:")[1].split()[0])


def test_each_malformed_probe_reports_how_many_artifacts_it_changed(tmp_path):
    assert _planted("G17", tmp_path, ["a.json", "b.csv", "c.bin"]) == 3          # garbage goes into every artifact
    assert _planted("G18", tmp_path, ["a.csv", "b.json", "c.bin"]) == 2          # text types only
    assert _planted("G19", tmp_path, ["a.json", "b.jsonl", "c.csv"]) == 2
    assert _planted("G20", tmp_path, ["a.json", "b.csv"]) == 1
    assert _planted("G21", tmp_path, ["a.xlsx", "b.zip", "c.json"]) == 2


def test_a_probe_with_nothing_of_its_type_to_damage_changed_nothing(tmp_path):
    assert _planted("G21", tmp_path, ["only.json", "blob.tar.gz"]) == 0
    assert _planted("G20", tmp_path, ["blob.tar.gz"]) == 0
    assert _planted("G18", tmp_path, ["blob.bin"]) == 0
    assert _planted("G19", tmp_path, ["a.csv"]) == 0


def test_a_zero_that_changed_nothing_is_inconclusive_not_held():
    probe = _probe(malformed=True, check=generic._planted_check)
    run = _run(0.0)
    run.agent_stdout = "[probe] RESULT planted:0\n"
    verdict, notes = runner.judge(probe, run, spec=None)
    assert verdict == "inconclusive" and any("nothing" in n and "changed" in n for n in notes)


def test_a_zero_after_something_was_changed_still_holds():
    probe = _probe(malformed=True, check=generic._planted_check)
    run = _run(0.0)
    run.agent_stdout = "[probe] RESULT planted:2\n"
    assert runner.judge(probe, run, spec=None)[0] == "held"


def test_a_crash_after_something_was_changed_is_still_a_crash():
    probe = _probe(malformed=True, check=generic._planted_check)
    run = _run(None, exit_code=1)
    run.agent_stdout = "[probe] RESULT planted:1\n"
    assert runner.judge(probe, run, spec=None)[0] == "crashed_on_bad_input"


def test_a_probe_that_never_reported_is_left_to_the_other_rules():
    probe = _probe(malformed=True, check=generic._planted_check)
    assert runner.judge(probe, _run(0.0), spec=None)[0] == "held"


def test_the_typed_malformed_probes_and_the_digit_shift_use_the_planted_check():
    probes = {p.pid: p for p in all_probes()}
    for pid in ("G17", "G18", "G19", "G20", "G21"):
        assert probes[pid].check is generic._planted_check, pid
    assert probes["G22"].check is not None and probes["G22"].check is not generic._oracle_ran  # both checks


def test_the_digit_shift_reports_what_it_changed(tmp_path):
    solve = tmp_path / "solve.sh"
    solve.write_text("#!/bin/bash\necho 'n 12' > " + str(tmp_path / "r.txt") + "\nhead -c 8 /dev/zero > " + str(tmp_path / "x.bin") + "\n")
    probe = next(p for p in all_probes() if p.pid == "G22")
    script = tmp_path / "g22.sh"
    script.write_text(probe.agent_script.replace("/solution/solve.sh", str(solve)))
    r = subprocess.run(["bash", str(script), str(tmp_path / "r.txt"), str(tmp_path / "x.bin")], capture_output=True, text=True)
    assert "RESULT planted:1" in r.stdout and "RESULT oracle_artifacts:2" in r.stdout
    only_binary = tmp_path / "solve2.sh"
    only_binary.write_text("#!/bin/bash\nhead -c 8 /dev/zero > " + str(tmp_path / "y.bin") + "\n")
    script.write_text(probe.agent_script.replace("/solution/solve.sh", str(only_binary)))
    r = subprocess.run(["bash", str(script), str(tmp_path / "y.bin")], capture_output=True, text=True)
    assert "RESULT planted:0" in r.stdout


def test_the_reference_solution_check_describes_what_it_found_without_calling_it_mutilation():
    run = _run(0.0)
    run.agent_stdout = "RESULT oracle_artifacts:2\n"
    _, notes = generic._oracle_ran(run, None)
    assert any("produced 2 artifact" in n for n in notes) and not any("mutilated" in n for n in notes)
