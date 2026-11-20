"""Batch 8: the LLM generators, with a fake client (the live run uses the real one)."""

import asyncio
import json
from types import SimpleNamespace

from app.services import generators


class FakeClient:
    model = "fake-model"

    def __init__(self, text):
        self.text, self.seen = text, None

    async def complete(self, messages, tools):
        self.seen = messages
        return SimpleNamespace(text=self.text)


def test_idea_review_uses_the_vendored_proposal_prompt():
    client = FakeClient("Verdict: promising.")
    out = asyncio.run(generators.idea_review("Recover a ledger from a corrupted WAL archive.", client))
    assert out["review"] == "Verdict: promising."
    system = client.seen[0].content
    assert generators.GUIDELINES_PLACEHOLDER not in system
    assert generators.GUIDELINES.read_text(encoding="utf-8").strip() in system
    assert system.startswith("# About Terminal Bench")
    assert client.seen[1].content.endswith("Recover a ledger from a corrupted WAL archive.")


def test_rubric_keeps_only_verbatim_evidence():
    trajectories = {"trial-0": "I ran pg_waldump on segment 0003 and saw the zeroed page.", "trial-1": "gave up"}
    items = {"items": [
        {"criterion": "inspects WAL", "evidence": [{"trial": "trial-0", "quote": "ran pg_waldump on segment 0003"}]},
        {"criterion": "made up", "evidence": [{"trial": "trial-1", "quote": "verified every table checksum"}]},
        {"criterion": "wrong trial", "evidence": [{"trial": "trial-1", "quote": "ran pg_waldump on segment 0003"}]},
    ]}
    out = asyncio.run(generators.agent_rubric("Recover it.", trajectories, FakeClient(json.dumps(items))))
    assert [i["criterion"] for i in out["items"]] == ["inspects WAL"]
    assert sorted(d["criterion"] for d in out["dropped"]) == ["made up", "wrong trial"]


def test_rubric_without_trials_says_so():
    out = asyncio.run(generators.agent_rubric("x", {}, FakeClient("{}")))
    assert out["error"].startswith("no agent trajectories")


def test_deliverable_always_ends_as_a_draft():
    out = asyncio.run(generators.deliverable({"task": "t"}, FakeClient("# Overview\nAll good.")))
    assert out["markdown"].endswith(generators.DRAFT_LINE)
    already = asyncio.run(generators.deliverable({"task": "t"}, FakeClient("text\n\n" + generators.DRAFT_LINE)))
    assert already["markdown"].count(generators.DRAFT_LINE) == 1
