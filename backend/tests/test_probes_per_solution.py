"""The valid-variant, weak-answer and edge-case probes run against every declared Oracle solution, each
with its own SOLUTION value, and never count an edit to code that solution does not run."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import Run, Submission
from app.services import edge_cases, task_runner, variant_probe
from app.services import solution_variants as sv
from tests.test_grader_isolation_api import env  # noqa: F401  (fixture)
from tests.test_solution_variants import task

INLINE = '''#!/bin/bash
case "${SOLUTION:-1}" in
  1) echo alpha > /app/o ;;
  2) echo beta > /app/o ;;
  *) exit 2 ;;
esac
'''
HELPERS = {
    "solution/solve.sh": 'case "${SOLUTION:-1}" in\n  1) bash /solution/solve_1.sh ;;\n  2) bash /solution/solve_2.sh ;;\nesac\n',
    "solution/solve_1.sh": "echo a > /app/o\n",
    "solution/solve_2.sh": "python3 /solution/helper.py\n",
    "solution/helper.py": "print('a')\n",
}


def edit(file, find="a", replace="b"):
    return {"file": file, "find": find, "replace": replace}


def test_files_only_another_solution_runs(tmp_path):
    root = task(tmp_path, files=HELPERS)
    assert sv.files_of_other_solutions(root, "1") == {"solution/solve_2.sh", "solution/helper.py"}
    assert sv.files_of_other_solutions(root, "2") == {"solution/solve_1.sh"}


def test_dispatch_by_file_name(tmp_path):
    files = {"solution/solve.sh": "bash /solution/solve_${SOLUTION:-1}.sh\n",
             "solution/solve_1.sh": "echo a\n", "solution/solve_2.sh": "echo b\n"}
    assert sv.files_of_other_solutions(task(tmp_path, files=files), "2") == {"solution/solve_1.sh"}


def test_edit_to_another_solutions_file_is_refused(tmp_path):
    root = task(tmp_path, files=HELPERS)
    problem = variant_probe.apply_edits(root, [edit("solution/helper.py")], "1")
    assert "SOLUTION=1 does not run" in problem
    assert (root / "solution/helper.py").read_text() == "print('a')\n"
    assert variant_probe.apply_edits(root, [edit("solution/helper.py")], "2") is None


def test_edit_inside_another_case_arm_is_refused(tmp_path):
    root = task(tmp_path, files={"solution/solve.sh": INLINE})
    assert "SOLUTION=1 does not run" in variant_probe.apply_edits(root, [edit("solution/solve.sh", "beta")], "1")
    assert variant_probe.apply_edits(root, [edit("solution/solve.sh", "beta")], "2") is None


@pytest.mark.parametrize("mode", ["valid", "broken"])
def test_at_most_four_proposals_and_the_model_sees_only_its_solution(tmp_path, monkeypatch, mode):
    root = task(tmp_path, files=HELPERS)
    (root / "instruction.md").write_text("Write a.")
    seen = {}

    class Client:
        async def complete(self, messages, tools):
            seen["user"] = messages[1].content
            return SimpleNamespace(text=json.dumps({"variants": [
                {"description": f"d{i}", "instruction_quote": "Write a.", "edits": [edit("solution/solve_1.sh")]}
                for i in range(9)]}))

    monkeypatch.setattr(variant_probe, "get_llm_client", Client)
    out = asyncio.run(variant_probe.propose_variants(root, mode, "1"))
    assert len(out["variants"]) == 4
    assert "SOLUTION=1" in seen["user"]
    assert "echo a > /app/o" in seen["user"] and "helper.py" not in seen["user"]


def _fake_harbor(monkeypatch, reward):
    ran = []

    async def run_single(root, agent, jobs_dir, job_name, timeout_sec, **kwargs):
        ran.append((jobs_dir.name, kwargs.get("agent_env")))
        return SimpleNamespace(reward=reward(kwargs.get("agent_env")), report={}, logs="")

    monkeypatch.setattr(task_runner.harbor_runner, "run_single", run_single)
    return ran


def _submission(env, tmp_path, monkeypatch):
    _, _, Session = env
    with Session() as db:
        db.get(Submission, "s1").extracted_path = str(task(tmp_path / "two", files=HELPERS))
        db.commit()
    monkeypatch.setattr(task_runner, "SessionLocal", Session)
    monkeypatch.setattr(task_runner.settings, "storage_dir", tmp_path / "storage")
    monkeypatch.setattr(task_runner, "_oracle_timeout_sec", lambda config: 10)
    monkeypatch.setattr(task_runner, "_get_task_config", lambda submission: None)
    return "s1"


def _logs(env, sub_id, kind):
    with env[2]() as db:
        run = db.query(Run).filter_by(submission_id=sub_id, kind=kind).one()
        return run.status, json.loads(run.logs)


@pytest.mark.parametrize("kind,runner,reward,status", [
    ("variant_probe", task_runner.run_variant_probe, 1.0, "passed"),
    ("weak_probe", task_runner.run_weak_probe, 0.0, "passed"),
])
def test_probe_runs_for_each_solution(env, tmp_path, monkeypatch, kind, runner, reward, status):  # noqa: F811
    sub_id = _submission(env, tmp_path, monkeypatch)

    async def propose(root, mode, solution):
        file = f"solution/solve_{solution}.sh"
        find = "echo" if solution == "1" else "python3"
        return {"variants": [{"description": "d", "instruction_quote": "q", "edits": [edit(file, find, "x")]}],
                "dropped": [{"description": "no", "reason": "r"}], "reason": ""}

    monkeypatch.setattr(variant_probe, "propose_variants", propose)
    ran = _fake_harbor(monkeypatch, lambda e: reward)
    asyncio.run(runner(sub_id))
    got_status, logs = _logs(env, sub_id, kind)
    assert [e for _, e in ran] == [{"SOLUTION": "1"}, {"SOLUTION": "2"}]
    assert len({name for name, _ in ran}) == 2  # separate job folders
    assert [v["solution"] for v in logs["variants"]] == ["1", "2"]
    assert [d["solution"] for d in logs["dropped"]] == ["1", "2"]
    assert got_status == status


def test_edge_probe_runs_for_each_solution_and_any_slip_confirms_the_gap(env, tmp_path, monkeypatch):  # noqa: F811
    sub_id = _submission(env, tmp_path, monkeypatch)

    async def check(root):
        return {"cases": [{"case": "empty", "expected": "e", "covered": False}], "error": None}

    async def breaks(root, cases, solution):
        file, find = ("solution/solve_1.sh", "echo") if solution == "1" else ("solution/solve_2.sh", "python3")
        return [{"case": "empty", "description": "d", "instruction_quote": "", "edits": [edit(file, find, "x")]}]

    monkeypatch.setattr(edge_cases, "run_edge_case_check", check)
    monkeypatch.setattr(variant_probe, "propose_edge_breaks", breaks)
    _fake_harbor(monkeypatch, lambda e: 1.0 if e["SOLUTION"] == "2" else 0.0)
    asyncio.run(task_runner.run_edge_case_check(sub_id, probe=True))
    status, logs = _logs(env, sub_id, "edge_cases")
    case = logs["cases"][0]
    assert logs["probe_total"] == logs["probe_done"] == 2
    assert [p["status"] for p in case["probes"]] == ["caught", "slipped_through"]
    assert case["probe"]["status"] == "slipped_through" and case["probe"]["solution"] == "2"
    assert status == "failed"
