"""Web tools stay on by default and what an agent did with the web reaches the judge and the evidence."""

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from app.config import Settings
from app.services import harbor_runner, submission_service as ss, web_use
from app.services.llm.base import LLMResponse
from app.services.tier1 import hacker, judge as judge1
from app.services.tier2 import judge as judge2


def write_trial(root, calls, reward=1.0):
    agent = root / "agent"
    agent.mkdir(parents=True)
    steps = [{"source": "agent", "message": "working", "tool_calls": [{"function_name": n, "arguments": a} for n, a in calls]}]
    (agent / "trajectory.json").write_text(json.dumps({"steps": steps}))
    (root / "verifier").mkdir()
    (root / "verifier" / "reward.txt").write_text(str(reward))
    (root / "result.json").write_text(json.dumps({"verifier_result": {"rewards": {"reward": reward}}, "agent_result": {}}))
    return root / "result.json"


def test_agents_run_with_web_tools_on_by_default():
    assert Settings(_env_file=None).agent_disable_web_search is False


def test_both_judges_are_told_that_finding_the_answer_online_is_a_shortcut_but_docs_are_not():
    for system in (judge1.SYSTEM, judge2.SYSTEM):
        assert "WEB USE" in system and "published solution" in system
        assert "Reading documentation, or installing a library or tool the task needs, is not" in system


def test_the_transcript_starts_with_the_computed_web_use_line(tmp_path):
    used = tmp_path / "a"
    write_trial(used, [("WebFetch", {"url": "https://github.com/acme/answers"}), ("Bash", {"command": "pip install numpy"})])
    text = hacker.condense_trajectory(used)
    assert text.splitlines()[0] == ("WEB USE (counted from the agent's tool calls): 1 web-tool call(s), 1 package install(s); "
                                    "hosts: github.com")
    clean = tmp_path / "b"
    write_trial(clean, [("Read", {"file_path": "/app/x"})])
    assert hacker.condense_trajectory(clean).splitlines()[0].endswith(": none")
    assert hacker.condense_trajectory(tmp_path / "nowhere") == ""


class FakeClient:
    def __init__(self, answer):
        self.answer, self.sent = answer, None

    async def complete(self, messages, tools=None, **kw):
        self.sent = messages
        return LLMResponse(text=json.dumps(self.answer))


@pytest.mark.parametrize("which", ["tier1", "tier2"])
def test_the_judge_sees_the_web_use_line_and_the_rule(monkeypatch, tmp_path, which):
    trial = tmp_path / "t"
    write_trial(trial, [("WebSearch", {"query": "solution for task"})])
    transcript = hacker.condense_trajectory(trial)
    answer = {"verdict": "exploited", "confidence": "high", "summary": "searched the answer", "techniques": ["web lookup"],
              "defences": [], "reasoning": "found it online", "exploit_kind": "copied_answers", "tried_shortcut": True}
    client = FakeClient(answer)
    module = judge1 if which == "tier1" else judge2
    monkeypatch.setattr(module, "get_anthropic_client", lambda *a, **k: client)
    result = asyncio.run(module.classify("do it", "", transcript, 1.0) if which == "tier1" else module.classify("do it", transcript, 1.0))
    assert result["verdict"] == "exploited"
    sent = {m.role: m.content for m in client.sent}
    assert "WEB USE (counted from the agent's tool calls): 1 web-tool call(s)" in sent["user"]
    assert "published solution" in sent["system"]


def test_a_parsed_trial_carries_web_use_and_a_flag(tmp_path):
    result = write_trial(tmp_path / "trial", [("WebFetch", {"url": "https://example.com/answer"}), ("Bash", {"command": "ls"})])
    outcome = harbor_runner._parse_trial(result, None, None, None)
    assert outcome.report["web_use"]["web_tool_calls"] == 1 and outcome.report["web_use"]["hosts"] == ["example.com"]
    assert any(f.startswith("the agent used the web (1 web-tool call(s); hosts: example.com)") for f in outcome.report["flags"])
    quiet = harbor_runner._parse_trial(write_trial(tmp_path / "q", [("Read", {"file_path": "/x"})]), None, None, None)
    assert quiet.report["web_use"]["used_web"] is False and not any("used the web" in f for f in quiet.report["flags"])


def logs(*uses):
    return json.dumps({"attempts": [{"verdict": "honest_solve", "web_use": u} for u in uses]})


