"""Evidence sets (from the post-fixes branch, batch 6) and rule 15.36.

The standard set is 3 trials of the main agent (HARBOR_AGENT with its .env model) followed by Failure Analysis on
the TB rubric; a second model family (SECOND_AGENT / SECOND_AGENT_MODEL, 3 more trials) is optional and off unless
asked for (`second=true`). The cheap set is 2 trials of CHEAP_AGENT / CHEAP_AGENT_MODEL. Trials are appended to the
existing ones. Rule 15.36 warns when the cheap model solved the task, and the per-model breakdown keeps a pass rate
made of several models readable. Harbor and the agents are faked, so nothing is spent."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import Base
from app.models import Run, Submission
from app.services import result_rules as rr
from app.services import task_runner


@pytest.fixture
def agents(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "harbor_agent", "claude-code")
    monkeypatch.setattr(s, "second_agent", "codex")
    monkeypatch.setattr(s, "second_agent_model", "openai/gpt-6-luna")
    monkeypatch.setattr(s, "cheap_agent", "claude-code")
    monkeypatch.setattr(s, "cheap_agent_model", "anthropic/claude-haiku-5-5")
    return s


# ---- which trials a set runs ---------------------------------------------------------------------------

def test_the_standard_set_is_the_main_agent_only_unless_the_second_family_is_asked_for(agents):
    assert task_runner.evidence_pairs("standard") == [{"agent": "claude-code", "model": None, "n": 3}]
    assert task_runner.evidence_pairs("standard", second=True) == [
        {"agent": "claude-code", "model": None, "n": 3},
        {"agent": "codex", "model": "openai/gpt-6-luna", "n": 3},
    ]


def test_the_pickers_choose_both_families(agents):
    pairs = task_runner.evidence_pairs("standard", second=True, agent="codex", model="openai/gpt-6-astra",
                                       second_agent="claude-code", second_model="anthropic/claude-sonnet-5-5")
    assert pairs == [
        {"agent": "codex", "model": "openai/gpt-6-astra", "n": 3},
        {"agent": "claude-code", "model": "anthropic/claude-sonnet-5-5", "n": 3},
    ]


def test_a_second_agent_picked_without_a_model_uses_that_agents_own_default(agents):
    # SECOND_AGENT_MODEL belongs to SECOND_AGENT (codex, an openai model), so it must not go to claude-code
    pairs = task_runner.evidence_pairs("standard", second=True, second_agent="claude-code")
    assert pairs[1] == {"agent": "claude-code", "model": None, "n": 3}
    assert task_runner.evidence_pairs("standard", second=True, second_agent="codex")[1]["model"] == "openai/gpt-6-luna"


def test_the_cheap_set_ignores_the_pickers(agents):
    assert task_runner.evidence_pairs("cheap", agent="codex", model="openai/gpt-6-astra") == [
        {"agent": "claude-code", "model": "anthropic/claude-haiku-5-5", "n": 2}
    ]


def test_no_second_agent_set_means_no_second_pair(agents, monkeypatch):
    monkeypatch.setattr(agents, "second_agent", "")
    assert len(task_runner.evidence_pairs("standard", second=True)) == 1


def test_the_cheap_set(agents):
    assert task_runner.evidence_pairs("cheap") == [
        {"agent": "claude-code", "model": "anthropic/claude-haiku-5-5", "n": 2}
    ]


# ---- running a set ----------------------------------------------------------------------------------------

@pytest.fixture
def world(tmp_path, monkeypatch, agents):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(task_runner, "SessionLocal", factory)
    with factory() as db:
        db.add(Submission(id="s1", task_name="t", uploaded_at=datetime.now(UTC), zip_path="x", extracted_path=str(tmp_path)))
        db.add(Run(submission_id="s1", kind="agent", run_index=0, status="passed", reward=1))  # an earlier trial
        db.commit()
    calls = SimpleNamespace(trials=[], analysis=[])

    async def fake_trials(submission_id, n, append=False, reasoning_effort=None, agent=None, max_cost_usd=None, model=None):
        with factory() as db:
            pending = db.query(Run).filter_by(submission_id=submission_id, kind="agent", status="pending").all()
            calls.trials.append({"n": n, "append": append, "agent": agent, "model": model, "cap": max_cost_usd,
                                 "pending": sorted(r.run_index for r in pending)})
            for r in pending:
                r.status, r.reward = "failed", 0
            db.commit()

    async def fake_analysis(submission_id, rubric="default"):
        calls.analysis.append(rubric)

    monkeypatch.setattr(task_runner, "run_agent_trials", fake_trials)
    monkeypatch.setattr(task_runner, "run_failure_analysis", fake_analysis)
    monkeypatch.setattr(task_runner, "_agent_config_error", lambda agent, model=None: None)

    def state():
        with factory() as db:
            row = db.query(Run).filter_by(submission_id="s1", kind="evidence_runs").one()
            return row.status, json.loads(row.logs)

    return SimpleNamespace(calls=calls, state=state, factory=factory)


def test_standard_set_appends_three_trials_then_failure_analysis(world):
    asyncio.run(task_runner.run_evidence_set("s1", "standard"))
    assert world.calls.trials == [
        {"n": 3, "append": True, "agent": "claude-code", "model": None, "cap": None, "pending": [1, 2, 3]}
    ]
    assert world.calls.analysis == ["tb"]
    status, doc = world.state()
    assert status == "passed" and doc["set"] == "standard" and doc["second"] is False
    assert [p["status"] for p in doc["pairs"]] == ["done"]


def test_standard_set_with_the_second_family_splits_the_cap(world):
    asyncio.run(task_runner.run_evidence_set("s1", "standard", max_cost_usd=10.0, second=True))
    assert [(c["agent"], c["cap"], c["pending"]) for c in world.calls.trials] == [
        ("claude-code", 5.0, [1, 2, 3]), ("codex", 5.0, [4, 5, 6]),
    ]
    assert world.calls.analysis == ["tb"]


def test_cheap_set_runs_two_trials_and_no_analysis(world):
    asyncio.run(task_runner.run_evidence_set("s1", "cheap"))
    assert [(c["n"], c["model"]) for c in world.calls.trials] == [(2, "anthropic/claude-haiku-5-5")]
    assert world.calls.analysis == []


def test_a_pair_that_is_not_set_up_is_skipped_and_fails_the_set(world, monkeypatch):
    monkeypatch.setattr(task_runner, "_agent_config_error",
                        lambda agent, model=None: "needs OPENAI_API_KEY" if agent == "codex" else None)
    asyncio.run(task_runner.run_evidence_set("s1", "standard", second=True))
    assert [c["agent"] for c in world.calls.trials] == ["claude-code"]
    status, doc = world.state()
    assert status == "failed" and doc["pairs"][1]["status"] == "not run: needs OPENAI_API_KEY"
    assert world.calls.analysis == ["tb"]  # the trials that ran are still analysed


def test_the_endpoint_checks_its_arguments(monkeypatch):
    from app.routers import submissions as router

    with pytest.raises(HTTPException) as e:
        asyncio.run(router.trigger_evidence_set("s1", which="huge", db=None))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        asyncio.run(router.trigger_evidence_set("s1", which="cheap", max_cost_usd=-1, db=None))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        asyncio.run(router.trigger_evidence_set("s1", which="standard", second_agent="gpt", db=None))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:  # a codex run takes openai/* models only
        asyncio.run(router.trigger_evidence_set("s1", which="standard", second_agent="codex",
                                                second_model="anthropic/claude-haiku-5-5", db=None))
    assert e.value.status_code == 400


def test_the_endpoint_passes_second_through(monkeypatch, agents):
    from app.routers import submissions as router

    for helper in ("_require_built_submission", "_require_oracle_passed", "_require_grader_isolation", "_raise_if_over_budget"):
        monkeypatch.setattr(router, helper, lambda *a, **k: None)
    monkeypatch.setattr(router.budget, "exceeded_message", lambda db: None)
    seen = {}

    async def fake_submit(key, coro):
        seen["key"] = key
        coro.close()

    monkeypatch.setattr(router.task_queue, "submit", fake_submit)
    out = asyncio.run(router.trigger_evidence_set("s1", which="standard", second=True, agent="claude-code",
                                                  second_agent="codex", second_model="openai/gpt-6-astra", db=None))
    assert seen["key"] == "s1:agent"
    assert json.loads(out["pairs"]) == [{"agent": "claude-code", "model": None, "n": 3},
                                        {"agent": "codex", "model": "openai/gpt-6-astra", "n": 3}]


# ---- the per-model breakdown and 15.36 -----------------------------------------------------------------------

def trial(i, reward, agent, model):
    return SimpleNamespace(kind="agent", run_index=i, status="passed" if reward == 1 else "failed", reward=reward,
                           logs=f"(exact command: harbor run --path /x --agent {agent} --jobs-dir /y --model {model} -n 1)",
                           verifier_json=None, started_at=None, finished_at=None)


def test_a_trials_agent_and_model_come_from_its_command():
    assert rr.trial_agent_model(trial(0, 1, "codex", "openai/gpt-6-luna")) == ("codex", "openai/gpt-6-luna")
    assert rr.trial_agent_model(SimpleNamespace(logs="no command here")) == (None, None)


def test_the_breakdown_groups_finished_trials_by_agent_and_model():
    runs = [trial(0, 1, "claude-code", "anthropic/claude-fable-5-1"), trial(1, 0, "claude-code", "anthropic/claude-fable-5-1"),
            trial(2, 0, "codex", "openai/gpt-6-luna"),
            SimpleNamespace(**{**vars(trial(3, None, "codex", "openai/gpt-6-luna")), "status": "running"})]
    assert rr.model_breakdown(runs) == [
        {"agent": "claude-code", "model": "anthropic/claude-fable-5-1", "passed": 1, "n": 2},
        {"agent": "codex", "model": "openai/gpt-6-luna", "passed": 0, "n": 1},
    ]


def test_15_36_warns_when_the_cheap_model_solved_it(agents):
    cheap = "anthropic/claude-haiku-5-5"
    lines = rr.rule_cheap_model([trial(0, 1, "claude-code", cheap), trial(1, 0, "claude-code", cheap)])
    assert len(lines) == 1 and lines[0].startswith("15.36:") and "1 of 2 passed" in lines[0]
    assert rr.rule_cheap_model([trial(0, 0, "claude-code", cheap)]) == []
    assert rr.rule_cheap_model([trial(0, 1, "claude-code", "anthropic/claude-fable-5-1")]) == []


def test_the_schema_carries_the_breakdown_the_set_and_15_36(agents, tmp_path):
    from app.services import submission_service

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Submission(id="s1", task_name="t", uploaded_at=datetime.now(UTC), zip_path="x", extracted_path=None))
    for t in (trial(0, 1, "claude-code", "anthropic/claude-fable-5-1"), trial(1, 1, "claude-code", "anthropic/claude-haiku-5-5")):
        db.add(Run(submission_id="s1", kind="agent", run_index=t.run_index, status=t.status, reward=t.reward, logs=t.logs))
    db.add(Run(submission_id="s1", kind="evidence_runs", run_index=0, status="passed",
               logs=json.dumps({"set": "cheap", "second": False, "cost_cap": None, "pairs": []})))
    db.commit()
    schema = submission_service.to_schema(db.get(Submission, "s1"))
    assert [g.model for g in schema.agent_trials.by_model] == ["anthropic/claude-fable-5-1", "anthropic/claude-haiku-5-5"]
    assert schema.evidence_set.status == "passed" and json.loads(schema.evidence_set.logs)["set"] == "cheap"
    assert any(w.startswith("15.36:") for w in schema.result_warnings)
    md = submission_service.build_summary_markdown(schema, None)
    assert "By model:" in md and "claude-haiku-5-5: 1 of 1" in md
