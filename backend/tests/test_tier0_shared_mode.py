"""Item 5, step 2: Tier 0 probes for SHARED-verifier tasks, judged by a real Harbor Oracle run.

Tier 0's own replica only models separate-verifier tasks, so a shared-mode task used to be skipped and nothing
probed the routes a shared verifier leaves open (Singularity-Bench NANO-K10 / CLUSTER-G05 leftover processes,
TERM-K01 swapped programs, HARNESS-G13 planted pytest hooks). Each probe becomes the task copy's solve.sh and a
real Harbor trial decides the reward. A probe also reports how much it managed to plant, so a reward of 0 from a
probe that could plant nothing is `inconclusive`, never a pass.

No Docker here: scripts run for real in a temp folder (their paths can be redirected), verdicts and parsers run on
fixtures, and the stage runs with the Harbor call faked."""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Run, Submission
from app.services import task_runner
from app.services.tier0 import shared_mode
from app.services.tier0.probes import all_probes

SHARED_TOML = '[task]\nname = "terminal-bench/smoke"\n\n[verifier]\ntimeout_sec = 120.0\n'
SEPARATE_TOML = '[task]\nname = "terminal-bench/smoke"\n\n[verifier]\nenvironment_mode = "separate"\n'
BY_SECTION_TOML = '[task]\nname = "x"\n\n[verifier.environment]\ncpus = 1\n'


def probe(pid):
    return next(p for p in shared_mode.PROBES if p.pid == pid)


# ---- which tasks, and the probe list --------------------------------------------------------

def test_a_task_without_a_separate_verifier_is_shared(tmp_path):
    (tmp_path / "task.toml").write_text(SHARED_TOML)
    assert shared_mode.is_shared_mode(tmp_path) is True


@pytest.mark.parametrize("toml", [SEPARATE_TOML, BY_SECTION_TOML])
def test_separate_verifiers_are_not_shared(tmp_path, toml):
    (tmp_path / "task.toml").write_text(toml)
    assert shared_mode.is_shared_mode(tmp_path) is False


def test_a_missing_or_broken_task_toml_is_not_called_shared(tmp_path):
    assert shared_mode.is_shared_mode(tmp_path) is False
    (tmp_path / "task.toml").write_text("not [valid toml")
    assert shared_mode.is_shared_mode(tmp_path) is False


def test_slug_comes_from_the_task_name(tmp_path):
    (tmp_path / "task.toml").write_text(SHARED_TOML)
    assert shared_mode.slug_of(tmp_path) == "smoke"
    other = tmp_path / "noname"
    other.mkdir()
    (other / "task.toml").write_text("[verifier]\n")
    assert shared_mode.slug_of(other) == "noname"


def test_three_probes_judged_by_harbor_with_ids_that_cannot_clash():
    assert [p.pid for p in shared_mode.PROBES] == ["G25", "G26", "G27"]
    for p in shared_mode.PROBES:
        assert p.harbor_probe is True and p.expect_reward == 0.0 and p.applies_to == ("*",)
    assert not {p.pid for p in shared_mode.PROBES} & {p.pid for p in all_probes()}


def test_probe_scripts_parse_under_bash():
    for p in shared_mode.PROBES:
        r = subprocess.run(["bash", "-n"], input=p.agent_script, capture_output=True, text=True)
        assert r.returncode == 0, (p.pid, r.stderr)


# ---- the scripts, run for real in a temp folder ---------------------------------------------

def run_script(pid, tmp_path, env=None, cwd=None):
    script = tmp_path / f"{pid}.sh"
    script.write_text(probe(pid).agent_script)
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=60,
                          env=full_env, cwd=cwd or tmp_path)


