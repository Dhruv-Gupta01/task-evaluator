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


# ---- in the submission, the evidence summary and the bundle ----------------------------------------

def _store(Session, status, payload):
    from datetime import UTC, datetime
    with Session() as db:
        db.add(Run(submission_id="s1", kind="worker_probe", run_index=0, status=status,
                   logs=json.dumps(payload), finished_at=datetime.now(UTC)))
        db.commit()


PASSED = {"status": "passed", "skipped": False, "note": "", "python": "/opt/verifier-venv/bin/python",
          "checks": [{"name": "unprivileged_uid", "ok": True, "detail": "uid 5, gid 5"},
                     {"name": "no_new_privs", "ok": True, "detail": "NoNewPrivs 1"}]}
FAILED = {"status": "failed", "skipped": False, "note": "",
          "checks": [{"name": "unprivileged_uid", "ok": False, "detail": "uid 0, gid 0"},
                     {"name": "no_new_privs", "ok": True, "detail": "NoNewPrivs 1"}]}


def test_the_submission_carries_the_probe_result(env):
    client, _, Session = env
    assert client.get("/submissions/s1").json()["worker_probe"]["status"] == "not-run"
    _store(Session, "passed", PASSED)
    body = client.get("/submissions/s1").json()["worker_probe"]
    assert body["status"] == "passed" and body["skipped"] is False
    assert [c["name"] for c in body["checks"]] == ["unprivileged_uid", "no_new_privs"]


def test_the_summary_says_not_run_passed_failed_or_skipped(env):
    from app.models import Submission
    from app.services import submission_service as ss

    def line():
        with env[2]() as db:
            return ss._worker_probe_line(ss.to_schema(db.get(Submission, "s1")))

    assert "not run" in line()
    _store(env[2], "passed", PASSED)
    assert "passed" in line() and "2 of 2" in line()
    with env[2]() as db:
        db.query(Run).delete(); db.commit()
    _store(env[2], "failed", FAILED)
    text = line()
    assert "FAILED" in text and "unprivileged_uid" in text and "1 of 2" in text
    with env[2]() as db:
        db.query(Run).delete(); db.commit()
    _store(env[2], "passed", {"status": "passed", "skipped": True, "note": "No Worker in this task's grader", "checks": []})
    assert "skipped" in line()


def test_the_evidence_bundle_has_the_probe(env):
    client, _, Session = env
    _store(Session, "passed", PASSED)
    bundle = client.get("/submissions/s1/evidence").json()
    assert bundle["worker_probe"]["status"] == "passed" and len(bundle["worker_probe"]["checks"]) == 2
    assert "Worker probe" in bundle["summary_markdown"]