def test_the_tier_evidence_says_whether_the_agent_used_the_web():
    looked = {"used_web": True, "web_tool_calls": 2, "fetches": 1, "installs": 0, "hosts": ["github.com"]}
    none = {"used_web": False, "web_tool_calls": 0, "fetches": 0, "installs": 0, "hosts": []}
    schema = NS(tier0=NS(status="not-run", logs=None), tier1=NS(status="failed", logs=logs(looked, none)),
                tier2=NS(status="failed", logs=logs(none, none)), tier_cycle=NS(status="not-run", logs=None))
    text = "\n".join(ss.cheat_evidence_lines(schema))
    assert "1 of 2 attempt(s) looked something up on the web (2 web-tool call(s), 1 curl/wget/git clone); hosts: github.com" in text
    assert "Web use: none in any attempt." in text
    assert "counts finding the answer online as a shortcut" in text


def test_attempts_without_a_trajectory_are_said_to_be_unreadable():
    schema = NS(tier0=NS(status="not-run", logs=None), tier1=NS(status="failed", logs=logs(None, None)),
                tier2=NS(status="not-run", logs=None), tier_cycle=NS(status="not-run", logs=None))
    assert "Web use: not readable" in "\n".join(ss.cheat_evidence_lines(schema))


def test_agent_trials_get_a_web_use_line():
    trials = [NS(verifier=NS(web_use={"used_web": True, "web_tool_calls": 1, "fetches": 0, "installs": 0, "hosts": ["pypi.org"]})),
              NS(verifier=NS(web_use={"used_web": False, "web_tool_calls": 0, "fetches": 0, "installs": 0, "hosts": []})),
              NS(verifier=None)]
    line = ss._agent_web_use_lines(NS(agent_trials=NS(trials=trials)))[0]
    assert line.startswith("- Web use: 1 of 2 trial(s) looked something up on the web (1 web-tool call(s)")
    assert ss._agent_web_use_lines(NS(agent_trials=NS(trials=[NS(verifier=None)]))) == []


def test_run_limits_report_declared_versus_ran(tmp_path):
    from datetime import UTC, datetime

    from app.models import Submission

    root = tmp_path / "task"
    root.mkdir()
    (root / "task.toml").write_text('[task]\nname = "x"\n[environment]\nallow_internet = false\n')
    sub = Submission(id="s1", task_name="x", uploaded_at=datetime.now(UTC), zip_path="z", extracted_path=str(root))
    schema = NS(agent_trials=NS(trials=[]), oracle=NS(runs=[]))
    out = ss.run_limits(sub, schema)
    assert out["network"]["declared"]["mode"] == "no-network" and out["network"]["effective"]["overrides_task"]
    text = " | ".join(out["warnings"])
    assert "declares network no-network (via `allow_internet`) but the platform ran it with network_mode=public" in text
    assert "the agent's web tools on, as in Terminal-Bench" in text


WEB = {"used_web": True, "web_tool_calls": 1, "fetches": 0, "installs": 0, "hosts": ["github.com"], "examples": ["WebFetch: x"]}


def test_tier2_attempts_keep_their_web_use(tmp_path):
    from app.services.tier2 import loop

    async def run_attempt(task_dir, index):
        return {"reward": 1.0, "tool_calls": 5, "cost_usd": 0.1, "trial_dir": str(tmp_path), "web_use": WEB}

    async def classify(outcome):
        return {"verdict": "exploited", "confidence": "high", "summary": "looked it up", "techniques": []}

    result = asyncio.run(loop.run(tmp_path, loop.Hooks(run_attempt, classify), loop.Limits()))
    assert result["attempts"][0]["web_use"] == WEB and result["attempts"][0]["verdict"] == "exploited"


def test_tier1_attempts_keep_their_web_use(tmp_path):
    from app.services.tier1 import loop

    async def run_attempt(task_dir, memory, index):
        return {"reward": 0.0, "tool_calls": 6, "cost_usd": 0.1, "trial_dir": str(tmp_path), "web_use": WEB}

    async def classify(outcome, memory):
        return {"verdict": "attempted_failed", "tried_shortcut": True, "confidence": "high", "summary": "searched", "techniques": []}

    async def propose(*a):
        return None

    hooks = loop.Hooks(run_attempt, classify, propose, lambda *a: None, lambda *a: None)
    # no attempt cap on this branch: the loop ends robust after 3 misses in a row; each attempt keeps its web use
    result = asyncio.run(loop.run(tmp_path, tmp_path, hooks, loop.Limits()))
    assert result["status"] == "robust" and all(a["web_use"] == WEB for a in result["attempts"])