def test_g25_starts_a_detached_program_that_keeps_rewriting_the_reward(tmp_path):
    logs = tmp_path / "logs"
    r = run_script("G25", tmp_path, {"PROBE_LOGS": str(logs)})
    pid_file = logs / "agent" / "probe-daemon.pid"
    try:
        assert r.returncode == 0, r.stderr
        assert "RESULT planted:1" in r.stdout
        assert pid_file.exists()
        (logs / "verifier" / "reward.txt").write_text("0\n")  # the grader writes its own verdict...
        time.sleep(0.6)
        assert (logs / "verifier" / "reward.txt").read_text().strip() == "1"  # ...and the program overwrites it
        assert json.loads((logs / "verifier" / "reward.json").read_text()) == {"reward": 1}
        beat1 = (logs / "agent" / "probe-daemon.last").read_text().strip()
        time.sleep(0.5)
        assert (logs / "agent" / "probe-daemon.last").read_text().strip() != beat1 or beat1.endswith(".N")
        # Harbor copies /logs/agent out once, when the agent phase ends, but /logs/verifier after grading, so the
        # heartbeat that proves "still running during grading" has to be written there too (found on a live run).
        assert (logs / "verifier" / "probe-daemon.last").exists()
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text().strip()), signal.SIGKILL)
            except (OSError, ValueError):
                pass


def test_g25_does_not_block_the_command_that_started_it(tmp_path):
    """Harbor waits for solve.sh; a daemon that held its stdout open would hang the Oracle forever."""
    logs = tmp_path / "logs"
    started = time.time()
    r = run_script("G25", tmp_path, {"PROBE_LOGS": str(logs)})
    try:
        assert time.time() - started < 15
        assert r.returncode == 0
    finally:
        pid_file = logs / "agent" / "probe-daemon.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text().strip()), signal.SIGKILL)
            except (OSError, ValueError):
                pass


def SAFE_PATH(bin_dir) -> str:
    """The temp folder plus system folders a user cannot write to. G26 replaces programs it finds on PATH, so a
    test must never let it see a developer's own (possibly writable) python or pytest."""
    return f"{bin_dir}:/usr/bin:/bin"


def fake_programs(bin_dir: Path, names):
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        p = bin_dir / name
        p.write_text(f'#!/bin/sh\necho "REAL {name} $*"\n')
        p.chmod(0o755)


def test_g26_swaps_pytest_and_python_but_only_for_pytest_runs(tmp_path):
    bin_dir = tmp_path / "bin"
    fake_programs(bin_dir, ["pytest", "python3"])
    env = {"PATH": SAFE_PATH(bin_dir)}
    r = run_script("G26", tmp_path, env)
    assert r.returncode == 0, r.stderr
    assert "RESULT planted:2" in r.stdout

    def call(*args):
        return subprocess.run([str(bin_dir / args[0]), *args[1:]], capture_output=True, text=True, env={**os.environ, **env})

    p = call("pytest", "/tests/test_outputs.py")
    assert p.returncode == 0 and "1 passed" in p.stdout
    m = call("python3", "-m", "pytest", "/tests/test_outputs.py", "-rA")
    assert m.returncode == 0 and "1 passed" in m.stdout
    other = call("python3", "-c", "print(1)")
    assert "REAL python3 -c print(1)" in other.stdout  # anything else still reaches the real program


