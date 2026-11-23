"""Item 2, wired in: the real poll loop of harbor_runner.run_job with a fake Harbor process whose agent
edits (or only appends to) its own session log between polls. No Docker, no credits."""
import asyncio
import json

import pytest

from app.services import harbor_runner, log_integrity

LINES = [f'{{"n": {i}}}' for i in range(5)]


class FakeProc:
    """Times out three times (three polls of the loop), then exits."""

    def __init__(self):
        self.calls = 0
        self.returncode = 0

    async def wait(self):
        self.calls += 1
        if self.calls <= 3:
            await asyncio.sleep(30)  # run_job's wait_for gives up after _POLL_INTERVAL_SEC
        return 0


def run_with_agent_behaviour(tmp_path, monkeypatch, behave):
    """behave(poll_number, trial_dir) is called from the container probe, once per poll, like an agent
    acting while the platform polls."""
    root = tmp_path / "task"
    root.mkdir()
    (root / "task.toml").write_text('[task]\nname = "x"\n')
    proc = FakeProc()

    async def fake_exec(*cmd, **kwargs):
        return proc

    polls = {"n": 0}

    def probe(trial_dir):
        polls["n"] += 1
        behave(polls["n"], trial_dir)
        return None

    monkeypatch.setattr(harbor_runner.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(harbor_runner, "_POLL_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(harbor_runner, "_COST_CHECK_INTERVAL_SEC", 0)
    monkeypatch.setattr(harbor_runner, "_probe_container", probe)
    jobs = tmp_path / "jobs"
    (jobs / "j1" / "trial-1").mkdir(parents=True)
    result = asyncio.run(harbor_runner.run_job(root, "claude-code", jobs, "j1", 60))
    return result, jobs / "j1" / "trial-1"


def session(trial_dir):
    return trial_dir / "agent" / "sessions" / "projects" / "-app" / "s.jsonl"


def finish(trial_dir):
    (trial_dir / "result.json").write_text(json.dumps({"verifier_result": {"rewards": {"reward": 1.0}}}))


def write_lines(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def test_an_agent_that_rewrites_its_log_between_polls_is_flagged(tmp_path, monkeypatch):
    def behave(poll, trial_dir):
        if poll == 1:
            write_lines(session(trial_dir), LINES[:4])          # the agent works; the platform sees 4 lines
        elif poll == 2:
            write_lines(session(trial_dir), ["cleaned"] + LINES[1:5])  # ...then edits line 0 to hide a step
        elif poll == 3:
            finish(trial_dir)

    result, trial_dir = run_with_agent_behaviour(tmp_path, monkeypatch, behave)
    outcome = result.trials[0]
    assert outcome.report["log_integrity"]["suspected"] is True
    assert any("rewritten" in f for f in outcome.report["flags"])
    stored = log_integrity.read_report(trial_dir)
    assert stored and stored["suspected"] is True and "s.jsonl" in stored["findings"][0]


def test_an_agent_that_only_appends_is_clean(tmp_path, monkeypatch):
    def behave(poll, trial_dir):
        if poll == 1:
            write_lines(session(trial_dir), LINES[:3])
        elif poll == 2:
            write_lines(session(trial_dir), LINES[:5])
        elif poll == 3:
            finish(trial_dir)

    result, trial_dir = run_with_agent_behaviour(tmp_path, monkeypatch, behave)
    outcome = result.trials[0]
    assert outcome.report["log_integrity"]["suspected"] is False
    assert not any("rewritten" in f for f in outcome.report["flags"])
    assert log_integrity.read_report(trial_dir)["suspected"] is False


def test_a_rewrite_after_the_last_poll_is_caught_by_the_final_copy(tmp_path, monkeypatch):
    """The agent edits the log right before the trial ends; Harbor's end-of-trial copy is what is compared."""
    def behave(poll, trial_dir):
        if poll == 1:
            write_lines(session(trial_dir), LINES[:4])
        elif poll == 3:
            write_lines(session(trial_dir), ["wiped"] + LINES[1:5])
            finish(trial_dir)

    result, _ = run_with_agent_behaviour(tmp_path, monkeypatch, behave)
    assert result.trials[0].report["log_integrity"]["suspected"] is True


def test_an_agent_with_no_session_log_gets_an_empty_clean_report(tmp_path, monkeypatch):
    """Terminus-2 writes its trajectory on the host and has no session log: nothing to compare, nothing flagged."""
    def behave(poll, trial_dir):
        if poll == 3:
            finish(trial_dir)

    result, _ = run_with_agent_behaviour(tmp_path, monkeypatch, behave)
    rep = result.trials[0].report["log_integrity"]
    assert rep["suspected"] is False and rep["sources"] == {}


# ---- the Codex container side, with a fake Docker --------------------------------------------

def test_codex_container_log_is_watched_with_docker_exec(tmp_path, monkeypatch):
    import subprocess

    state = {"data": log_integrity.summarise(b"".join(l.encode() + b"\n" for l in LINES[:3]), 0)}
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        total, prev, all_digest = state["data"]
        out = f"FILE rollout-2026-11-23T01-02-03-abc.jsonl\nTOTAL {total}\nPREV {prev}\nALL {all_digest}\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(harbor_runner.subprocess, "run", fake_run)
    monkeypatch.setattr(harbor_runner, "_trial_container_ids", lambda *a, **k: ["cid1"])
    watch = log_integrity.LogWatch()
    harbor_runner._watch_logs(tmp_path, watch, "codex")
    assert watch.lines_seen("rollout-2026-11-23T01-02-03-abc.jsonl") == 3
    assert calls and calls[0][:3] == ["docker", "exec", "cid1"]

    # the agent rewrites line 0 inside the container; the next look sees a different digest for the first 3 lines
    tampered = b"".join(l.encode() + b"\n" for l in ["cleaned"] + LINES[1:5])
    state["data"] = log_integrity.summarise(tampered, 3)
    harbor_runner._watch_logs(tmp_path, watch, "codex")
    assert watch.report()["suspected"] is True


def test_other_agents_are_not_probed_in_the_container(tmp_path, monkeypatch):
    monkeypatch.setattr(harbor_runner.subprocess, "run", lambda *a, **k: pytest.fail("docker must not be called"))
    harbor_runner._watch_logs(tmp_path, log_integrity.LogWatch(), "claude-code")


def test_docker_errors_are_swallowed(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("docker gone")

    monkeypatch.setattr(harbor_runner.subprocess, "run", boom)
    monkeypatch.setattr(harbor_runner, "_trial_container_ids", lambda *a, **k: ["cid1"])
    harbor_runner._watch_logs(tmp_path, log_integrity.LogWatch(), "codex")  # must not raise
