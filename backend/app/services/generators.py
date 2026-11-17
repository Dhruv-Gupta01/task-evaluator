"""LLM generators (batch 8). A person reviews every output before it is used.

- idea_review (G1): reviews a task idea before it is built, with Terminal-Bench's own
  proposal prompt (vendor/tb_prompts/task-proposal.md).
- agent_rubric (N13): proposes rubric items for scoring agent trajectories. Every item must
  quote its evidence from a trajectory; a quote that is not found verbatim is dropped, and an
  item left with no verified evidence is dropped too.
- deliverable (N14): a client-facing task write-up built only from stored results. It always
  ends with the line "Draft, needs review."
"""

import json
import re
from pathlib import Path

from app.services.llm.base import Message
from app.services.llm.factory import get_llm_client

PROPOSAL_PROMPT = Path(__file__).resolve().parents[2] / "vendor" / "tb_prompts" / "task-proposal.md"
DRAFT_LINE = "Draft, needs review."
MAX_TRAJECTORY_CHARS = 40_000


def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except ValueError:
        return {}


async def idea_review(idea: str, client=None) -> dict:
    client = client or get_llm_client()
    prompt = PROPOSAL_PROMPT.read_text(encoding="utf-8")
    resp = await client.complete(
        [Message(role="system", content=prompt), Message(role="user", content=f"Task idea:\n\n{idea.strip()}")],
        tools=[],
    )
    return {"review": resp.text.strip(), "prompt": "vendor/tb_prompts/task-proposal.md (bdf6034)", "model": client.model}


RUBRIC_PROMPT = """You write a rubric for scoring agent trajectories on one Terminal-Bench task.
Read the task instruction and the trajectories. Propose 4 to 10 rubric items about the work
(how the agent investigated, verified and decided), never about the final reward alone.
Every item MUST cite at least one piece of evidence: an exact quote, copied character for
character, of 20 to 200 characters from one named trajectory. Do not paraphrase quotes.
Answer with JSON only:
{"items": [{"criterion": "...", "pass_when": "...", "weight": 1-3,
            "evidence": [{"trial": "<trial name>", "quote": "<exact text>"}]}]}"""


def _norm(text: str) -> str:
    return " ".join(text.split())


def verify_rubric(items: list[dict], trajectories: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """Keeps only evidence quoted verbatim (whitespace-insensitive) from the named trajectory."""
    normed = {name: _norm(text) for name, text in trajectories.items()}
    kept, dropped = [], []
    for item in items:
        good, bad = [], []
        for ev in item.get("evidence") or []:
            quote, trial = _norm(str(ev.get("quote", ""))), str(ev.get("trial", ""))
            (good if len(quote) >= 12 and quote in normed.get(trial, "") else bad).append(ev)
        if good:
            kept.append({**item, "evidence": good, "unverified_evidence": bad})
        else:
            dropped.append({**item, "reason": "no quote found verbatim in the named trajectory"})
    return kept, dropped


async def agent_rubric(instruction: str, trajectories: dict[str, str], client=None) -> dict:
    if not trajectories:
        return {"items": [], "dropped": [], "error": "no agent trajectories to read; run agent trials first"}
    client = client or get_llm_client()
    body = "\n\n".join(f"=== TRAJECTORY {name} ===\n{text[-MAX_TRAJECTORY_CHARS:]}" for name, text in trajectories.items())
    resp = await client.complete(
        [Message(role="system", content=RUBRIC_PROMPT),
         Message(role="user", content=f"=== INSTRUCTION ===\n{instruction}\n\n{body}")],
        tools=[],
    )
    items = _json(resp.text).get("items") or []
    kept, dropped = verify_rubric([i for i in items if isinstance(i, dict)], trajectories)
    return {"items": kept, "dropped": dropped, "model": client.model, "trials": sorted(trajectories)}


DELIVERABLE_PROMPT = f"""You write the client-facing summary of one Terminal-Bench task from the FACTS
JSON only. Sections: Overview, Verification, Evidence (pass rates exactly as given, with their
intervals), Open flags and rulings, Known limits. Use no number, name or claim that is not in
FACTS; when something was not run, say it was not run. Markdown. The last line must be exactly:
{DRAFT_LINE}"""


async def deliverable(facts: dict, client=None) -> dict:
    client = client or get_llm_client()
    resp = await client.complete(
        [Message(role="system", content=DELIVERABLE_PROMPT),
         Message(role="user", content="FACTS:\n" + json.dumps(facts, indent=1, default=str)[:120_000])],
        tools=[],
    )
    text = resp.text.rstrip()
    if not text.endswith(DRAFT_LINE):
        text += f"\n\n{DRAFT_LINE}"
    return {"markdown": text, "model": client.model}