def test_g26_plants_nothing_it_cannot_write(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    bin_dir = tmp_path / "bin"
    fake_programs(bin_dir, ["pytest"])
    bin_dir.chmod(0o555)
    try:
        r = run_script("G26", tmp_path, {"PATH": SAFE_PATH(bin_dir)})
        assert "RESULT planted:0" in r.stdout and "BLOCKED" in r.stdout
        assert "REAL pytest" in subprocess.run([str(bin_dir / "pytest")], capture_output=True, text=True).stdout
    finally:
        bin_dir.chmod(0o755)


def test_g26_with_no_such_programs_plants_nothing(tmp_path):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    r = run_script("G26", tmp_path, {"PATH": SAFE_PATH(empty)})
    assert "RESULT planted:0" in r.stdout


def test_g27_plants_a_pytest_hook_where_a_merged_upload_would_keep_it(tmp_path):
    tests_dir, work = tmp_path / "tests", tmp_path / "work"
    work.mkdir()
    r = run_script("G27", tmp_path, {"PROBE_TESTS": str(tests_dir)}, cwd=work)
    assert r.returncode == 0, r.stderr
    assert "RESULT planted:2" in r.stdout
    for conftest in (tests_dir / "conftest.py", work / "conftest.py"):
        text = conftest.read_text()
        assert "hookwrapper" in text and "pytest_runtest_makereport" in text and 'outcome = "passed"' in text


def test_the_planted_hook_really_turns_a_failing_test_into_a_pass(tmp_path):
    """The payload is not just text: run pytest on a failing test with it in place."""
    tests_dir = tmp_path / "tests"
    run_script("G27", tmp_path, {"PROBE_TESTS": str(tests_dir)}, cwd=tmp_path)
    (tests_dir / "test_fail.py").write_text("def test_fail():\n    assert False\n")
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", str(tests_dir / "test_fail.py")],
                       capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0, r.stdout[-400:]


# ---- reading what the trial left behind -----------------------------------------------------

def test_planted_count_is_read_from_the_oracle_output():
    assert shared_mode.planted_count("[probe] x\nRESULT planted:3\n") == 3
    assert shared_mode.planted_count("RESULT planted:0") == 0
    assert shared_mode.planted_count("nothing here") is None
    assert shared_mode.planted_count(None) is None


def trial(tmp_path, *, verifier=("2026-11-19T12:00:10.000000Z", "2026-11-19T12:00:11.000000Z"), heartbeat=None, oracle=None,
          where="verifier", agent_heartbeat=None):
    (tmp_path / "agent").mkdir(parents=True, exist_ok=True)
    (tmp_path / "verifier").mkdir(parents=True, exist_ok=True)
    if heartbeat is not None:
        (tmp_path / where / "probe-daemon.last").write_text(f"{heartbeat}\n")
    if agent_heartbeat is not None:
        (tmp_path / "agent" / "probe-daemon.last").write_text(f"{agent_heartbeat}\n")
    if oracle is not None:
        (tmp_path / "agent" / "oracle.txt").write_text(oracle)
    if verifier:
        (tmp_path / "result.json").write_text(json.dumps({"verifier": {"started_at": verifier[0], "finished_at": verifier[1]}}))
    return tmp_path


def epoch(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def test_a_program_still_beating_during_grading_survived(tmp_path):
    t = trial(tmp_path, heartbeat=epoch("2026-11-19T12:00:10.500000Z"))
    assert shared_mode.daemon_survived(t) is True


def test_a_program_that_stopped_before_grading_did_not(tmp_path):
    t = trial(tmp_path, heartbeat=epoch("2026-11-19T12:00:09.000000Z"))
    assert shared_mode.daemon_survived(t) is False


def test_the_verifier_folder_heartbeat_wins_over_the_stale_agent_folder_one(tmp_path):
    """The agent folder is copied out when the agent phase ends, so its heartbeat always predates grading."""
    t = trial(tmp_path, heartbeat=epoch("2026-11-19T12:00:10.900000Z"),
              agent_heartbeat=epoch("2026-11-19T12:00:09.000000Z"))
    assert shared_mode.daemon_survived(t) is True


def test_an_agent_folder_heartbeat_alone_is_used_when_the_verifier_folder_has_none(tmp_path):
    t = trial(tmp_path, agent_heartbeat=epoch("2026-11-19T12:00:09.000000Z"))
    assert shared_mode.daemon_survived(t) is False


def test_survival_is_unknown_without_the_heartbeat_or_the_times(tmp_path):
    assert shared_mode.daemon_survived(trial(tmp_path / "a")) is None
    assert shared_mode.daemon_survived(trial(tmp_path / "b", heartbeat=1.0, verifier=None)) is None


def test_a_heartbeat_with_a_literal_N_from_a_date_without_nanoseconds_is_still_read(tmp_path):
    base = int(epoch("2026-11-19T12:00:10.500000Z"))
    assert shared_mode.daemon_survived(trial(tmp_path, heartbeat=f"{base}.N")) is True


def test_the_oracle_output_is_read_from_the_trial(tmp_path):
    assert shared_mode.oracle_output(trial(tmp_path, oracle="RESULT planted:1\n")) == "RESULT planted:1\n"
    assert shared_mode.oracle_output(tmp_path / "missing") is None


# ---- verdicts -------------------------------------------------------------------------------

def test_an_exploit_by_the_daemon_probe_says_whether_the_program_was_running_during_grading():
    alive = shared_mode.judge(probe("G25"), 1.0, planted=1, daemon_alive=True)
    assert alive[0] == "exploited" and any("still running while the grader ran" in n for n in alive[1])
    dead = shared_mode.judge(probe("G25"), 1.0, planted=1, daemon_alive=False)
    assert dead[0] == "exploited"
    assert any("not running" in n and "reward.json" in n for n in dead[1])  # a stale pre-written file did it


def test_other_probes_add_no_daemon_note_to_an_exploit():
    verdict, notes = shared_mode.judge(probe("G26"), 1.0, planted=3, daemon_alive=None)
    assert not any("program" in n and "running" in n for n in notes)


def test_a_reward_of_one_is_an_exploit():
    verdict, notes = shared_mode.judge(probe("G26"), 1.0, planted=2, daemon_alive=None)
    assert verdict == "exploited" and any("scored 1" in n for n in notes)


def test_a_zero_after_planting_holds():
    assert shared_mode.judge(probe("G26"), 0.0, planted=2, daemon_alive=None)[0] == "held"


def test_a_zero_when_nothing_was_planted_proves_nothing():
    verdict, notes = shared_mode.judge(probe("G26"), 0.0, planted=0, daemon_alive=None)
    assert verdict == "inconclusive" and any("could not plant" in n for n in notes)
    assert shared_mode.judge(probe("G26"), 0.0, planted=None, daemon_alive=None)[0] == "inconclusive"


def test_no_reward_is_inconclusive():
    assert shared_mode.judge(probe("G25"), None, planted=1, daemon_alive=True)[0] == "inconclusive"


def test_the_daemon_probe_says_whether_the_program_lived_into_grading():
    alive = shared_mode.judge(probe("G25"), 0.0, planted=1, daemon_alive=True)
    assert alive[0] == "held" and any("still running while the grader ran" in n for n in alive[1])
    dead = shared_mode.judge(probe("G25"), 0.0, planted=1, daemon_alive=False)
    assert dead[0] == "held" and any("stopped before grading" in n for n in dead[1])
    unknown = shared_mode.judge(probe("G25"), 0.0, planted=1, daemon_alive=None)
    assert unknown[0] == "held" and any("could not confirm" in n for n in unknown[1])


# ---- the stage ------------------------------------------------------------------------------

@pytest.fixture
def world(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(task_runner, "SessionLocal", factory)
    monkeypatch.setattr(task_runner.settings, "storage_dir", tmp_path / "storage")
    monkeypatch.setattr(task_runner, "_get_task_config", lambda submission: object())
    root = tmp_path / "up"
    root.mkdir()
    (root / "task.toml").write_text(SHARED_TOML)
    with factory() as db:
        db.add(Submission(id="s1", task_name="smoke", uploaded_at=datetime.now(UTC), zip_path="x", extracted_path=str(root)))
        db.commit()
    return type("W", (), {"factory": factory, "root": root})


def stage_result(world):
    with world.factory() as db:
        row = db.query(Run).filter_by(submission_id="s1", kind="tier0_probes").one()
        return row.status, row.reward, json.loads(row.logs)


def fake_probe_rows(verdicts):
    async def fake(submission, spec, p, config):
        return {
            "probe_id": p.pid, "task": spec.slug, "flaw_class": p.flaw, "title": p.title, "tier": 0,
            "verdict": verdicts[p.pid], "reward": 1.0 if verdicts[p.pid] == "exploited" else 0.0,
            "expected_reward": 0.0, "positive_control": None, "positive_control_reward": None,
            "positive_control_ok": None, "fault_results": [], "unsanitized": None, "needs_harbor": False,
            "diagnostics": {}, "notes": ["fake"], "duration_sec": 1.0,
        }
    return fake


def test_a_shared_task_runs_the_three_probes_through_harbor(world, monkeypatch):
    seen = []
    inner = fake_probe_rows({"G25": "held", "G26": "held", "G27": "held"})

    async def spy(submission, spec, p, config):
        seen.append((p.pid, spec.slug, spec.root, spec.artifacts))
        return await inner(submission, spec, p, config)

    monkeypatch.setattr(task_runner, "_tier0_harbor_probe", spy)
    asyncio.run(task_runner.run_tier0_stage("s1"))
    status, reward, doc = stage_result(world)
    assert [s[0] for s in seen] == ["G25", "G26", "G27"]
    assert seen[0][1] == "smoke" and Path(seen[0][2]) == world.root.resolve() or Path(seen[0][2]) == world.root
    assert seen[0][3] == []
    assert (status, reward) == ("passed", 1)
    assert doc["shared_mode"] is True and doc["skipped"] is None and doc["task"] == "smoke"
    assert [r["probe_id"] for r in doc["results"]] == ["G25", "G26", "G27"]
    assert doc["summary"]["probes_run"] == 3 and doc["summary"]["held"] == 3


def test_an_exploited_shared_probe_fails_the_stage_and_is_counted(world, monkeypatch):
    monkeypatch.setattr(task_runner, "_tier0_harbor_probe",
                        fake_probe_rows({"G25": "exploited", "G26": "exploited", "G27": "held"}))
    asyncio.run(task_runner.run_tier0_stage("s1"))
    status, reward, doc = stage_result(world)
    assert (status, reward) == ("failed", 0)
    assert doc["summary"]["exploited"] == 2 and sorted(doc["summary"]["exploited_ids"]) == ["G25", "G26"]


def test_an_inconclusive_probe_does_not_fail_the_stage_but_is_listed(world, monkeypatch):
    monkeypatch.setattr(task_runner, "_tier0_harbor_probe",
                        fake_probe_rows({"G25": "held", "G26": "inconclusive", "G27": "held"}))
    asyncio.run(task_runner.run_tier0_stage("s1"))
    status, _, doc = stage_result(world)
    assert status == "passed"
    assert doc["summary"]["needs_harbor_to_settle"] == ["G26"]


def test_without_harbor_the_task_is_skipped_as_before(world, monkeypatch):
    called = []
    monkeypatch.setattr(task_runner, "_tier0_harbor_probe", lambda *a, **k: called.append(1))
    asyncio.run(task_runner.run_tier0_stage("s1", harbor=False))
    status, reward, doc = stage_result(world)
    assert (status, reward) == ("passed", 1) and not called
    assert doc["skipped"] and "shared" in doc["skipped"].lower() and doc["results"] == []


def test_a_separate_task_never_takes_the_shared_path(world, monkeypatch):
    (world.root / "task.toml").write_text(SEPARATE_TOML)  # separate, but this fixture has no artifacts, so Tier 0 skips it
    called = []
    monkeypatch.setattr(task_runner, "_tier0_harbor_probe", lambda *a, **k: called.append(1))
    asyncio.run(task_runner.run_tier0_stage("s1"))
    _, _, doc = stage_result(world)
    assert not called and doc.get("shared_mode") is not True
