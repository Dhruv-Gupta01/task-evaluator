"""At least two different correct solutions per task (docs/oracle-solutions.md).

The task declares them in `[metadata] oracle_solutions`; solve.sh picks one through $SOLUTION; each is run
as its own Oracle trial and every run must score 1.
"""
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import Run
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


def test_two_values_that_run_the_same_script_text_are_not_different(tmp_path):
    same = {**TWO, "solution/solve_2.sh": TWO["solution/solve_1.sh"] + "# another comment\n\n"}
    severity, message = check(task(tmp_path, files=same))
    assert severity == "fail" and "same" in message


def test_two_arms_running_the_same_command_are_not_different(tmp_path):
    same = {**TWO, "solution/solve.sh": CASE.replace("solve_2.sh", "solve_1.sh")}
    assert check(task(tmp_path, files=same))[0] == "fail"


def test_a_task_solution_env_that_would_override_the_value_fails(tmp_path):
    severity, message = check(task(tmp_path, extra_toml='\n[solution.env]\nSOLUTION = "1"\n'))
    assert severity == "fail" and "solution.env" in message


# ---- running them ----------------------------------------------------------------------------------

def run_stage(env, monkeypatch, rewards: dict, runs=2):
    client, root, Session = env
    monkeypatch.setattr(task_runner, "SessionLocal", Session)
    monkeypatch.setattr(task_runner, "_get_task_config", lambda s: None)
    monkeypatch.setattr(task_runner, "_oracle_timeout_sec", lambda c: 10)
    seen = []

    async def pretend(task_root, agent, jobs_dir, job_name, timeout_sec, agent_env=None, **kw):
        seen.append(agent_env["SOLUTION"])
        return SimpleNamespace(reward=rewards[agent_env["SOLUTION"]])

    monkeypatch.setattr(task_runner.harbor_runner, "run_single", pretend)
    asyncio.run(task_runner.run_solution_variants_stage("s1", runs))
    return client.get("/submissions/s1").json()["solution_variants"], seen


def declare(root: Path, values='["1", "2"]'):
    toml = (root / "task.toml").read_text()
    (root / "task.toml").write_text(toml + f"\n[metadata]\noracle_solutions = {values}\n")
    (root / "solution").mkdir(exist_ok=True)
    for rel, text in TWO.items():
        (root / rel).write_text(text)


def test_every_value_scoring_1_every_time_passes(env, monkeypatch):
    declare(env[1])
    out, seen = run_stage(env, monkeypatch, {"1": 1.0, "2": 1.0})
    assert out["status"] == "passed" and seen == ["1", "1", "2", "2"]
    assert [(v["value"], v["status"], v["rewards"]) for v in out["values"]] == [("1", "passed", [1.0, 1.0]), ("2", "passed", [1.0, 1.0])]


def test_one_rejected_solution_fails_and_is_named_in_the_evidence(env, monkeypatch):
    client = env[0]
    declare(env[1])
    out, _ = run_stage(env, monkeypatch, {"1": 1.0, "2": 0.0}, runs=1)
    assert out["status"] == "failed" and out["values"][1]["status"] == "failed"
    bundle = client.get("/submissions/s1/evidence").json()
    assert bundle["solution_variants"]["status"] == "failed" and "SOLUTION=2" in bundle["summary_markdown"]


def test_a_run_with_no_reward_is_not_a_pass(env, monkeypatch):
    declare(env[1])
    out, _ = run_stage(env, monkeypatch, {"1": 1.0, "2": None}, runs=1)
    assert out["status"] == "failed"


def test_the_endpoint_refuses_a_task_with_fewer_than_two(env):
    client, root, Session = env
    response = client.post("/submissions/s1/solution-variants")
    assert response.status_code == 400 and "oracle_solutions" in response.json()["detail"]
    declare(root)
    assert client.post("/submissions/s1/solution-variants?runs=9").status_code == 400
    assert client.post("/submissions/s1/solution-variants").json() == {"status": "started"}
