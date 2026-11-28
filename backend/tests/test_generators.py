"""Item 17 (from the post-fixes branch, batch 8): Idea review and the agent rubric, with a fake model.

Idea review sends a task idea to the judge model with Terminal-Bench's own proposal prompt, its task guidelines and
the list of accepted tasks. The agent rubric reads every agent trial's full transcript (the same condensed transcript
the Tier judges read, every step kept, no overall cap) and keeps only items whose quotes are found word for word in
the trial they name. The deliverable draft was not ported: the evidence summary and the task-deliverable skill cover it."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Run, Submission
from app.services import generators, judge_guard, task_runner


class FakeClient:
    model = "fake-model"

    def __init__(self, text):
        self.text, self.seen = text, None

    async def complete(self, messages, tools):
        self.seen = messages
        return SimpleNamespace(text=self.text, usage=None)


# ---- idea review ---------------------------------------------------------------------------------------

def test_idea_review_uses_the_vendored_proposal_prompt():
    client = FakeClient("Verdict: promising.")
    out = asyncio.run(generators.idea_review("Recover a ledger from a corrupted WAL archive.", client))
    assert out["review"] == "Verdict: promising." and out["model"] == "fake-model"
    system = client.seen[0].content
    assert generators.GUIDELINES_PLACEHOLDER not in system
    assert generators.GUIDELINES.read_text(encoding="utf-8").strip() in system
    assert system.startswith("# About Terminal Bench")
    assert "# Existing Terminal Bench tasks" in system and "- atrx-vep-crispr (" in system
    assert system.index("# Existing Terminal Bench tasks") < system.index("# Task proposal to evaluate")
    assert client.seen[1].content.endswith("Recover a ledger from a corrupted WAL archive.")


def test_the_vendored_files_are_the_post_fixes_copies():
    import subprocess
    root = Path(__file__).resolve().parents[2]
    for name in ("task-proposal.md", "task-guidelines.md", "existing-tasks.md"):
        try:
            theirs = subprocess.run(
                ["git", "show", f"origin/harbor-5.0-post-fixes-harbor-more-checks:backend/vendor/tb_prompts/{name}"],
                cwd=root, capture_output=True, check=True,
            ).stdout
        except (subprocess.CalledProcessError, FileNotFoundError):
            pytest.skip("the post-fixes branch is not available in this checkout")
        assert (generators.PROPOSAL_PROMPT.parent / name).read_bytes() == theirs, name


# ---- agent rubric --------------------------------------------------------------------------------------------

def test_rubric_keeps_only_verbatim_evidence():
    trajectories = {"t__a": "I ran pg_waldump on segment 0003 and saw the zeroed page.", "t__b": "gave up"}
    items = {"items": [
        {"criterion": "inspects WAL", "evidence": [{"trial": "t__a", "quote": "ran pg_waldump on segment 0003"}]},
        {"criterion": "made up", "evidence": [{"trial": "t__b", "quote": "verified every table checksum"}]},
        {"criterion": "wrong trial", "evidence": [{"trial": "t__b", "quote": "ran pg_waldump on segment 0003"}]},
    ]}
    out = asyncio.run(generators.agent_rubric("Recover it.", trajectories, FakeClient(json.dumps(items))))
    assert [i["criterion"] for i in out["items"]] == ["inspects WAL"]
    assert sorted(d["criterion"] for d in out["dropped"]) == ["made up", "wrong trial"]


def test_rubric_sends_every_transcript_whole_behind_the_evidence_guard():
    long = "step " * 30_000 + "THE END MARKER"
    client = FakeClient('{"items": []}')
    asyncio.run(generators.agent_rubric("Do it.", {"t__a": "FIRST MARKER " + long}, client))
    system, user = client.seen[0].content, client.seen[1].content
    assert judge_guard.EVIDENCE_GUARD in system
    assert "FIRST MARKER" in user and "THE END MARKER" in user  # nothing cut from either end


def test_rubric_without_trials_says_so():
    out = asyncio.run(generators.agent_rubric("x", {}, FakeClient("{}")))
    assert out["error"].startswith("no agent trajectories")


def test_a_model_answer_that_is_not_json_is_an_error_not_an_empty_rubric():
    out = asyncio.run(generators.agent_rubric("x", {"t__a": "text"}, FakeClient("sorry, I cannot")))
    assert out["items"] == [] and "no rubric items" in out["error"]


# ---- the transcripts and the endpoints ------------------------------------------------------------------------

def _trial(folder: Path, message: str) -> None:
    (folder / "agent").mkdir(parents=True)
    (folder / "result.json").write_text("{}")
    (folder / "agent" / "trajectory.json").write_text(json.dumps({"steps": [
        {"source": "user", "message": "the task"},
        {"source": "agent", "message": message, "tool_calls": [], "observation": {"results": []}},
    ]}))


def test_transcripts_come_from_every_agent_trial_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(task_runner.settings, "storage_dir", tmp_path)
    job = tmp_path / "submissions" / "s1" / "runs" / "agent" / "harbor" / "agent-job1"
    _trial(job / "t__a", "I read the spec first.")
    _trial(job / "t__b", "I wrote the parser.")
    (job / "not-a-trial").mkdir()
    got = task_runner.agent_transcripts("s1")
    assert sorted(got) == ["agent-job1/t__a", "agent-job1/t__b"]
    assert "I read the spec first." in got["agent-job1/t__a"]


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    monkeypatch.setattr(task_runner.settings, "storage_dir", tmp_path / "storage")
    root = tmp_path / "task"
    root.mkdir()
    (root / "instruction.md").write_text("Recover the ledger.\n")
    session.add(Submission(id="s1", task_name="t", uploaded_at=datetime.now(UTC), zip_path="x", extracted_path=str(root)))
    session.commit()
    job = tmp_path / "storage" / "submissions" / "s1" / "runs" / "agent" / "harbor" / "agent-job1"
    _trial(job / "t__a", "I ran pg_waldump on segment 0003 first.")
    return session


def test_the_rubric_endpoint_stores_its_result(db, monkeypatch):
    from app.routers import submissions as router

    answer = {"items": [{"criterion": "inspects WAL", "pass_when": "it reads the WAL", "weight": 2,
                         "evidence": [{"trial": "agent-job1/t__a", "quote": "ran pg_waldump on segment 0003"}]}]}
    monkeypatch.setattr(generators, "get_llm_client", lambda: FakeClient(json.dumps(answer)))
    monkeypatch.setattr(router.budget, "judge_exceeded_message", lambda db: None)
    out = asyncio.run(router.agent_rubric("s1", db))
    assert [i["criterion"] for i in out["items"]] == ["inspects WAL"]
    row = db.query(Run).filter_by(submission_id="s1", kind="agent_rubric").one()
    assert row.status == "passed" and json.loads(row.logs)["trials"] == ["agent-job1/t__a"]
    from app.services import submission_service
    assert submission_service.to_schema(db.get(Submission, "s1")).agent_rubric.status == "passed"


def test_the_idea_endpoint_checks_length_and_saves(db, monkeypatch, tmp_path):
    from app.routers import submissions as router

    monkeypatch.setattr(generators, "get_llm_client", lambda: FakeClient("Verdict: reject, a duplicate."))
    monkeypatch.setattr(router.budget, "judge_exceeded_message", lambda db: None)
    with pytest.raises(HTTPException) as e:
        asyncio.run(router.idea_review(router.IdeaBody(idea="too short"), db))
    assert e.value.status_code == 400
    out = asyncio.run(router.idea_review(router.IdeaBody(idea="Recover a ledger from a corrupted WAL archive."), db))
    saved = json.loads((tmp_path / "storage" / "idea_reviews" / f"{out['id']}.json").read_text())
    assert saved["review"] == "Verdict: reject, a duplicate." and saved["idea"].startswith("Recover")
    listed = router.list_idea_reviews()
    assert listed[0]["id"] == out["id"] and listed[0]["review"].startswith("Verdict")
