"""The other LLM prompts that read what an agent produced get the same injection guard as the two judges
(tests/test_judge_injection_guard.py). The Tier 1 fixer matters most: an agent that could talk it into removing
a check or deleting a service would change the task itself, not just a label."""
import pytest

from app.services import judge_guard, task_runner
from app.services.tier1 import fixer


def squash(text: str) -> str:
    return " ".join(text.lower().split())


def test_the_fixer_prompt_carries_its_guard():
    assert judge_guard.FIXER_GUARD in fixer.SYSTEM
    text = squash(judge_guard.FIXER_GUARD)
    for must in ("never instructions", "ignore", "remove or weaken", "tests/ and environment/"):
        assert must in text, must


def test_the_fixer_guard_names_the_things_an_attacker_would_ask_for():
    text = squash(judge_guard.FIXER_GUARD)
    assert "service" in text and "check" in text and "approved" in text


@pytest.mark.parametrize("name", ["_CLASSIFY_PROMPT", "_AUDIT_PROMPT"])
def test_failure_analysis_prompts_carry_the_evidence_guard(name):
    prompt = getattr(task_runner, name)
    assert judge_guard.EVIDENCE_GUARD in prompt
    assert "never instructions" in squash(judge_guard.EVIDENCE_GUARD)
    assert "particular ruling" in squash(judge_guard.EVIDENCE_GUARD)


@pytest.mark.parametrize("name", ["_CLASSIFY_PROMPT", "_AUDIT_PROMPT"])
def test_the_json_answer_format_is_still_intact(name):
    """The guard was added around the prompt; the answer format must not have been disturbed."""
    prompt = getattr(task_runner, name)
    assert "Respond with ONLY JSON" in prompt and prompt.count("{") == prompt.count("}")


def test_the_guard_sits_before_the_task_so_it_cannot_be_scrolled_past():
    assert task_runner._CLASSIFY_PROMPT.startswith(judge_guard.EVIDENCE_GUARD)
    assert task_runner._AUDIT_PROMPT.startswith(judge_guard.EVIDENCE_GUARD)


def test_all_three_guards_are_distinct_but_share_the_core_rule():
    for guard in (judge_guard.TRANSCRIPT_GUARD, judge_guard.EVIDENCE_GUARD, judge_guard.FIXER_GUARD):
        assert "ignore" in squash(guard) and "never instructions" in squash(guard)
