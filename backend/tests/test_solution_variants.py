"""Correct alternative solutions: each run as its own Oracle trial with --ae SOLUTION=<value>."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Run, Submission
from app.services import flag_results, harbor_runner, solution_variants as sv, static_checks, submission_service as ss, task_runner
from app.services.harbor_runner import TrialOutcome
from app.services.task_config import TaskConfig
from tests.test_flags import make_task


def row(value, index, status="passed", reward=1):
    return NS(kind=sv.PREFIX + value, run_index=index, status=status, reward=reward, raw_reward=None if reward is None else float(reward))


# ---- the shared helpers -------------------------------------------------------------------------
def test_grouping_status_and_values():
    rows = [row("2", 1), row("1", 0), row("2", 0, reward=0, status="failed"), NS(kind="oracle", run_index=0, status="passed", reward=1)]
    groups = sv.grouped(rows)
    assert list(groups) == ["1", "2"] and [r.run_index for r in groups["2"]] == [0, 1]
    assert sv.group_status(groups["1"]) == "passed" and sv.group_status(groups["2"]) == "failed"
    assert sv.group_status([row("1", 0, "running", None), row("1", 1)]) == "running"
    assert sv.group_status([row("1", 0, "pending", None), row("1", 1)]) == "pending"
    assert sv.group_status([]) == "failed" and sv.value_of("oracle") is None and sv.value_of("solution_variant:a-b") == "a-b"
    assert sv.rewards_text(groups["2"]) == "0, 1"


# ---- Harbor: --ae, and the task's own [solution.env] -------------------------------------------
def test_prepare_task_drops_only_the_solution_env_key_the_platform_sets(tmp_path):
    root = tmp_path / "task"
    root.mkdir()
    (root / "task.toml").write_text('[task]\nname = "x"\n\n[solution.env]\nSOLUTION = "1"\nOTHER = "keep"\n')
    path, notes = harbor_runner._prepare_task(root, tmp_path / "copy", drop_solution_env=("SOLUTION",))
    new = (path / "task.toml").read_text()
    assert "SOLUTION" not in new and 'OTHER = "keep"' in new and "solution.env SOLUTION dropped" in (notes or "")
    assert 'SOLUTION = "1"' in (root / "task.toml").read_text()  # the extracted task is never edited


def test_a_task_that_does_not_set_it_is_left_alone(tmp_path):
    root = tmp_path / "task"
    root.mkdir()
    (root / "task.toml").write_text('[task]\nname = "x"\n')
    _, notes = harbor_runner._prepare_task(root, tmp_path / "copy", drop_solution_env=("SOLUTION",))
    assert "solution.env" not in (notes or "")


def test_run_job_passes_the_value_as_ae(tmp_path, monkeypatch):
    seen = {}

    async def fake_exec(*cmd, **kwargs):
        seen["cmd"] = list(cmd)
        raise FileNotFoundError("harbor")  # ends the job right after the command is built

    monkeypatch.setattr(harbor_runner.asyncio, "create_subprocess_exec", fake_exec)
    root = tmp_path / "task"
    root.mkdir()
    (root / "task.toml").write_text('[task]\nname = "x"\n')
    asyncio.run(harbor_runner.run_job(root, "oracle", tmp_path / "jobs", "j1", 60, agent_env={"SOLUTION": "2"}))
    cmd = seen["cmd"]
    assert cmd[cmd.index("--ae") + 1] == "SOLUTION=2"


# ---- the runner ---------------------------------------------------------------------------------
@pytest.fixture
def world(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(task_runner, "SessionLocal", factory)
    monkeypatch.setattr(task_runner.settings, "storage_dir", tmp_path / "storage")
    root = tmp_path / "up"
    make_task(root)
    with factory() as db:
        db.add(Submission(id="s1", task_name="t", uploaded_at=datetime.now(UTC), zip_path="x", extracted_path=str(root), task_checksum="canon"))
        db.commit()
    return NS(factory=factory, root=root, calls=[])


def add_pending(world, values, runs):
    with world.factory() as db:
        for v in values:
            for i in range(runs):
                db.add(Run(submission_id="s1", kind=sv.PREFIX + v, run_index=i, status="pending"))
        db.commit()


def test_each_solution_runs_as_its_own_oracle_trial_and_is_kept_out_of_the_gate(world, monkeypatch):
    add_pending(world, ["1", "2"], 2)
    rewards = {"1": [1.0, 1.0], "2": [1.0, 0.0]}

    async def fake_run_single(task_root, agent, jobs_dir, job_name, timeout, **kw):
        value = kw["agent_env"]["SOLUTION"]
        world.calls.append((agent, kw["agent_env"], kw["drop_solution_env"]))
        return TrialOutcome(reward=rewards[value].pop(0), logs="log", task_checksum="other")

    monkeypatch.setattr(harbor_runner, "run_single", fake_run_single)
    asyncio.run(task_runner.run_solution_variants("s1"))
    assert [c[0] for c in world.calls] == ["oracle"] * 4 and all(c[2] == ("SOLUTION",) for c in world.calls)
    with world.factory() as db:
        rows = db.query(Run).filter(Run.kind.startswith(sv.PREFIX, autoescape=True)).order_by(Run.kind, Run.run_index).all()
        assert [(r.kind[len(sv.PREFIX):], r.run_index, r.reward, r.status) for r in rows] == [
            ("1", 0, 1, "passed"), ("1", 1, 1, "passed"), ("2", 0, 1, "passed"), ("2", 1, 0, "failed")]
        assert db.query(Run).filter_by(kind="oracle").count() == 0  # never a Gate 1 run
        assert db.get(Submission, "s1").task_checksum == "canon"  # the task-version comparison is untouched
        assert task_runner.solution_variant_values(db, "s1") == ["1", "2"]


def test_a_crash_marks_the_unfinished_rows_failed(world, monkeypatch):
    add_pending(world, ["1"], 2)

    async def boom(*a, **k):
        raise RuntimeError("docker is down")

    monkeypatch.setattr(harbor_runner, "run_single", boom)
    asyncio.run(task_runner.run_solution_variants("s1"))
    with world.factory() as db:
        assert {r.status for r in db.query(Run).filter(Run.kind.startswith(sv.PREFIX, autoescape=True))} == {"failed"}


# ---- Tier 1's patch check ----------------------------------------------------------------------
def gate(monkeypatch, tmp_path, rewards, variants):
    calls = []

    async def fake(task_root, agent, jobs_dir, job_name, timeout, **kw):
        key = (kw.get("agent_env") or {}).get("SOLUTION", "default")
        calls.append(key)
        return TrialOutcome(reward=rewards[key], logs="E   AssertionError: expected 3")

    monkeypatch.setattr(harbor_runner, "run_single", fake)
    ok, why = asyncio.run(task_runner._gate_candidate(tmp_path / "c", tmp_path / "gate", TaskConfig(), variants))
    return ok, why, calls


def test_the_patch_check_needs_every_correct_alternative_to_still_pass(monkeypatch, tmp_path):
    ok, why, calls = gate(monkeypatch, tmp_path, {"default": 1.0, "2": 1.0, "3": 1.0}, ["2", "3"])
    assert ok and calls == ["default", "2", "3"] and "2 correct alternative solution(s) (2, 3) also score 1" in why
    ok, why, calls = gate(monkeypatch, tmp_path, {"default": 1.0, "2": 1.0, "3": 0.0}, ["2", "3"])
    assert not ok and "SOLUTION=3" in why and "rejects a valid solution" in why and "AssertionError" in why


def test_the_patch_check_without_variants_is_the_old_single_oracle(monkeypatch, tmp_path):
    ok, why, calls = gate(monkeypatch, tmp_path, {"default": 1.0}, [])
    assert ok and calls == ["default"] and why == "Oracle reward 1 on the patched task"
    ok, why, calls = gate(monkeypatch, tmp_path, {"default": 0.0, "2": 1.0}, ["2"])
    assert not ok and calls == ["default"]  # the alternatives are not tried when the default already fails
    ok, why, _ = gate(monkeypatch, tmp_path, {"default": None}, [])
    assert not ok and "no reward" in why


# ---- the result, the evidence, the Flag, the hint ----------------------------------------------
def test_the_result_and_the_evidence_lines():
    sub = NS(runs=[row("1", 0), row("1", 1), row("2", 0), row("2", 1, "failed", 0)])
    result = ss._solution_variants_result(sub)
    assert result.status == "failed" and [(v.value, v.status) for v in result.variants] == [("1", "passed"), ("2", "failed")]
    text = "\n".join(ss.solution_variant_lines(NS(solution_variants=result)))
    assert "SOLUTION=1: all 2 run(s) scored 1" in text and "SOLUTION=2: 1 of 2 run(s) scored 1 (rewards 1, 0)" in text
    assert "Nop and the Weak-answer probe" in text
    assert ss._solution_variants_result(NS(runs=[])).status == "not-run"
    assert ss.solution_variant_lines(NS(solution_variants=ss._solution_variants_result(NS(runs=[])))) == []


def test_flag_15_38_fires_only_for_a_rejected_correct_solution():
    bad = flag_results.RESULT_RULES["15.38"]([row("1", 0), row("2", 0), row("2", 1, "failed", 0)])
    assert len(bad.findings) == 1 and bad.findings[0].evidence == ["SOLUTION=2: rewards 1, 0"]
    assert flag_results.RESULT_RULES["15.38"]([row("1", 0), row("1", 1)]).findings == []
    none = flag_results.RESULT_RULES["15.38"]([])
    assert none.findings == [] and none.note == "no solution variants run yet"
    unfinished = flag_results.RESULT_RULES["15.38"]([row("2", 0, "pending", None)])
    assert unfinished.findings == []


def test_the_detection_hint(tmp_path):
    class R:
        def __init__(self):
            self.items = []

        def add(self, n, s, m):
            self.items.append((n, s, m))

    with_var = write_solution(tmp_path / "a", 'case "${SOLUTION:-1}" in\n 1) bash a.sh;;\nesac\n')
    r = R()
    from app.services import hygiene_checks
    hygiene_checks.check_solution_variants_hint(with_var, r)
    assert r.items and r.items[0][1] == "info" and "Solution variants card" in r.items[0][2]
    r2 = R()
    hygiene_checks.check_solution_variants_hint(write_solution(tmp_path / "b", "echo hello\n"), r2)
    assert r2.items == []


def write_solution(root, text):
    (root / "solution").mkdir(parents=True)
    (root / "solution" / "solve.sh").write_text(text)
    return root


# ---- the endpoint -------------------------------------------------------------------------------
def test_the_endpoint_validates_and_replaces_the_earlier_request(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app
    from app.routers import submissions as router

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(Submission(id="s1", task_name="t", uploaded_at=datetime.now(UTC), zip_path="x", extracted_path=str(tmp_path), build_status="passed"))
        db.commit()

    async def no_queue(key, coro):
        coro.close()

    monkeypatch.setattr(router.task_queue, "submit", no_queue)
    monkeypatch.setattr(router, "_require_built_submission", lambda sid, db: None)
    app.dependency_overrides[get_db] = lambda: Session()
    try:
        client = TestClient(app)
        assert client.post("/submissions/s1/solution-variants?values=1,2").status_code == 400  # no passing Oracle yet
        with Session() as db:
            db.add(Run(submission_id="s1", kind="oracle", run_index=0, status="passed", reward=1))
            db.commit()
        for query in ("values=", "values=a%20b", "values=1&runs=0", "values=1&runs=6", "values=" + ",".join(str(i) for i in range(11))):
            assert client.post(f"/submissions/s1/solution-variants?{query}").status_code == 400, query
        assert client.post("/submissions/s1/solution-variants?values=1,2,2&runs=2").status_code == 202
        kinds = sorted((r.kind, r.run_index, r.status) for r in Session().query(Run).filter(Run.kind.startswith(sv.PREFIX)))
        assert kinds == [("solution_variant:1", 0, "pending"), ("solution_variant:1", 1, "pending"),
                         ("solution_variant:2", 0, "pending"), ("solution_variant:2", 1, "pending")]
        assert client.post("/submissions/s1/solution-variants?values=3&runs=1").status_code == 202
        kinds = [r.kind for r in Session().query(Run).filter(Run.kind.startswith(sv.PREFIX))]
        assert kinds == ["solution_variant:3"]  # the new request replaced the old rows
        assert Session().query(Run).filter_by(kind="oracle").count() == 1  # the Gate 1 rows are untouched
    finally:
        app.dependency_overrides.clear()
