"""Four rules that read stored results (services/result_rules.py), adapted from the post-fixes branch where they
were Flags 15.6, 15.11, 15.12 and 15.31. Here they are plain warning lines (SubmissionSchema.result_warnings,
the page's Results review box and the evidence summary), with no ruling. 15.20 (no arm64 run) was left out: this
platform runs everything on linux/arm64, so it can never fire, and its reverse would fire on every submission. 15.27
(timeout far above the slowest trial) too: Terminal-Bench tells authors to declare 8 hours, so it always fired."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.services import result_rules as rr
from app.services import verifier_report


def run(kind, index=0, status="passed", reward=None, logs=None, verifier=None, start=None, end=None):
    return SimpleNamespace(kind=kind, run_index=index, status=status, reward=reward, logs=logs,
                           verifier_json=json.dumps(verifier) if verifier else None,
                           started_at=start, finished_at=end)


def trial(i, reward, failed, tests=10, agent_s=600.0, limit=28800.0):
    return run("agent", i, status="passed" if reward == 1 else "failed", reward=reward,
               verifier={"ctrf": {"tests": tests, "failed_names": failed},
                         "facts": {"agent_seconds": agent_s, "agent_limit_sec": limit}})


@pytest.fixture
def no_override(monkeypatch):
    monkeypatch.setattr(get_settings(), "agent_timeout_sec", 0)
    monkeypatch.setattr(get_settings(), "min_trials_for_rate", 5)


# ---- 15.6 duplicate test names ---------------------------------------------------------------

def test_ctrf_reader_records_duplicate_names(tmp_path):
    tests = [{"name": "test_case[x]", "status": "passed"}, {"name": "test_case[x]", "status": "failed"},
             {"name": "test_other", "status": "passed"}]
    path = tmp_path / "ctrf.json"
    path.write_text(json.dumps({"results": {"summary": {"tests": 3, "passed": 2, "failed": 1}, "tests": tests}}))
    assert verifier_report._read_ctrf(path)["duplicate_names"] == ["test_case[x]"]


def test_duplicate_names_warns_and_names_the_run():
    runs = [run("oracle", 0, reward=1, verifier={"ctrf": {"tests": 3, "duplicate_names": ["test_case[x]"]}}),
            run("nop", 0, reward=0, verifier={"ctrf": {"tests": 3, "duplicate_names": []}})]
    lines = rr.rule_duplicate_names(runs)
    assert len(lines) == 1 and lines[0].startswith("15.6:") and "oracle 0: test_case[x]" in lines[0]
    assert "test-stdout.txt" in lines[0] and "unique ids" not in lines[0]  # unique pytest ids do not help
    assert rr.rule_duplicate_names(runs[1:]) == []


# ---- 15.11 too few trials for a pass rate ------------------------------------------------------

def test_wilson_interval():
    lo, hi = rr.wilson(1, 3)
    assert lo < 1 / 3 < hi and 0.0 <= lo and hi <= 1.0
    assert rr.wilson(0, 0) == (0.0, 1.0)
    assert rr.pass_rate_text(1, 3) == "1 of 3 passed (95% interval 6% to 79%)"


def test_interval_warns_below_the_minimum(no_override):
    lines = rr.rule_interval(1, 3)
    assert len(lines) == 1 and lines[0].startswith("15.11:") and "1 of 3 passed" in lines[0] and "minimum here is 5" in lines[0]
    assert rr.rule_interval(3, 5) == []
    assert rr.rule_interval(0, 0) == []


# ---- 15.12 few separating tests ----------------------------------------------------------------

def test_separating_tests_warns_when_few_tests_differ():
    trials = [trial(0, 1, []), trial(1, 0, ["t1"]), trial(2, 0, ["t1", "t2"])]
    assert rr.separating_tests(trials) == (["t1", "t2"], 10)
    lines = rr.rule_separating(trials)
    assert len(lines) == 1 and "2 of 10 tests" in lines[0] and "t1" in lines[0]


def test_separating_tests_quiet_when_no_trial_failed_a_test_or_one_trial():
    assert rr.rule_separating([trial(i, 1, []) for i in range(4)]) == []
    assert rr.rule_separating([trial(0, 0, ["t1"])]) == []


def test_separating_tests_quiet_when_many_tests_differ():
    names = [f"t{i}" for i in range(6)]
    trials = [trial(0, 0, names[:3], tests=10), trial(1, 0, names[3:], tests=10)]
    assert rr.rule_separating(trials) == []  # 6 of 10 differ: more than max(3, 10 // 5)


# ---- 15.31 timeout under load ---------------------------------------------------------------------

T0 = datetime(2026, 11, 17, 12, tzinfo=UTC)


def timed_out_trial():
    return run("agent", 2, status="failed", reward=None, logs="=== EXCEPTION: AgentTimeoutError ===",
               start=T0, end=T0 + timedelta(hours=1))


def test_timeout_under_load_warns_when_another_harbor_run_overlapped():
    other = run("oracle", 0, reward=1, start=T0 + timedelta(minutes=10), end=T0 + timedelta(minutes=20))
    lines = rr.rule_timeout_under_load([timed_out_trial()], [other])
    assert len(lines) == 1 and lines[0].startswith("15.31:") and "agent 2" in lines[0] and "1 other" in lines[0]


def test_timeout_under_load_quiet_alone_or_without_a_timeout():
    later = run("oracle", 0, reward=1, start=T0 + timedelta(hours=2), end=T0 + timedelta(hours=3))
    assert rr.rule_timeout_under_load([timed_out_trial()], [later]) == []
    assert rr.rule_timeout_under_load([timed_out_trial()], []) == []
    finished = run("agent", 0, reward=0, status="failed", start=T0, end=T0 + timedelta(hours=1))
    assert rr.rule_timeout_under_load([finished], [later]) == []


def test_a_broken_rule_reports_itself_instead_of_hiding_the_others(monkeypatch):
    def boom(runs):
        raise ValueError("bad data")
    monkeypatch.setattr(rr, "rule_separating", boom)
    lines = rr.evaluate([trial(0, 1, [])], [], (1, 1))
    assert any(line.startswith("15.12: rule error: ValueError") for line in lines)


# ---- the schema, the other submissions' runs and the evidence summary --------------------------------

def test_schema_and_evidence_carry_the_warnings(no_override):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db import Base
    from app.models import Run, Submission
    from app.services import submission_service

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    for sid in ("s1", "s2"):
        db.add(Submission(id=sid, task_name=sid, uploaded_at=now, zip_path="x", extracted_path=None))
    ctrf = {"tests": 10, "passed": 9, "failed": 1, "skipped": 0, "failed_names": ["t1"]}
    report = {"ctrf": ctrf, "facts": {"agent_seconds": 600, "agent_limit_sec": 28800}}
    db.add(Run(submission_id="s1", kind="agent", run_index=0, status="passed", reward=1, verifier_json=json.dumps(
        {**report, "ctrf": {**ctrf, "passed": 10, "failed": 0, "failed_names": []}})))
    db.add(Run(submission_id="s1", kind="agent", run_index=1, status="failed", reward=0, verifier_json=json.dumps(report)))
    db.add(Run(submission_id="s1", kind="agent", run_index=2, status="failed", reward=None,
               logs="=== EXCEPTION: AgentTimeoutError ===", started_at=T0, finished_at=T0 + timedelta(hours=1)))
    db.add(Run(submission_id="s2", kind="oracle", run_index=0, status="passed", reward=1,
               started_at=T0 + timedelta(minutes=5), finished_at=T0 + timedelta(minutes=15)))
    db.commit()

    schema = submission_service.to_schema(db.get(Submission, "s1"))
    rules = {w.split(":", 1)[0] for w in schema.result_warnings}
    assert {"15.11", "15.12", "15.31"} <= rules and "15.27" not in rules  # 15.31 found through s2's run
    assert schema.agent_trials.pass_interval is not None
    assert schema.agent_trials.pass_interval[0] < 1 / 3 < schema.agent_trials.pass_interval[1]

    md = submission_service.build_summary_markdown(schema, None)
    assert "## Results review" in md and "15.31:" in md
    assert "1 of 3 passed (95% interval" in md

    quiet = submission_service.to_schema(db.get(Submission, "s2"))
    assert quiet.result_warnings == [] and "## Results review" not in submission_service.build_summary_markdown(quiet, None)
