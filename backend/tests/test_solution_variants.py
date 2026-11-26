"""At least two different correct solutions per task (docs/oracle-solutions.md).

The task declares them in `[metadata] oracle_solutions`; solve.sh picks one through $SOLUTION. The Oracle runs
every declared solution at least 3 times, and the later stages stay closed until all of them pass.
"""
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import Run, Submission
from app.services import solution_variants as sv
from app.services import task_runner
from app.services.static_checks import StaticCheckReport
from tests._fixtures import make_task
from tests.test_grader_isolation_api import env  # noqa: F401  (fixture)

CASE = '''#!/bin/bash
case "${SOLUTION:-1}" in
  1) bash /solution/solve_1.sh ;;
  2) bash /solution/solve_2.sh ;;
  *) exit 2 ;;
esac
'''
TWO = {"solution/solve.sh": CASE, "solution/solve_1.sh": "echo a > /app/o\n", "solution/solve_2.sh": "printf 'a\\n' > /app/o\n"}


def task(tmp_path: Path, declared='["1", "2"]', files=TWO, extra_toml="") -> Path:
    root = make_task(tmp_path, files=files)
    toml = (root / "task.toml").read_text()
    if declared is not None:
        toml = toml.replace("[metadata]\n", f"[metadata]\noracle_solutions = {declared}\n", 1)
    (root / "task.toml").write_text(toml + extra_toml)
    return root


def check(root):
    report = StaticCheckReport()
    sv.check_oracle_solutions(root, report)
    return {r.name: (r.severity, r.message) for r in report.results}["oracle_solutions"]


def test_two_declared_different_solutions_pass_the_static_check(tmp_path):
    severity, message = check(task(tmp_path))
    assert severity == "info" and "1, 2" in message
    assert sv.declared_values(task(tmp_path / "b")) == ["1", "2"]


@pytest.mark.parametrize("declared", [None, "[]", '["1"]', '["1", "1"]', '"1,2"', '["1", "b a d"]'])
def test_fewer_than_two_usable_values_fails(tmp_path, declared):
    severity, message = check(task(tmp_path, declared=declared))
    assert severity == "fail" and "oracle_solutions" in message


def test_a_solve_sh_that_never_reads_SOLUTION_fails(tmp_path):
    severity, message = check(task(tmp_path, files={"solution/solve.sh": "echo a > /app/o\n"}))
    assert severity == "fail" and "$SOLUTION" in message


def identical(root):
    report = StaticCheckReport()
    sv.check_identical_solutions(root, report)
    found = {r.name: (r.severity, r.message) for r in report.results}
    return found["identical_solutions"]


def test_two_values_that_run_the_same_script_text_are_not_different(tmp_path):
    same = {**TWO, "solution/solve_2.sh": TWO["solution/solve_1.sh"] + "# another comment\n\n"}
    severity, message = identical(task(tmp_path, files=same))
    assert severity == "fail" and "same code" in message


def test_two_arms_running_the_same_command_are_not_different(tmp_path):
    same = {**TWO, "solution/solve.sh": CASE.replace("solve_2.sh", "solve_1.sh")}
    assert identical(task(tmp_path, files=same))[0] == "fail"


def test_two_solution_files_with_the_same_code_fail_even_without_a_case(tmp_path):
    files = {"solution/solve.sh": 'bash "/solution/solve_${SOLUTION:-1}.sh"\n',
             "solution/solve_1.sh": "echo a > /app/o\n", "solution/solve_2.sh": "# other comment\necho a > /app/o\n"}
    severity, message = identical(task(tmp_path, files=files))
    assert severity == "fail" and "solve_1.sh" in message and "solve_2.sh" in message


def test_different_solutions_are_not_flagged(tmp_path):
    assert identical(task(tmp_path))[0] == "info"


def test_a_task_solution_env_that_would_override_the_value_fails(tmp_path):
    severity, message = check(task(tmp_path, extra_toml='\n[solution.env]\nSOLUTION = "1"\n'))
    assert severity == "fail" and "solution.env" in message


# ---- the Oracle gate ---------------------------------------------------------------------------------

def declare(root: Path, values='["1", "2"]'):
    toml = (root / "task.toml").read_text()
    (root / "task.toml").write_text(toml + f"\n[metadata]\noracle_solutions = {values}\n")
    (root / "solution").mkdir(exist_ok=True)
    for rel, text in TWO.items():
        (root / rel).write_text(text)


def add_runs(Session, rows):
    with Session() as db:
        for index, (solution, reward) in enumerate(rows):
            db.add(Run(submission_id="s1", kind="oracle", run_index=index, solution=solution, reward=reward,
                       status="passed" if reward == 1 else "failed", finished_at=datetime.now(UTC)))
        db.commit()


def oracle(client):
    return client.get("/submissions/s1").json()["oracle"]


ALL_GOOD = [("1", 1)] * 3 + [("2", 1)] * 3


def test_the_oracle_endpoint_queues_every_declared_solution_three_times(env):
    client, root, Session = env
    declare(root)
    assert client.post("/submissions/s1/oracle").status_code == 202
    with Session() as db:
        rows = db.query(Run).filter_by(kind="oracle").order_by(Run.run_index).all()
        assert [r.solution for r in rows] == ["1", "1", "1", "2", "2", "2"]


def test_the_oracle_refuses_a_task_with_one_solution_and_fewer_than_three_runs(env):
    client, root, _ = env
    refused = client.post("/submissions/s1/oracle")
    assert refused.status_code == 400 and "oracle_solutions" in refused.json()["detail"]
    declare(root)
    assert client.post("/submissions/s1/oracle?n=2").status_code == 400


