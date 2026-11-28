"""Tier 0 with every declared solution, and the three Oracle probes from the post-fixes branch's Probes stage.

1. Every Tier 0 probe that runs the author's solution (G13, G16, G22, G23, G24) is expanded into one probe per
   value of `[metadata] oracle_solutions`, and the replica passes `SOLUTION=<value>` into the agent container.
2. G28 (a tests/ data folder the grader reads removed), G29 (a directory at the first artifact) and G30 (a 4 GB
   sparse file there) run through a real Harbor Oracle trial on a task copy, once per declared solution. Each
   solution's control is its own passing Oracle-gate runs. Harbor is faked here, so nothing is spent.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Run, Submission
from app.services import harbor_runner, task_runner
from app.services.tier0 import docker_runner, oracle_probes, runner
from app.services.tier0.probes import generic
from tests._fixtures import make_task, write

SOLUTION_PROBES = {"G13", "G16", "G22", "G23", "G24"}


def task(tmp_path: Path, solutions=("fast", "slow"), artifacts='["/app/out.json"]') -> Path:
    root = make_task(tmp_path / "t", test_py='DATA = "/tests/cases/"\nREF = "/tests/expected/"\n')
    toml = (root / "task.toml").read_text().replace('artifacts = ["/app/out.json"]', f"artifacts = {artifacts}")
    if solutions is not None:
        toml = toml.replace("[metadata]\n", f"[metadata]\noracle_solutions = {json.dumps(list(solutions))}\n", 1)
    (root / "task.toml").write_text(toml)
    write(root, {
        "solution/solve.sh": 'case "$SOLUTION" in fast) echo 1 > /app/out.json ;; slow) echo 1 > /app/out.json ;; esac\n',
        "tests/cases/a.json": "{}", "tests/expected/a.json": "{}",
        "tests/unused/x.json": "{}",  # never named by the grader: not a data folder it reads
        "tests/__pycache__/c.pyc": "x",
        "tests/Dockerfile": "FROM python:3.12-slim\n",  # a separate verifier needs its own image
    })
    return root


# ---- 1. the replica probes, once per solution ----------------------------------------------------------

def test_exactly_the_probes_that_run_the_solution_are_marked():
    marked = {p.pid for p in generic.PROBES if p.uses_solution}
    assert marked == SOLUTION_PROBES
    for p in generic.PROBES:  # nothing else may quietly run solve.sh without SOLUTION
        if "/solution/solve.sh" in p.agent_script and "bash /solution/solve.sh" in p.agent_script:
            assert p.uses_solution, p.pid


def test_expand_per_solution(tmp_path):
    spec = SimpleNamespace(root=task(tmp_path), artifacts=["/app/out.json"])
    g13 = next(p for p in generic.PROBES if p.pid == "G13")
    g0 = next(p for p in generic.PROBES if p.pid == "G0")
    out = runner.expand_per_solution([g13, g0], spec)
    assert [p.pid for p in out] == ["G13@fast", "G13@slow", "G0"]
    assert out[0].solution == "fast" and "SOLUTION=fast" in out[0].title and out[2].solution is None


def test_no_declared_solutions_leaves_the_probes_alone(tmp_path):
    spec = SimpleNamespace(root=task(tmp_path, solutions=None), artifacts=["/app/out.json"])
    g13 = next(p for p in generic.PROBES if p.pid == "G13")
    assert runner.expand_per_solution([g13], spec) == [g13]


def test_per_artifact_and_per_solution_combine(tmp_path):
    spec = SimpleNamespace(root=task(tmp_path, artifacts='["/app/a.json", "/app/b.json"]'),
                           artifacts=["/app/a.json", "/app/b.json"])
    g23 = next(p for p in generic.PROBES if p.pid == "G23")
    out = runner.expand_per_solution(runner.expand_per_artifact([g23], spec), spec)
    assert [p.pid for p in out] == ["G23-0@fast", "G23-0@slow", "G23-1@fast", "G23-1@slow"]


def test_the_replica_passes_solution_into_the_agent_container():
    g13 = next(p for p in generic.PROBES if p.pid == "G13")
    import dataclasses
    assert docker_runner._agent_env_args(dataclasses.replace(g13, solution="fast")) == ["-e", "SOLUTION=fast"]
    assert docker_runner._agent_env_args(g13) == []


# ---- 2. the Oracle probes: plan, apply, judge ------------------------------------------------------------

def test_plan_covers_every_solution(tmp_path):
    root = task(tmp_path)
    plan = oracle_probes.plan(root)
    assert [e["pid"] for e in plan] == [
        "G28-0@fast", "G28-0@slow", "G28-1@fast", "G28-1@slow", "G29@fast", "G29@slow", "G30@fast", "G30@slow",
    ]
    assert {e["folder"] for e in plan if e["kind"] == "data_removal"} == {"cases", "expected"}
    assert all(e["artifact"] == "/app/out.json" for e in plan if e["kind"] != "data_removal")


def test_plan_without_solutions_or_artifacts(tmp_path):
    root = task(tmp_path, solutions=None)
    assert [e["solution"] for e in oracle_probes.plan(root)] == [None] * 4
    bare = make_task(tmp_path / "bare")
    (bare / "task.toml").write_text((bare / "task.toml").read_text().replace('artifacts = ["/app/out.json"]\n', ""))
    assert oracle_probes.plan(bare) == []  # no data folder, no artifact: nothing to probe


def test_a_folder_harbor_never_uploads_is_not_removed(tmp_path):
    root = task(tmp_path)
    (root / "tests" / ".harborignore").write_text("expected/\n")
    assert {e["folder"] for e in oracle_probes.plan(root) if e["kind"] == "data_removal"} == {"cases"}


def test_apply_removes_the_folder_or_plants_after_the_real_solution(tmp_path):
    root = task(tmp_path)
    plan = {e["pid"]: e for e in oracle_probes.plan(root)}
    import shutil
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    oracle_probes.apply(copy, plan["G28-0@fast"])
    assert not (copy / "tests" / "cases").exists() and (copy / "tests" / "expected").exists()
    copy2 = tmp_path / "copy2"
    shutil.copytree(root, copy2)
    oracle_probes.apply(copy2, plan["G30@slow"])
    script = (copy2 / "solution" / "solve.sh").read_text()
    assert (copy2 / "solution" / "solve_probe_original.sh").read_text().startswith('case "$SOLUTION"')
    assert script.index("solve_probe_original.sh") < script.index("truncate -s 4G")
    assert 'A="/app/out.json"' in script


@pytest.mark.parametrize("kind, reward, control, verdict", [
    ("data_removal", 1.0, True, "fail_open"),
    ("data_removal", 0.0, True, "held"),
    ("data_removal", None, True, "held"),
    ("directory", 1.0, True, "exploited"),
    ("oversized", None, True, "crashed_on_bad_input"),
    ("directory", 0.0, True, "held"),
])
def test_judge(kind, reward, control, verdict):
    entry = {"pid": "x", "kind": kind, "solution": "fast", "folder": "cases", "artifact": "/app/out.json"}
    out = oracle_probes.judge(entry, reward, {"passed": 3, "tests": 10}, tests=10)
    assert out["verdict"] == verdict


def test_a_held_result_without_a_passing_gate_run_is_unproven():
    entry = {"pid": "x", "kind": "directory", "solution": "slow", "artifact": "/app/out.json"}
    out = oracle_probes.judge(entry, 0.0, {"passed": 0, "tests": None}, tests=None)
    assert out["verdict"] == "held" and out["positive_control_ok"] is False
    assert any("SOLUTION=slow" in n and "never scored 1" in n for n in out["notes"])
    assert oracle_probes.judge(entry, 0.0, None, tests=None)["positive_control_ok"] is False


def test_fewer_tests_collected_is_named_15_8():
    entry = {"pid": "x", "kind": "data_removal", "solution": "fast", "folder": "cases"}
    out = oracle_probes.judge(entry, 1.0, {"passed": 3, "tests": 10}, tests=4)
    assert out["verdict"] == "fail_open" and any("15.8" in n and "4 test(s)" in n for n in out["notes"])


# ---- the stage, with Harbor faked ------------------------------------------------------------------------

@pytest.fixture
def world(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(task_runner, "SessionLocal", factory)
    monkeypatch.setattr(task_runner.settings, "storage_dir", tmp_path / "storage")
    root = task(tmp_path)
    with factory() as db:
        db.add(Submission(id="s1", task_name="t", uploaded_at=datetime.now(UTC), zip_path="x", extracted_path=str(root)))
        ctrf = json.dumps({"ctrf": {"tests": 10, "passed": 10, "failed": 0, "skipped": 0}})
        for i, sol in enumerate(["fast", "fast", "fast", "slow", "slow", "slow"]):
            db.add(Run(submission_id="s1", kind="oracle", run_index=i, solution=sol, status="passed",
                       reward=1 if sol == "fast" else 0, verifier_json=ctrf))
        db.commit()
    # The replica part is not under test: no Docker probes, no replay, no images.
    monkeypatch.setattr(task_runner.tier0_runner, "run_suite", lambda *a, **k: [])
    monkeypatch.setattr(task_runner.tier0_runner, "run_replays", lambda *a, **k: [])
    monkeypatch.setattr(task_runner.tier0_docker, "cleanup_images", lambda spec: 0)
    from app.services.tier0 import selftest
    monkeypatch.setattr(selftest, "run", lambda spec: [])
    monkeypatch.setattr(task_runner.tier0_probes, "for_task", lambda slug: [])
    return SimpleNamespace(factory=factory, root=root)


def test_the_stage_runs_every_oracle_probe_with_its_solution(world, monkeypatch):
    calls = []

    async def fake_single(copy_root, agent, jobs_dir, job_name, timeout_sec, agent_env=None, **kw):
        removed_cases = not (Path(copy_root) / "tests" / "cases").exists()
        calls.append((agent, (agent_env or {}).get("SOLUTION"), removed_cases))
        reward = 1.0 if removed_cases else 0.0  # the grader passes without its cases folder: fail open
        return SimpleNamespace(reward=reward, report={"ctrf": {"tests": 4 if removed_cases else 10}}, logs="",
                               trial_dir=None)

    monkeypatch.setattr(harbor_runner, "run_single", fake_single)
    asyncio.run(task_runner.run_tier0_stage("s1"))
    with world.factory() as db:
        row = db.query(Run).filter_by(submission_id="s1", kind="tier0_probes").one()
        doc = json.loads(row.logs)
    assert len(calls) == 8 and {c[1] for c in calls} == {"fast", "slow"} and {c[0] for c in calls} == {"oracle"}
    by_id = {r["probe_id"]: r for r in doc["results"]}
    assert by_id["G28-0@fast"]["verdict"] == "fail_open" and by_id["G28-0@slow"]["verdict"] == "fail_open"
    assert by_id["G29@fast"]["verdict"] == "held" and by_id["G29@fast"]["positive_control_ok"] is True
    assert by_id["G29@slow"]["positive_control_ok"] is False  # SOLUTION=slow never scored 1 in the gate
    assert sorted(doc["summary"]["fail_open_ids"]) == ["G28-0@fast", "G28-0@slow"]
    assert "G29@slow" in doc["summary"]["held_but_positive_control_failed"]
    assert row.status == "failed"


def test_without_harbor_the_oracle_probes_do_not_run(world, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("Harbor must not run")
    monkeypatch.setattr(harbor_runner, "run_single", boom)
    asyncio.run(task_runner.run_tier0_stage("s1", harbor=False))
    with world.factory() as db:
        doc = json.loads(db.query(Run).filter_by(submission_id="s1", kind="tier0_probes").one().logs)
    assert not any(r["probe_id"].startswith(("G28", "G29", "G30")) for r in doc["results"])
