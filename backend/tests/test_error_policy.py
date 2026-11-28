"""Harbor's own classification of a trial's failure (services/error_policy.py): trials with no valid reward are
left out of the pass rate, a NaN or infinite reward is no reward (+inf used to pass `>= 1.0`), and the one
automatic infra retry follows Harbor's should_retry (never a cancelled trial).
"""
import json
import math
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Run, Submission
from app.services import error_policy, harbor_runner, submission_service, task_runner


# ---- the table ------------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("CancelledError", (False, False, False)),
    ("NonZeroAgentExitCodeError", (False, True, True)),
    ("AgentTimeoutError", (False, True, True)),
    ("EnvironmentStartTimeoutError", (True, False, False)),
    ("ApiRateLimitError", (True, True, False)),
    ("RewardFileNotFoundError", (False, True, False)),
    ("AgentAuthenticationError", (False, False, False)),
])
def test_known_exception_types_have_harbors_classification(name, expected):
    p = error_policy.lookup(name)
    assert (p.should_retry, p.should_grade, p.has_valid_reward) == expected
    assert p.known is True


def test_an_unknown_exception_type_gets_harbors_default_and_is_marked_unknown():
    p = error_policy.lookup("SomethingNewError")
    assert (p.should_retry, p.should_grade, p.has_valid_reward) == (True, True, True)
    assert p.known is False


def test_the_copy_names_the_harbor_version_it_was_taken_from():
    assert error_policy.HARBOR_VERSION == "0.26.0"


def installed_harbor_table():
    python = Path.home() / ".local/share/uv/tools/harbor/bin/python"
    if not python.exists():
        return None
    code = (
        "import json\nfrom harbor.models.trial.error_policy import ERROR_POLICIES\n"
        "print(json.dumps({k: [v.should_retry, v.should_grade, v.has_valid_reward] for k, v in ERROR_POLICIES.items()}))"
    )
    r = subprocess.run([str(python), "-c", code], capture_output=True, text=True, timeout=60)
    return json.loads(r.stdout) if r.returncode == 0 else None


def test_our_copy_matches_the_installed_harbor():
    """If Harbor changes its table, this fails and the copy has to be refreshed (like the vendored checks)."""
    table = installed_harbor_table()
    if table is None:
        pytest.skip("the Harbor tool environment is not importable here")
    ours = {k: [p.should_retry, p.should_grade, p.has_valid_reward] for k, p in error_policy.POLICIES.items()}
    assert ours == table


# ---- reading a trial's exception ------------------------------------------------------------

def test_info_from_an_exception_gives_the_flags_and_names_the_type():
    info = error_policy.from_exception_info({"exception_type": "CancelledError", "exception_message": "stopped"})
    assert info == {"exception_type": "CancelledError", "should_retry": False, "should_grade": False,
                    "has_valid_reward": False, "known": True}


def test_no_exception_means_no_info():
    assert error_policy.from_exception_info(None) is None
    assert error_policy.from_exception_info({}) is None
    assert error_policy.from_exception_info({"exception_message": "no type"}) is None


def test_only_a_known_type_with_no_valid_reward_gives_a_reason():
    assert "CancelledError" in error_policy.no_valid_reward_reason({"exception_type": "CancelledError"})
    assert error_policy.no_valid_reward_reason({"exception_type": "NonZeroAgentExitCodeError"}) is None
    assert error_policy.no_valid_reward_reason({"exception_type": "SomethingNewError"}) is None  # unknown: not guessed
    assert error_policy.no_valid_reward_reason(None) is None


# ---- _parse_trial ---------------------------------------------------------------------------

def trial_dir(tmp_path, result: dict | str):
    (tmp_path / "verifier").mkdir(parents=True, exist_ok=True)
    text = result if isinstance(result, str) else json.dumps(result)
    (tmp_path / "result.json").write_text(text)
    return tmp_path / "result.json"


def test_a_cancelled_trial_is_marked_as_having_no_valid_reward(tmp_path):
    out = harbor_runner._parse_trial(trial_dir(tmp_path, {
        "verifier_result": None,
        "exception_info": {"exception_type": "CancelledError", "exception_message": "stopped by the platform"},
    }))
    report = out.report
    assert report["error_policy"]["has_valid_reward"] is False
    assert "CancelledError" in report["no_valid_reward"]
    assert any("no valid reward" in f and "not an agent failure" in f for f in report["flags"])


