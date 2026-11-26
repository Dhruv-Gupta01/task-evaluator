"""The Grader Isolation gate through the API: run, schema, and blocking only when enforced."""
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Run, Submission
from app.routers import submissions as router
from tests.test_verifier_isolation_detection import _task
from tests.test_worker_required import RUN_AS_ROOT

QUIET = 'def test_x():\n    assert open("/app/out.txt").read() == "1"\n'


@pytest.fixture
def env(tmp_path, monkeypatch):
    task = _task(tmp_path / "t", RUN_AS_ROOT)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(Submission(id="s1", task_name="t", uploaded_at=datetime.now(UTC), zip_path="x",
                          extracted_path=str(task), build_status="passed", image_tag="img"))
        db.commit()

    async def no_queue(key, coro):
        coro.close()

    monkeypatch.setattr(router.task_queue, "submit", no_queue)
    app.dependency_overrides[get_db] = lambda: Session()
    yield TestClient(app), task, Session
    app.dependency_overrides.clear()


def test_the_gate_is_not_run_until_asked(env):
    client, _, _ = env
    assert client.get("/submissions/s1").json()["grader_isolation"]["status"] == "not-run"


def test_running_it_records_missing_and_does_not_block_by_default(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setattr(router.settings, "grader_isolation_enforce", False)
    body = client.post("/submissions/s1/grader-isolation").json()
    assert body["state"] == "missing" and body["status"] == "failed" and body["blocks"] is False
    assert "tests/test_x.py:4" in body["evidence"]
    assert client.post("/submissions/s1/agent-trials?n=1").status_code == 202  # advisory: not blocked


def test_when_enforced_it_blocks_agents_and_only_revising_the_task_clears_it(env, monkeypatch):
    client, task, _ = env
    monkeypatch.setattr(router.settings, "grader_isolation_enforce", True)
    blocked = client.post("/submissions/s1/agent-trials?n=1")
    assert blocked.status_code == 409 and "Grader Isolation is missing" in blocked.json()["detail"]
    for path in ("tier1", "tier2", "tiers", "cheat-trial"):
        assert client.post(f"/submissions/s1/{path}").status_code == 409, path
    (task / "tests" / "test_x.py").write_text(QUIET)
    assert client.post("/submissions/s1/grader-isolation").json()["status"] == "passed"
    assert client.post("/submissions/s1/agent-trials?n=1").status_code == 202


def test_a_clean_task_passes_and_is_never_blocked(env, monkeypatch):
    client, task, _ = env
    monkeypatch.setattr(router.settings, "grader_isolation_enforce", True)
    (task / "tests" / "test_x.py").write_text(QUIET)
    assert client.post("/submissions/s1/grader-isolation").json()["status"] == "passed"
    assert client.post("/submissions/s1/agent-trials?n=1").status_code == 202


def test_an_unvalidated_submission_gets_a_clear_error(env):
    client, _, Session = env
    with Session() as db:
        db.get(Submission, "s1").extracted_path = None
        db.commit()
    assert client.post("/submissions/s1/grader-isolation").status_code == 400
    assert client.post("/submissions/nope/grader-isolation").status_code == 404


def test_the_review_report_waits_for_an_enforced_gate_only(env, monkeypatch):
    from app.models import Submission as S
    from app.services import review_report
    client, _, Session = env
    with Session() as db:
        for kind in ("oracle", "nop", "sufficiency", "agent"):
            db.add(Run(submission_id="s1", kind=kind, run_index=0, status="passed", reward=1))
        sub = db.get(S, "s1"); sub.build_status = "passed"; db.commit()
        monkeypatch.setattr(review_report, "to_schema", lambda s: __import__("types").SimpleNamespace(
            build=_st("passed"), oracle=_st("passed"), nop=_st("passed"), sufficiency=_st("passed"), agent_trials=_st("passed"),
            grader_isolation=__import__("types").SimpleNamespace(enforced=False, status="not-run")))
        assert review_report.all_gates_terminal(sub)[0] is True
        monkeypatch.setattr(review_report, "to_schema", lambda s: __import__("types").SimpleNamespace(
            build=_st("passed"), oracle=_st("passed"), nop=_st("passed"), sufficiency=_st("passed"), agent_trials=_st("passed"),
            grader_isolation=__import__("types").SimpleNamespace(enforced=True, status="not-run")))
        ready, missing = review_report.all_gates_terminal(sub)
        assert ready is False and missing == ["grader_isolation"]


def _st(status):
    import types
    return types.SimpleNamespace(status=status)
