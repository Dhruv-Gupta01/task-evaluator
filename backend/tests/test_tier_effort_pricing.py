from app.services import task_runner
from app.services.llm import pricing
from app.services.llm.base import Usage


def test_haiku_45_is_priced():
    assert pricing.is_priced("claude-haiku-4-5")
    assert pricing.cost_usd("claude-haiku-4-5", Usage(input_tokens=1_000_000, output_tokens=1_000_000)) == 6.0


def test_effort_meta_says_none_was_sent_for_haiku_45():
    m = task_runner._tier_effort_meta("claude-code", "anthropic/claude-haiku-4-5", "medium")
    assert m["reasoning_effort"] is None and m["reasoning_effort_requested"] == "medium"
    assert "no effort" in m["reasoning_effort_note"]


def test_effort_meta_keeps_effort_for_models_that_take_it():
    m = task_runner._tier_effort_meta("claude-code", "anthropic/claude-haiku-5-5", "medium")
    assert m == {"reasoning_effort": "medium", "reasoning_effort_requested": "medium"}