def test_an_agent_crash_that_harbor_grades_keeps_its_reward_and_is_not_excluded(tmp_path):
    out = harbor_runner._parse_trial(trial_dir(tmp_path, {
        "verifier_result": {"rewards": {"reward": 0.0}},
        "exception_info": {"exception_type": "NonZeroAgentExitCodeError", "exception_message": "exit 1"},
    }))
    assert out.reward == 0.0
    assert out.report["error_policy"]["should_retry"] is False
    assert not out.report.get("no_valid_reward")


def test_a_trial_without_an_exception_has_no_policy_entry(tmp_path):
    out = harbor_runner._parse_trial(trial_dir(tmp_path, {"verifier_result": {"rewards": {"reward": 1.0}}}))
    assert "error_policy" not in out.report and not out.report.get("no_valid_reward")


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_a_reward_that_is_not_a_finite_number_is_treated_as_no_reward(tmp_path, literal):
    out = harbor_runner._parse_trial(trial_dir(tmp_path, '{"verifier_result": {"rewards": {"reward": %s}}}' % literal))
    assert out.reward is None
    assert "not a finite number" in out.report["no_valid_reward"]
    assert any("not a finite number" in f for f in out.report["flags"])


def test_a_finite_reward_is_untouched(tmp_path):
    out = harbor_runner._parse_trial(trial_dir(tmp_path, {"verifier_result": {"rewards": {"reward": 0.73}}}))
    assert out.reward == 0.73 and not out.report.get("no_valid_reward")


def test_positive_infinity_can_no_longer_count_as_a_pass(tmp_path):
    out = harbor_runner._parse_trial(trial_dir(tmp_path, '{"verifier_result": {"rewards": {"reward": Infinity}}}'))
    run = Run(submission_id="s", kind="agent", run_index=0, status="pending")
    task_runner._apply_trial_outcome(run, out)
    assert run.reward is None and run.status == "failed"


# ---- the pass rate --------------------------------------------------------------------------

@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Submission(id="s1", task_name="t", uploaded_at=datetime.now(UTC), zip_path="x", extracted_path="x"))
    session.commit()
    yield session
    session.close()


def add_trials(db, specs):
    """specs: (reward, report-dict-or-None) per trial."""
    for i, (reward, report) in enumerate(specs):
        db.add(Run(submission_id="s1", kind="agent", run_index=i, status="passed" if reward == 1 else "failed",
                   reward=reward, raw_reward=None if reward is None else float(reward),
                   verifier_json=json.dumps(report) if report is not None else None))
    db.commit()
    return db.get(Submission, "s1")


NO_VALID = {"no_valid_reward": "Harbor classes CancelledError as having no valid reward",
            "error_policy": {"exception_type": "CancelledError", "should_retry": False, "should_grade": False,
                             "has_valid_reward": False, "known": True}, "flags": []}


def test_a_trial_with_no_valid_reward_is_left_out_of_the_pass_rate(db):
    sub = add_trials(db, [(1, None), (0, None), (None, NO_VALID)])
    res = submission_service._agent_trials_result(sub)
    assert res.n == 3 and res.no_valid_reward == 1
    assert res.pass_rate == pytest.approx(1 / 2)  # not 1 / 3
    assert set(res.pass_at_k) == {"1", "2"}


def test_a_trial_that_is_both_a_grader_error_and_no_valid_reward_is_removed_once(db):
    both = {**NO_VALID, "grader_error": {"reason": "reward_file_missing", "evidence": "x"}}
    sub = add_trials(db, [(1, None), (0, None), (None, both)])
    res = submission_service._agent_trials_result(sub)
    assert res.grader_errors == 1 and res.no_valid_reward == 1
    assert res.pass_rate == pytest.approx(1 / 2)  # 2 scored trials, not 1


def test_a_failure_without_the_marker_still_counts_against_the_agent(db):
    sub = add_trials(db, [(1, None), (0, None), (None, {"flags": []})])
    res = submission_service._agent_trials_result(sub)
    assert res.no_valid_reward == 0 and res.pass_rate == pytest.approx(1 / 3)


def test_all_trials_without_a_valid_reward_give_no_rate_not_zero(db):
    sub = add_trials(db, [(None, NO_VALID), (None, NO_VALID)])
    res = submission_service._agent_trials_result(sub)
    assert res.pass_rate is None and res.no_valid_reward == 2


def test_the_evidence_summary_says_how_many_were_left_out(db):
    sub = add_trials(db, [(1, None), (None, NO_VALID)])
    schema = submission_service.to_schema(sub)
    text = submission_service.build_summary_markdown(schema, None)
    assert "no valid reward" in text.lower() and "1 of 2" in text


# ---- the retry decision ---------------------------------------------------------------------

