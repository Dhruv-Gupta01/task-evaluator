"""Did the agent refuse the task instead of attempting it (5.0 category J)?

Some tasks trip a model's safety filter. A refusal scores reward 0 like a real
failure, so it drags down the pass rate and pass@k without saying anything
about the task's difficulty. This reads a finished trial's own files (no LLM)
and labels a non-passing trial "refused" in two cases:

* the model provider blocked the request (a content-policy or safety error in
  the trial's exception or log), or
* the agent's messages contain a refusal phrase and it did almost no work
  (fewer than MIN_TOOL_CALLS tool calls). An agent that says "I can't X" but
  then runs many commands is engaging with the task, so it is not counted.

A phrase match can be wrong, so the label is evidence for a human, never a
gate. Pass rate and pass@k are left unchanged; the refused count and the pass
rate without refusals are shown beside them."""

import json
import re
from pathlib import Path

MIN_TOOL_CALLS = 3  # same bar as the Cheat Trial's "did not really try"

_PHRASES = re.compile(
    r"\b(?:i\s+(?:can(?:'|’)?t|cannot|won(?:'|’)?t|will\s+not|am\s+not\s+able\s+to|(?:'|’)?m\s+not\s+able\s+to|"
    r"(?:'|’)?m\s+unable\s+to|am\s+unable\s+to|must\s+decline|have\s+to\s+decline|need\s+to\s+decline)"
    r"(?:\s+\w+){0,3}\s+(?:help|assist|comply|provide|continue|proceed|fulfil+|complete|support|do\s+(?:that|this)|"
    r"write|create|build|implement|carry\s+out|perform)"
    r"|i\s+must\s+decline|i(?:'|’)?m\s+sorry,?\s+but\s+i\s+(?:can(?:'|’)?t|cannot|won(?:'|’)?t)"
    r"|against\s+my\s+(?:guidelines|policies|usage\s+policies)|violates?\s+(?:the\s+)?(?:usage|safety|content)\s+polic)",
    re.I,
)
_PROVIDER_BLOCK = re.compile(
    r"content[_ ]policy|usage\s+polic|safety\s+(?:system|filter|classifier)|flagged\s+as\s+potentially\s+violating"
    r"|blocked\s+by\s+(?:our|the)\s+(?:safety|content)|stop_reason[\"': ]+refusal|finish_reason[\"': ]+content_filter"
    r"|invalid_prompt|ResponsibleAIPolicyViolation",
    re.I,
)


def _agent_messages(trial_dir: Path) -> list[str]:
    try:
        steps = json.loads((trial_dir / "agent" / "trajectory.json").read_text()).get("steps") or []
    except (OSError, ValueError):
        return []
    return [str(s.get("message") or "") for s in steps if s.get("source") == "agent" and s.get("message")]


def _snippet(text: str, match: re.Match) -> str:
    start = max(0, match.start() - 60)
    return re.sub(r"\s+", " ", text[start : match.end() + 100]).strip()[:220]


def detect(trial_dir: Path, reward: float | None, tool_calls: int | None, exception_text: str = "") -> dict | None:
    """{"kind": "provider_block" | "phrase", "evidence": str} for a refused trial, else None."""
    if reward is not None and reward >= 1.0:
        return None
    texts = [exception_text]
    try:
        texts.append((trial_dir / "trial.log").read_text(errors="replace")[-20000:])
    except OSError:
        pass
    for text in texts:
        m = _PROVIDER_BLOCK.search(text)
        if m:
            return {"kind": "provider_block", "evidence": _snippet(text, m)}
    if tool_calls is not None and tool_calls >= MIN_TOOL_CALLS:
        return None
    for message in _agent_messages(trial_dir):
        m = _PHRASES.search(message)
        if m:
            return {"kind": "phrase", "evidence": _snippet(message, m)}
    return None
