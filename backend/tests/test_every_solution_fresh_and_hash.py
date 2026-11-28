"""Fresh build and the hash proof run every solution the task declares (`[metadata] oracle_solutions`), not only
what solution/solve.sh does with SOLUTION unset. Fresh build passes only when every solution scores 1 after the
no-cache rebuild; the hash proof counts a damaged digest as used only when no solution still scores 1 with it.
Docker and Harbor are faked, so nothing is spent."""

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
from app.services import docker_orchestrator, harbor_runner, hash_checks, task_runner
from tests._fixtures import make_task


def task(tmp_path: Path, solutions=("1", "2")) -> Path:
    root = make_task(tmp_path / "t")
    if solutions:
        toml = (root / "task.toml").read_text()
        (root / "task.toml").write_text(toml.replace("[metadata]\n", f"[metadata]\noracle_solutions = {json.dumps(list(solutions))}\n", 1))
    return root


@pytest.fixture
def world(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(task_runner, "SessionLocal", factory)
    monkeypatch.setattr(task_runner.settings, "storage_dir", tmp_path / "storage")
    monkeypatch.setattr(docker_orchestrator, "build_image", lambda *a, **k: ("img:fresh", "built"))
    monkeypatch.setattr(docker_orchestrator, "remove_image", lambda tag: None)

    def add(root: Path) -> None:
        with factory() as db:
            db.add(Submission(id="s1", task_name="t", uploaded_at=datetime.now(UTC), zip_path="x", extracted_path=str(root)))
            db.commit()

    def logs(kind: str):
        with factory() as db:
            row = db.query(Run).filter_by(submission_id="s1", kind=kind).one()
            return row.status, json.loads(row.logs)

    return SimpleNamespace(add=add, logs=logs)


def fake_harbor(monkeypatch, rewards: dict):
    calls = []

    async def run_single(task_root, agent, jobs_dir, job_name, timeout_sec, agent_env=None, **kw):
        value = (agent_env or {}).get("SOLUTION")
        calls.append((value, kw.get("fresh_build", False)))
        reward = rewards[value]
        failed = [] if reward == 1.0 else ["test_x"]
        return SimpleNamespace(reward=reward, report={"ctrf": {"failed_names": failed}, "flags": []}, logs="log",
                               trial_dir=None)

    monkeypatch.setattr(harbor_runner, "run_single", run_single)
    return calls


# ---- fresh build ---------------------------------------------------------------------------------------

def test_fresh_build_runs_every_solution_and_passes_when_all_score_1(world, monkeypatch, tmp_path):
    world.add(task(tmp_path))
    calls = fake_harbor(monkeypatch, {"1": 1.0, "2": 1.0})
    asyncio.run(task_runner.run_fresh_build("s1"))
    status, doc = world.logs("fresh_build")
    assert calls == [("1", True), ("2", True)]
    assert status == "passed" and [o["solution"] for o in doc["oracles"]] == ["1", "2"]


def test_fresh_build_fails_when_one_solution_breaks(world, monkeypatch, tmp_path):
    world.add(task(tmp_path))
    fake_harbor(monkeypatch, {"1": 1.0, "2": 0.0})
    asyncio.run(task_runner.run_fresh_build("s1"))
    status, doc = world.logs("fresh_build")
    assert status == "failed"
    assert doc["oracle"]["solution"] == "2" and doc["oracle"]["failed_tests"] == ["test_x"]  # the one that broke


def test_fresh_build_without_declared_solutions_runs_once(world, monkeypatch, tmp_path):
    world.add(task(tmp_path, solutions=None))
    calls = fake_harbor(monkeypatch, {None: 1.0})
    asyncio.run(task_runner.run_fresh_build("s1"))
    status, doc = world.logs("fresh_build")
    assert calls == [(None, True)] and status == "passed" and doc["oracle"]["solution"] is None


# ---- hash proof ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("rewards, combined", [
    ({"1": 0.0, "2": 0.0}, 0.0),   # every solution caught the damage: the digest is used
    ({"1": 0.0, "2": 1.0}, 1.0),   # one solution still passes with a wrong digest: not used on its path
    ({"1": 0.0, "2": None}, None),  # one gave no reward: cannot be proven
])
def test_hash_proof_combines_every_solution(world, monkeypatch, tmp_path, rewards, combined):
    world.add(task(tmp_path))
    calls = fake_harbor(monkeypatch, rewards)
    seen = []

    async def fake_proof(task_root, run_oracle, work, progress=None, **kw):
        copy = Path(work) / "c"
        copy.mkdir(parents=True)
        seen.append(await run_oracle(copy, 1))
        return {"status": "passed", "verdict": "all_used", "entries": [], "runs": 1, "total": 0, "note": ""}

    monkeypatch.setattr(hash_checks, "run_proof", fake_proof)
    asyncio.run(task_runner.run_hash_proof_stage("s1"))
    assert [c[0] for c in calls] == ["1", "2"] and seen == [combined]
