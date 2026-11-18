"""Batches 4, 6 and 7: probe planning and the Flag rules that read stored results."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services import flag_results as fres
from app.services import probes
from tests.test_flags import make_task


def run(kind, index=0, status="passed", reward=None, logs=None, verifier=None, start=None, end=None):
    return SimpleNamespace(kind=kind, run_index=index, status=status, reward=reward,
                           logs=json.dumps(logs) if isinstance(logs, (dict, list)) else logs,
                           verifier_json=json.dumps(verifier) if verifier else None,
                           started_at=start, finished_at=end)


def fired(rule, runs, **kw):
    return fres.RESULT_RULES[rule](runs, **kw).findings


def test_probe_plan_reads_data_folders_and_artifacts(tmp_path):
    code = 'from pathlib import Path\nD = Path("/tests") / "fixtures"\n'
    root = make_task(tmp_path / "t", code, files={"tests/fixtures/a.json": "{}", "tests/generator/gen.py": "x=1\n"})
    names = [p["name"] for p in probes.plan(root, hostile_kinds=("fifo",))]
    assert names == ["control", "remove tests/fixtures", "fifo at /app/out.json",
                     "null rung 1 (nop)", "null rung 2 (empty artifacts)"]


def test_probe_changes_to_the_copy(tmp_path):
    root = make_task(tmp_path / "t", files={"tests/fixtures/a.json": "{}"})
    (root / "solution" / "solve.sh").write_text("#!/bin/bash\necho real > /app/out.json\n")
    probes.apply(root, {"kind": "hostile", "artifact": "/app/out.json", "hostile": "fifo"})
    solve = (root / "solution" / "solve.sh").read_text()
    assert "solve_probe_original.sh" in solve and 'mkfifo "$A"' in solve
    assert (root / "solution" / "solve_probe_original.sh").read_text().endswith("/app/out.json\n")
    probes.apply(root, {"kind": "data_removal", "folder": "fixtures"})
    assert not (root / "tests" / "fixtures").exists()
    probes.cap_verifier_timeout(root, 150)
    assert "timeout_sec = 150.0" in (root / "task.toml").read_text()


def test_probe_verdicts_need_a_positive_control():
    hostile = {"kind": "hostile"}
    assert probes.verdict(hostile, 0.0, 1.0) == "held"
    assert probes.verdict(hostile, 0.0, 0.0) == "unverified"
    assert probes.verdict(hostile, None, 1.0) == "invalid_reward"
    assert probes.verdict({"kind": "data_removal"}, 1.0, 1.0) == "failed_open"


def _probe_run(entries, control=1.0, expected=61):
    return run("probes", logs={"in_progress": False, "control_reward": control, "expected_tests": expected,
                               "probes": entries})


def test_probe_rules():
    runs = [_probe_run([
        {"name": "control", "kind": "control", "verdict": "reached_1", "reward": 1.0, "tests": 61},
        {"name": "remove tests/testdata", "kind": "data_removal", "verdict": "failed_open", "reward": 1.0, "tests": 6},
        {"name": "fifo at /app/x", "kind": "hostile", "verdict": "invalid_reward", "reward": None, "tests": None},
        {"name": "null rung 2", "kind": "null_rung2", "verdict": "held", "reward": 0.0, "tests": 61},
    ])]
    assert "failed_open" in fired("15.1", runs)[0].evidence[0]
    assert "6 test(s) collected, control 61" in fired("15.8", runs)[0].evidence[0]
    assert "invalid_reward" in fired("15.3", runs)[0].evidence[0]
    assert fired("15.37", runs) == [] and fired("15.33", runs) == []
    weak = [_probe_run([{"name": "fifo", "kind": "hostile", "verdict": "unverified", "reward": 0.0}], control=0.0)]
    assert fired("15.33", weak)


def test_report_rules():
    verifier = {"ctrf": {"tests": 3, "duplicate_names": ["test_case[x]"]}}
    runs = [
        run("oracle", reward=1, verifier=verifier),
        run("nop", reward=0, verifier={"facts": {}}),
        run("static_checks", logs="not json at all"),
        run("ai_detection", logs={"skipped": True}),
    ]
    assert "test_case[x]" in fired("15.6", runs)[0].evidence[0]
    ev = fired("15.10", runs)[0].evidence
    assert any("static_checks" in e for e in ev) and any(e.startswith("nop 0: no valid ctrf.json") for e in ev)
    assert "AI Detection" in fired("15.9", runs)[0].evidence[0]


def _trial(i, reward, failed, tests=10, model="anthropic/claude-sonnet-5-5", agent_s=600, limit=28800, platform="linux/amd64"):
    return run("agent", i, reward=reward, logs=f"harbor run -a claude-code -m {model}\n",
               verifier={"ctrf": {"tests": tests, "failed_names": failed},
                         "facts": {"agent_seconds": agent_s, "agent_limit_sec": limit, "platform": platform}})


def test_evidence_rules():
    trials = [_trial(0, 1, []), _trial(1, 0, ["t1"]), _trial(2, 0, ["t1", "t2"])]
    assert fres.wilson(1, 3)[0] < 0.34 < fres.wilson(1, 3)[1]
    assert "1 successes in 3 attempts" in fired("15.11", trials)[0].evidence[0]
    assert "2 of 10 tests separate" in fired("15.12", trials)[0].evidence[0]
    assert fired("15.13", trials) == []
    assert fired("15.13", [_trial(0, 0, ["t1"])])
    assert fired("15.20", trials)
    assert "48.0x" in fired("15.27", trials)[0].detail
    cheap = [_trial(0, 1, [], model="anthropic/claude-haiku-5-5")]
    assert fired("15.36", cheap)


def test_timeout_under_load():
    t0 = datetime(2026, 11, 17, 12, tzinfo=UTC)
    timed_out = run("agent", reward=None, status="failed", logs="AgentTimeoutError", start=t0, end=t0 + timedelta(hours=1))
    other = run("oracle", reward=1, start=t0 + timedelta(minutes=10), end=t0 + timedelta(minutes=20))
    assert fres.RESULT_RULES["15.31"]([timed_out], all_runs=[timed_out, other]).findings
    assert fres.RESULT_RULES["15.31"]([timed_out], all_runs=[timed_out]).findings == []


def test_cheat_coverage():
    assert fired("15.19", [])
    refused = [run("cheat_trial", status="failed", reward=0, logs={"inconclusive": True, "tier": "2"})]
    assert "not evidence the trap held" in fired("15.19", refused)[0].detail
    usable = [run("cheat_trial", status="passed", reward=0, logs={"tier": "1"})]
    assert fired("15.19", usable) == []


def test_separating_tests_quiet_when_every_trial_passed():
    trials = [_trial(i, 1, []) for i in range(6)]
    result = fres.RESULT_RULES["15.12"](trials)
    assert result.findings == [] and "no trial failed a test" in result.note


def test_cheat_trials_of_another_tier_are_kept(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db import Base, get_db
    from app.main import app
    from app.models import Run, Submission
    from app.routers import submissions as router

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Submission(id="s1", task_name="t", uploaded_at=datetime.now(UTC), zip_path="x",
                      extracted_path=str(tmp_path), build_status="passed"))
    db.add(Run(submission_id="s1", kind="cheat_trial", run_index=0, status="passed", logs=json.dumps({"tier": "2"})))
    db.commit()

    async def no_queue(key, coro):
        coro.close()

    monkeypatch.setattr(router.task_queue, "submit", no_queue)
    monkeypatch.setattr(router, "_require_built_submission", lambda sid, db: None)
    app.dependency_overrides[get_db] = lambda: Session()
    try:
        client = TestClient(app)
        assert client.post("/submissions/s1/cheat-trial?n=1&cheat_prompt=hinted").status_code == 202
        rows = sorted((r.run_index, r.status) for r in Session().query(Run).filter_by(kind="cheat_trial"))
        assert rows == [(0, "passed"), (1, "pending")]
        s2 = Session()
        tier1 = s2.query(Run).filter_by(kind="cheat_trial", run_index=1).one()
        tier1.status, tier1.logs = "passed", json.dumps({"tier": "1"})
        s2.commit()
        assert client.post("/submissions/s1/cheat-trial?n=1&cheat_prompt=tb").status_code == 202
        rows = sorted((r.run_index, r.status) for r in Session().query(Run).filter_by(kind="cheat_trial"))
        assert rows == [(1, "passed"), (2, "pending")]  # tier 2 replaced, tier 1 kept
    finally:
        app.dependency_overrides.clear()


def test_case_count_quiet_when_the_verifier_noticed():
    noticed = [_probe_run([
        {"name": "control", "kind": "control", "verdict": "reached_1", "reward": 1.0, "tests": 131},
        {"name": "remove tests/fixtures", "kind": "data_removal", "verdict": "held", "reward": 0.0, "tests": 0},
    ], expected=131)]
    assert fired("15.8", noticed) == [] and fired("15.1", noticed) == []
