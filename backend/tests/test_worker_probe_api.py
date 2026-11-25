"""The Worker probe through the API (the Docker part is covered by a live run, not here)."""
import asyncio
import json

from app.models import Run
from app.services import task_runner
from tests.test_grader_isolation_api import env  # noqa: F401  (fixture)


def test_not_run_until_asked(env):
    client, _, _ = env
    assert client.get("/submissions/s1/worker-probe").json()["status"] == "not-run"


def test_asking_marks_it_pending_and_queues_it(env):
    client, _, Session = env
    assert client.post("/submissions/s1/worker-probe").json() == {"status": "started"}
    with Session() as db:
        assert db.query(Run).filter_by(submission_id="s1", kind="worker_probe").one().status == "pending"


def test_an_unknown_submission_is_a_404(env):
    client, _, _ = env
    assert client.post("/submissions/nope/worker-probe").status_code == 404


def test_the_stage_stores_a_skip_for_a_task_that_has_no_worker(env, monkeypatch):
    client, _, Session = env
    monkeypatch.setattr(task_runner, "SessionLocal", Session)
    asyncio.run(task_runner.run_worker_probe_stage("s1"))  # the fixture task runs agent code with no Worker
    body = client.get("/submissions/s1/worker-probe").json()
    assert body["status"] == "passed" and body["skipped"] is True and "Worker" in body["note"]
    with Session() as db:
        assert json.loads(db.query(Run).filter_by(kind="worker_probe").one().logs)["skipped"] is True