def test_identical_solutions_are_refused_by_the_oracle(env):
    client, root, _ = env
    declare(root)
    (root / "solution/solve_2.sh").write_text((root / "solution/solve_1.sh").read_text())
    refused = client.post("/submissions/s1/oracle")
    assert refused.status_code == 400 and "same code" in refused.json()["detail"]


def test_all_solutions_passing_three_times_opens_the_gate(env):
    client, root, Session = env
    declare(root)
    add_runs(Session, ALL_GOOD)
    result = oracle(client)
    assert result["status"] == "passed" and result["all_passed"] is True
    assert [(s["value"], s["passed_runs"], s["passed"]) for s in result["solutions"]] == [("1", 3, True), ("2", 3, True)]
    assert client.post("/submissions/s1/nop").status_code == 202
    assert client.post("/submissions/s1/agent-trials?n=1").status_code == 202


@pytest.mark.parametrize("rows", [
    [("1", 1)] * 3,                                   # the second solution never ran
    [("1", 1)] * 3 + [("2", 1)] * 2,                  # only twice
    [("1", 1)] * 3 + [("2", 1), ("2", 0), ("2", 1)],  # one failure
    [(None, 1)] * 6,                                  # runs from before solutions were recorded
])
def test_anything_less_keeps_the_gate_and_the_later_stages_closed(env, rows):
    client, root, Session = env
    declare(root)
    add_runs(Session, rows)
    result = oracle(client)
    assert result["status"] == "failed" and result["all_passed"] is False
    for path in ("nop", "agent-trials?n=1", "sufficiency", "cheat-trial"):
        response = client.post(f"/submissions/s1/{path}")
        assert response.status_code == 400 and "Oracle" in response.json()["detail"], path


def test_a_task_with_one_solution_never_passes_the_oracle(env):
    client, _, Session = env
    add_runs(Session, [(None, 1)] * 3)
    assert oracle(client)["status"] == "failed"


def test_the_evidence_says_which_solution_did_not_pass(env):
    client, root, Session = env
    declare(root)
    add_runs(Session, [("1", 1)] * 3 + [("2", 0)] * 3)
    summary = client.get("/submissions/s1/evidence").json()["summary_markdown"]
    assert "SOLUTION=2: 0 of 3" in summary


def test_the_runner_passes_each_rows_solution_to_the_oracle(env, monkeypatch):
    client, root, Session = env
    declare(root)
    client.post("/submissions/s1/oracle")
    monkeypatch.setattr(task_runner, "SessionLocal", Session)
    monkeypatch.setattr(task_runner, "_get_task_config", lambda s: SimpleNamespace(
        environment=SimpleNamespace(build_timeout_sec=1), verifier=SimpleNamespace(timeout_sec=1)))
    monkeypatch.setattr(task_runner, "_agent_timeout_sec", lambda c: 1)
    monkeypatch.setattr(task_runner, "_record_checksum", lambda *a, **k: None)
    seen = []

    async def pretend(task_root, agent, jobs_dir, job_name, timeout_sec, agent_env=None, **kw):
        seen.append((agent_env or {}).get("SOLUTION"))
        return SimpleNamespace(reward=1.0, logs="", report=None, task_checksum=None)

    monkeypatch.setattr(task_runner.harbor_runner, "run_single", pretend)
    monkeypatch.setattr(task_runner, "_apply_trial_outcome", lambda run, o: setattr(run, "reward", 1) or setattr(run, "status", "passed"))
    asyncio.run(task_runner.run_oracle_trials("s1", 6))
    assert seen == ["1", "1", "1", "2", "2", "2"]


# ---- Tier 1's patch check ----------------------------------------------------------------------------

def gate_with(monkeypatch, tmp_path, rewards):
    seen = []

    async def pretend(task_root, agent, jobs_dir, job_name, timeout_sec, agent_env=None, **kw):
        value = (agent_env or {}).get("SOLUTION")
        seen.append(value)
        return SimpleNamespace(reward=rewards[value], logs="boom", report=None)

    monkeypatch.setattr(task_runner.harbor_runner, "run_single", pretend)
    candidate = task(tmp_path / "candidate")
    ok, note = asyncio.run(task_runner._tier1_gate(candidate, tmp_path / "gate", 10))
    return ok, note, seen


def test_a_patch_must_keep_every_solution_passing(monkeypatch, tmp_path):
    ok, note, seen = gate_with(monkeypatch, tmp_path, {"1": 1.0, "2": 1.0})
    assert ok and seen == ["1", "2"]


def test_a_patch_that_breaks_the_second_solution_is_rejected(monkeypatch, tmp_path):
    ok, note, _ = gate_with(monkeypatch, tmp_path, {"1": 1.0, "2": 0.0})
    assert not ok and "SOLUTION=2" in note


# ---- flakiness is judged per solution (found in the browser) ------------------------------------------

def test_one_solution_failing_every_time_is_not_flaky(env):
    client, root, Session = env
    declare(root)
    add_runs(Session, [("1", 1)] * 3 + [("2", 0)] * 3)
    result = oracle(client)
    assert result["flaky"] is False and result["flaky_note"] is None


def test_one_solution_that_sometimes_fails_is_flaky_and_named(env):
    client, root, Session = env
    declare(root)
    add_runs(Session, [("1", 1)] * 3 + [("2", 1), ("2", 0), ("2", 1)])
    result = oracle(client)
    assert result["flaky"] is True and "SOLUTION=2: 1 of 3" in result["flaky_note"] and "SOLUTION=1" not in result["flaky_note"]
