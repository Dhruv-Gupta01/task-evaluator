"""The hash proof through the API, with Harbor replaced by a pretend Oracle."""
import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

from app.models import Run
from app.services import task_runner
from tests.test_grader_isolation_api import env  # noqa: F401  (fixture)


def oracle_passed(Session):
    with Session() as db:
        db.add(Run(submission_id="s1", kind="oracle", run_index=0, status="passed", reward=1, finished_at=datetime.now(UTC)))
        db.commit()


def test_not_run_until_asked_and_refused_before_the_oracle_passes(env):
    client, _, _ = env
    assert client.get("/submissions/s1").json()["hash_proof"]["status"] == "not-run"
    assert client.post("/submissions/s1/hash-proof").status_code == 400


def test_asking_after_a_passing_oracle_queues_it(env):
    client, _, Session = env
    oracle_passed(Session)
    assert client.post("/submissions/s1/hash-proof").json() == {"status": "started"}
    assert client.get("/submissions/s1").json()["hash_proof"]["status"] == "pending"


def test_the_stage_stores_each_entry_and_the_summary_names_the_unused_one(env, monkeypatch):
    client, task, Session = env
    oracle_passed(Session)
    (task / "tests" / "expected.json").write_text(json.dumps({"a_sha256": "a" * 64, "decoy_sha256": "b" * 64}))
    monkeypatch.setattr(task_runner, "SessionLocal", Session)
    monkeypatch.setattr(task_runner, "_get_task_config", lambda s: None)
    monkeypatch.setattr(task_runner, "_oracle_timeout_sec", lambda c: 10)

    async def pretend(copy_root, agent, jobs_dir, job_name, timeout_sec, **kw):
        stored = json.loads((copy_root / "tests" / "expected.json").read_text())
        return SimpleNamespace(reward=float(stored["a_sha256"] == "a" * 64))

    monkeypatch.setattr(task_runner.harbor_runner, "run_single", pretend)
    asyncio.run(task_runner.run_hash_proof_stage("s1"))

    proof = client.get("/submissions/s1").json()["hash_proof"]
    assert proof["status"] == "failed" and proof["verdict"] == "unused_hash" and proof["total"] == 2
    assert {e["name"]: e["result"] for e in proof["entries"]} == {"a_sha256": "used", "decoy_sha256": "not_used"}
    bundle = client.get("/submissions/s1/evidence").json()
    assert bundle["hash_proof"]["verdict"] == "unused_hash"
    assert "decoy_sha256 (not_used)" in bundle["summary_markdown"]
