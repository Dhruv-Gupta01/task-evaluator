"""Failure Analysis runs one `harbor analyze` per trial folder, each with its own limit (ANALYZE_TRIAL_TIMEOUT_SEC),
at most two at a time. Before, one call per job meant a single hung evaluator ran the whole job to
ANALYZE_TIMEOUT_SEC and threw away the trials that had finished (found live on 2026-11-20). Harbor is faked here,
so nothing is spent."""
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
from app.services.harbor_runner import AnalyzeJobResult


@pytest.fixture
def world(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(task_runner, "SessionLocal", factory)
    monkeypatch.setattr(task_runner.settings, "storage_dir", tmp_path / "storage")
    monkeypatch.setattr(task_runner.settings, "analyze_timeout_sec", 3600)
    monkeypatch.setattr(task_runner.settings, "analyze_trial_timeout_sec", 900)
    with factory() as db:
        db.add(Submission(id="s1", task_name="t", uploaded_at=datetime.now(UTC), zip_path="x", extracted_path=None))
        db.commit()
    agent_root = tmp_path / "storage" / "submissions" / "s1" / "runs" / "agent" / "harbor"
    job = agent_root / "agent-job1"
    for name in ("t__a", "t__b", "t__c"):
        (job / name).mkdir(parents=True)
        (job / name / "result.json").write_text("{}")
    (job / "not-a-trial").mkdir()  # no result.json: not analysed

    async def no_model(*a, **k):
        return None

    monkeypatch.setattr(task_runner, "_classify_failures", no_model)
    monkeypatch.setattr(task_runner, "_audit_trials", no_model)
    return SimpleNamespace(factory=factory, job=job, agent_root=agent_root)


def logs(world) -> dict:
    with world.factory() as db:
        return json.loads(db.query(Run).filter_by(submission_id="s1", kind="failure_analysis").one().logs)


def fake_analyze(calls: list, hang: str | None = None, state: dict | None = None):
    async def run(target, out_dir, job_name, agent, model, timeout_sec, **kwargs):
        if state is not None:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.01)
        if state is not None:
            state["now"] -= 1
        calls.append((Path(target).name, timeout_sec))
        if hang and Path(target).name == hang:
            return AnalyzeJobResult(report=None, cost_usd=None, error=f"timed out after {timeout_sec:.0f}s")
        return AnalyzeJobResult(report={"results": [{"trial_name": Path(target).name}]}, cost_usd=0.01)
    return run


def test_each_trial_folder_gets_its_own_call_and_limit(world, monkeypatch):
    calls: list = []
    monkeypatch.setattr(harbor_runner, "run_analyze", fake_analyze(calls))
    asyncio.run(task_runner.run_failure_analysis("s1", "default"))
    assert sorted(calls) == [("t__a", 900), ("t__b", 900), ("t__c", 900)]  # not the job folder, not not-a-trial
    doc = logs(world)
    assert sorted(r["trial_name"] for r in doc["results"]) == ["t__a", "t__b", "t__c"]
    assert doc["cost_usd"] == pytest.approx(0.03)


def test_one_hung_trial_keeps_the_other_trials_results(world, monkeypatch):
    calls: list = []
    monkeypatch.setattr(harbor_runner, "run_analyze", fake_analyze(calls, hang="t__b"))
    asyncio.run(task_runner.run_failure_analysis("s1", "default"))
    doc = logs(world)
    assert sorted(r["trial_name"] for r in doc["results"]) == ["t__a", "t__c"]
    assert "t__b" in doc["error"] and "timed out" in doc["error"]
    assert "t__a" not in doc["error"]


def test_at_most_two_analyses_run_at_once(world, monkeypatch):
    for name in ("t__d", "t__e"):
        (world.job / name).mkdir()
        (world.job / name / "result.json").write_text("{}")
    calls: list = []
    state = {"now": 0, "peak": 0}
    monkeypatch.setattr(harbor_runner, "run_analyze", fake_analyze(calls, state=state))
    asyncio.run(task_runner.run_failure_analysis("s1", "default"))
    assert len(calls) == 5 and state["peak"] == 2


def test_the_trial_limit_never_exceeds_the_whole_analysis_limit(world, monkeypatch):
    monkeypatch.setattr(task_runner.settings, "analyze_timeout_sec", 300)
    calls: list = []
    monkeypatch.setattr(harbor_runner, "run_analyze", fake_analyze(calls))
    asyncio.run(task_runner.run_failure_analysis("s1", "default"))
    assert {limit for _, limit in calls} == {300}


def test_a_job_folder_without_trial_folders_is_still_analysed_whole(world, monkeypatch):
    bare = world.agent_root / "agent-job2"
    bare.mkdir()
    calls: list = []
    monkeypatch.setattr(harbor_runner, "run_analyze", fake_analyze(calls))
    asyncio.run(task_runner.run_failure_analysis("s1", "default"))
    assert ("agent-job2", 300) in calls or ("agent-job2", 3600) in calls
