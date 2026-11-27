"""The Tier 0 -> 1 -> 2 cycle (task_runner.run_tier_cycle): the loop rules, with fake stages."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Run, Submission
from app.services import task_runner
from app.services.task_runner import run_tier_cycle


@pytest.fixture
def world(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(task_runner, "SessionLocal", factory)
    monkeypatch.setattr(task_runner.settings, "storage_dir", tmp_path / "storage")
    upload = tmp_path / "upload"
    upload.mkdir()
    (upload / "tests").mkdir()
    (upload / "tests" / "test.sh").write_text("v0\n")
    (upload / "task.toml").write_text("[task]\nname = \"x\"\n")
    with factory() as db:
        db.add(Submission(id="s1", task_name="t", uploaded_at=datetime.now(UTC), zip_path="x", extracted_path=str(upload)))
        db.commit()
    return type("W", (), {"factory": factory, "upload": upload, "calls": [], "history": []})


def _row(world, kind, status, logs):
    with world.factory() as db:
        row = task_runner._get_or_create_run(db, "s1", kind, 0)
        row.status, row.logs = status, json.dumps(logs)
        db.commit()


def install(world, monkeypatch, tier1, tier2):
    """tier1: per-round dicts {result, patched}; tier2: per-round {result}. Tier 1 'patches' by writing
    a hardened copy whose tests/test.sh names the round, as the real stage would."""
    t1, t2 = iter(tier1), iter(tier2)
    state = {"round": 0}

    async def fake0(sid, repeats, harbor, task_root=None):
        state["round"] += 1
        world.calls.append(("tier0", state["round"], task_root))
        _row(world, "tier0_probes", "passed", {"summary": {"exploited": 0}})

    async def fake1(sid, agent, model, effort, max_cost, task_root=None, earlier_rounds=None):
        spec = next(t1)
        world.calls.append(("tier1", state["round"], task_root, max_cost))
        world.history.append([r["round"] for r in earlier_rounds or []])
        hardened = task_runner.settings.storage_dir / "submissions" / sid / "tier1" / "hardened"
        base = task_root or world.upload
        import shutil

        if hardened.exists():
            shutil.rmtree(hardened)
        shutil.copytree(base, hardened)
        if spec.get("patched"):
            (hardened / "tests" / "test.sh").write_text(f"patched in round {state['round']}\n")
        fixes = [{"accepted": True}] * spec.get("patched", 0)
        _row(world, "tier1_loop", "passed" if spec["result"] == "robust" else "failed",
             {"status": spec["result"], "reason": "r", "attempts": [{"verdict": "attempted_failed"}], "fixes": fixes,
              "cost_usd": spec.get("cost", 1.0)})

    async def fake2(sid, agent, model, effort, max_cost, task_copy=None):
        spec = next(t2)
        world.calls.append(("tier2", state["round"], max_cost))
        _row(world, "tier2_loop", "passed" if spec["result"] == "robust" else "failed",
             {"status": spec["result"], "reason": "r", "attempts": [{"verdict": "honest_solve"}], "cost_usd": spec.get("cost", 1.0)})

    monkeypatch.setattr(task_runner, "run_tier0_stage", fake0)
    monkeypatch.setattr(task_runner, "run_tier1_stage", fake1)
    monkeypatch.setattr(task_runner, "run_tier2_stage", fake2)


def result(world):
    with world.factory() as db:
        row = db.query(Run).filter_by(submission_id="s1", kind="tier_cycle").one()
        return row.status, json.loads(row.logs)


def go(**kw):
    asyncio.run(run_tier_cycle("s1", **kw))


def test_a_patch_restarts_all_three_tiers_on_the_patched_copy(world, monkeypatch):
    install(world, monkeypatch, [{"result": "robust", "patched": 1}, {"result": "robust"}], [{"result": "robust"}])
    go()
    status, doc = result(world)
    assert status == "passed" and doc["status"] == "robust" and len(doc["rounds"]) == 2
    kinds = [(c[0], c[1]) for c in world.calls]
    # round 1 patched: no Tier 2 there; round 2 runs all three on the patched copy
    assert kinds == [("tier0", 1), ("tier1", 1), ("tier0", 2), ("tier1", 2), ("tier2", 2)]
    assert world.calls[0][2] is None
    patched_copy = world.calls[2][2]
    assert patched_copy is not None and (patched_copy / "tests" / "test.sh").read_text() == "patched in round 1\n"
    assert world.calls[3][2] == patched_copy
    assert (world.upload / "tests" / "test.sh").read_text() == "v0\n"  # the upload is never edited
    assert "patched in round 1" in doc["diff"]
    assert doc["rounds"][0]["tier2"] is None and doc["rounds"][1]["tier2"]["result"] == "robust"


def test_stops_not_converged_when_every_round_needs_a_patch(world, monkeypatch):
    install(world, monkeypatch, [{"result": "robust", "patched": 1}] * 3, [])
    go(max_rounds=3)
    status, doc = result(world)
    assert status == "failed" and doc["status"] == "not_converged" and len(doc["rounds"]) == 3
    assert not any(c[0] == "tier2" for c in world.calls)


def test_tier1_stopping_for_a_person_ends_the_cycle(world, monkeypatch):
    install(world, monkeypatch, [{"result": "unpatchable"}], [])
    go()
    _, doc = result(world)
    assert doc["status"] == "needs_review" and not any(c[0] == "tier2" for c in world.calls)


def test_tier2_hit_is_reported_and_nothing_is_patched(world, monkeypatch):
    install(world, monkeypatch, [{"result": "robust"}], [{"result": "exploited"}])
    go()
    _, doc = result(world)
    assert doc["status"] == "exploited" and len(doc["rounds"]) == 1


def test_a_tier2_that_ran_out_of_money_is_not_robust(world, monkeypatch):
    install(world, monkeypatch, [{"result": "robust"}], [{"result": "inconclusive"}])
    go()
    status, doc = result(world)
    assert status == "failed" and doc["status"] == "inconclusive" and "not proof" in doc["reason"]


def test_shared_cost_cap_is_split_across_rounds(world, monkeypatch):
    install(world, monkeypatch, [{"result": "robust", "patched": 1, "cost": 6.0}, {"result": "robust", "cost": 3.0}],
            [{"result": "robust", "cost": 1.0}])
    go(max_cost_usd=20.0)
    caps = [c[3] for c in world.calls if c[0] == "tier1"] + [c[2] for c in world.calls if c[0] == "tier2"]
    assert caps == [20.0, 14.0, 11.0]  # what was left when each stage started
    assert result(world)[1]["cost_usd"] == 10.0


def test_budget_stops_before_the_next_round(world, monkeypatch):
    install(world, monkeypatch, [{"result": "robust", "patched": 1, "cost": 9.0}], [])
    go(max_cost_usd=5.0)
    _, doc = result(world)
    assert doc["status"] == "budget" and len(doc["rounds"]) == 1


def test_a_crash_is_recorded_not_raised(world, monkeypatch):
    install(world, monkeypatch, [], [])  # tier1 script is empty: StopIteration inside the cycle
    go()
    status, doc = result(world)
    assert status == "failed" and doc["status"] == "error" and doc.get("error")


# The evidence summary states what the attackers actually did (honest solves, refusals, ...)
def _schema(tier0=None, tier1=None, tier2=None, cycle=None):
    from types import SimpleNamespace as NS

    def stage(doc):
        return NS(status="not-run" if doc is None else "failed", logs=None if doc is None else json.dumps(doc))

    return NS(tier0=stage(tier0), tier1=stage(tier1), tier2=stage(tier2), tier_cycle=stage(cycle))


def test_summary_states_honest_solves_and_refusals():
    from app.services import submission_service as ss

    attempts = [{"verdict": v} for v in ("honest_solve", "honest_solve", "refused", "attempted_failed", "not_attempted")]
    text = "\n".join(ss.cheat_evidence_lines(_schema(tier2={"attempts": attempts})))
    assert "2 solved the task honestly" in text and "1 refused" in text and "1 tried a shortcut and failed" in text
    assert "1 barely tried" in text
    assert "not evidence that the grader resisted a hack" in text
    assert "never added together" in text


def test_summary_says_when_nothing_really_tried_or_nothing_ran():
    from app.services import submission_service as ss

    only_honest = "\n".join(ss.cheat_evidence_lines(_schema(tier1={"attempts": [{"verdict": "honest_solve"}] * 3})))
    assert "No attempt really tried a shortcut, so this tier did not test the grader" in only_honest
    nothing = "\n".join(ss.cheat_evidence_lines(_schema()))
    assert nothing.count("not run yet") == 3


def test_summary_reports_the_loop_and_a_skipped_tier0():
    from app.services import submission_service as ss

    cycle = {"status": "robust", "reason": "no patch was needed", "rounds": [{"patched": 2}, {"patched": 0}]}
    text = "\n".join(ss.cheat_evidence_lines(_schema(tier0={"skipped": "shared verifier"}, cycle=cycle)))
    assert "skipped (shared verifier), so it is not evidence" in text
    assert "2 round(s), 2 patch(es) accepted by Tier 1 (the upload was never edited), ended: robust" in text


def test_tier0_replay_of_an_older_version_is_not_blamed_on_the_harness(monkeypatch):
    """After a patch, the upload's own stored rollouts were graded by the old verifier: a different
    reward is LOOSENED/REGRESSION, not 'the replica disagrees with Harbor'."""
    from types import SimpleNamespace as NS

    from app.services.tier0 import docker_runner, replay, runner

    monkeypatch.setattr(replay, "recorded_reward", lambda bundle: 1.0)
    monkeypatch.setattr(docker_runner, "run_replay", lambda spec, bundle, platform=None: NS(reward=0.0, notes=[]))
    bundle = NS(key="trial-1")
    opts = runner.Options(platform=None, sanitize=True, repeats=1)
    same = runner.run_replays(None, [(bundle, "agent", "this upload")], opts)
    patched = runner.run_replays(None, [(bundle, "agent", "earlier version (before the patch)")], opts)
    assert same[0]["verdict"] == "harness_disagrees"
    assert patched[0]["verdict"] == "REGRESSION"


def test_no_round_limit_by_default_and_each_round_gets_the_earlier_rounds(world, monkeypatch):
    # seven rounds in a row find and patch a hack; with no limit the cycle keeps going until one is clean
    install(world, monkeypatch, [{"result": "patched", "patched": 1}] * 7 + [{"result": "robust"}], [{"result": "robust"}])
    go()
    status, doc = result(world)
    assert status == "passed" and doc["status"] == "robust" and len(doc["rounds"]) == 8
    assert world.history == [list(range(1, n)) for n in range(1, 9)]  # round n's Tier 1 is told about rounds 1..n-1