def run_with(reward, exception_type=None, logs="agent trial log", extra_report=None):
    report = dict(extra_report or {})
    if exception_type:
        report["error_policy"] = error_policy.from_exception_info({"exception_type": exception_type})
    return Run(submission_id="s", kind="agent", run_index=0, status="failed" if reward != 1 else "passed",
               reward=reward, logs=logs, verifier_json=json.dumps(report) if report else None)


INFRA = "litellm.RateLimitError: rate limit exceeded, please retry"


def test_no_reward_plus_infra_noise_is_still_retried():
    assert task_runner.infra_retry_reason(run_with(None, logs=INFRA)) is not None


def test_no_reward_with_a_type_harbor_says_is_retryable_is_retried_without_a_text_marker():
    reason = task_runner.infra_retry_reason(run_with(None, "EnvironmentStartTimeoutError"))
    assert reason and "EnvironmentStartTimeoutError" in reason


def test_a_cancelled_trial_is_never_retried_even_if_its_log_shows_noise():
    """Harbor says should_retry is False, and the platform's own stop (cost cap, user) must not restart the work."""
    assert task_runner.infra_retry_reason(run_with(None, "CancelledError", logs=INFRA)) is None


def test_a_reward_file_error_is_not_retried():
    assert task_runner.infra_retry_reason(run_with(None, "RewardFileNotFoundError", logs=INFRA)) is None


def test_an_agent_that_exited_non_zero_while_infra_noise_showed_is_retried_although_harbor_graded_it():
    reason = task_runner.infra_retry_reason(run_with(0, "NonZeroAgentExitCodeError", logs=INFRA))
    assert reason and "graded" in reason


def test_an_agent_that_exited_non_zero_with_no_infra_noise_is_a_real_failure():
    assert task_runner.infra_retry_reason(run_with(0, "NonZeroAgentExitCodeError")) is None


def test_a_plain_zero_with_infra_noise_in_the_log_is_not_retried():
    """Nothing says the agent died; the zero is a real result."""
    assert task_runner.infra_retry_reason(run_with(0, None, logs=INFRA)) is None


def test_a_pass_is_never_retried():
    assert task_runner.infra_retry_reason(run_with(1, "NonZeroAgentExitCodeError", logs=INFRA)) is None


def test_a_trial_already_retried_is_not_retried_again():
    logs = f"{task_runner._INFRA_RETRY_MARKER} earlier\n{INFRA}"
    assert task_runner.infra_retry_reason(run_with(None, logs=logs)) is None


def test_a_run_with_no_logs_is_not_retried():
    assert task_runner.infra_retry_reason(run_with(None, logs="")) is None


# ---- the retry loop end to end (Harbor faked) -----------------------------------------------

def test_the_retry_loop_reruns_an_infra_killed_agent_and_records_why(db, monkeypatch, tmp_path):
    import asyncio

    sub = db.get(Submission, "s1")
    run = run_with(0, "NonZeroAgentExitCodeError", logs=INFRA)
    run.submission_id, run.run_index = "s1", 0
    db.add(run)
    db.commit()

    async def fake_run_job(*args, **kwargs):
        ok = harbor_runner.TrialOutcome(reward=1.0, logs="retry log", report={"flags": []})
        return harbor_runner.HarborJobResult(trials=[ok])

    monkeypatch.setattr(harbor_runner, "run_job", fake_run_job)
    asyncio.run(task_runner._retry_infra_agent_failures(
        db, sub, "s1", [run], "claude-code", "anthropic/x", {}, 60.0, tmp_path / "jobs"))
    assert run.reward == 1 and run.status == "passed"
    assert "exited non-zero while the log showed infra noise" in run.logs  # the reason, not "None"
    assert "None" not in run.logs.split("=== ORIGINAL")[0]
    assert "retry log" in run.logs


def test_the_retry_loop_leaves_a_real_failure_alone(db, monkeypatch, tmp_path):
    import asyncio

    sub = db.get(Submission, "s1")
    run = run_with(0, "NonZeroAgentExitCodeError", logs="the agent's solution crashed with a ValueError")
    run.submission_id, run.run_index = "s1", 0
    db.add(run)
    db.commit()
    called = []

    async def fake_run_job(*args, **kwargs):
        called.append(1)
        return harbor_runner.HarborJobResult(trials=[])

    monkeypatch.setattr(harbor_runner, "run_job", fake_run_job)
    asyncio.run(task_runner._retry_infra_agent_failures(
        db, sub, "s1", [run], "claude-code", "anthropic/x", {}, 60.0, tmp_path / "jobs"))
    assert not called and run.reward == 0
