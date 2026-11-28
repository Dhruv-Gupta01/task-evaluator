"""Model-written drafts that a person reviews before anything uses them (ported from the post-fixes branch,
batch 8, on 2026-11-28).

- idea_review (G1): reviews a task idea before it is built, with Terminal-Bench's own proposal prompt
  (vendor/tb_prompts/task-proposal.md), its task guidelines and the list of accepted tasks.
- agent_rubric (N13): proposes rubric items about how agents worked on one task. Every item must quote its
  evidence from a named trial; a quote not found word for word in that trial is dropped, and an item left with no
  verified quote is dropped too. It reads each trial's full transcript, with no overall cap (this branch's rule for
  model judges).

The post-fixes branch's deliverable draft is not here: the evidence summary (built from data, no model) and the
task-deliverable skill already produce the client write-up.
"""

import json
import re
from pathlib import Path

from app.services.judge_guard import EVIDENCE_GUARD
from app.services.llm.base import Message
from app.services.llm.factory import get_llm_client

PROPOSAL_PROMPT = Path(__file__).resolve().parents[2] / "vendor" / "tb_prompts" / "task-proposal.md"
GUIDELINES = PROPOSAL_PROMPT.with_name("task-guidelines.md")
GUIDELINES_PLACEHOLDER = "{{task_guidelines}}"
EXISTING_TASKS = PROPOSAL_PROMPT.with_name("existing-tasks.md")
PROPOSAL_COMMIT = "bdf6034"
MIN_QUOTE_CHARS = 12


def proposal_prompt() -> str:
    """The proposal prompt with Terminal-Bench's task guidelines put where it says {{task_guidelines}}, as
    upstream's own rubric_review.py does. The guidelines promise a list of accepted tasks "at the end of this
    document" that upstream's file does not have, so the vendored list goes before the proposal itself."""
    text = PROPOSAL_PROMPT.read_text(encoding="utf-8")
    text = text.replace(GUIDELINES_PLACEHOLDER, GUIDELINES.read_text(encoding="utf-8").strip())
    marker = "# Task proposal to evaluate"
    listing = EXISTING_TASKS.read_text(encoding="utf-8").strip()
    return text.replace(marker, listing + "\n\n" + marker, 1) if marker in text else text + "\n\n" + listing


def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def idea_review(idea: str, client=None) -> dict:
    client = client or get_llm_client()
    resp = await client.complete(
        [Message(role="system", content=proposal_prompt()),
         Message(role="user", content=f"Task idea:\n\n{idea.strip()}")],
        tools=[],
    )
    return {"review": resp.text.strip(), "prompt": f"vendor/tb_prompts/task-proposal.md ({PROPOSAL_COMMIT})",
            "model": client.model}


RUBRIC_PROMPT = f"""You write a rubric for scoring agent trajectories on one Terminal-Bench task.
Read the task instruction and the trajectories. Propose 4 to 10 rubric items about the work
(how the agent investigated, verified and decided), never about the final reward alone.
Every item MUST cite at least one piece of evidence: an exact quote, copied character for
character, of 20 to 200 characters from one named trajectory. Do not paraphrase quotes.
Answer with JSON only:
{{"items": [{{"criterion": "...", "pass_when": "...", "weight": 1-3,
            "evidence": [{{"trial": "<trial name>", "quote": "<exact text>"}}]}}]}}

{EVIDENCE_GUARD}"""


def _norm(text: str) -> str:
    return " ".join(text.split())


def verify_rubric(items: list[dict], trajectories: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """Keeps only evidence quoted word for word (whitespace-insensitive) from the trial it names."""
    normed = {name: _norm(text) for name, text in trajectories.items()}
    kept, dropped = [], []
    for item in items:
        good, bad = [], []
        for ev in item.get("evidence") or []:
            if not isinstance(ev, dict):
                continue
            quote, trial = _norm(str(ev.get("quote", ""))), str(ev.get("trial", ""))
            (good if len(quote) >= MIN_QUOTE_CHARS and quote in normed.get(trial, "") else bad).append(ev)
        if good:
            kept.append({**item, "evidence": good, "unverified_evidence": bad})
        else:
            dropped.append({**item, "reason": "no quote found word for word in the named trial"})
    return kept, dropped


async def agent_rubric(instruction: str, trajectories: dict[str, str], client=None) -> dict:
    """Rubric items from every trial's full transcript (nothing is cut). A model whose context cannot hold them
    fails the call, which is reported as an error, never as a partial rubric."""
    if not trajectories:
        return {"items": [], "dropped": [], "error": "no agent trajectories to read; run agent trials first"}
    client = client or get_llm_client()
    body = "\n\n".join(f"=== TRAJECTORY {name} ===\n{text}" for name, text in trajectories.items())
    resp = await client.complete(
        [Message(role="system", content=RUBRIC_PROMPT),
         Message(role="user", content=f"=== INSTRUCTION ===\n{instruction}\n\n{body}")],
        tools=[],
    )
    items = _json(resp.text).get("items")
    out = {"items": [], "dropped": [], "model": client.model, "trials": sorted(trajectories),
           "transcript_chars": sum(len(t) for t in trajectories.values())}
    if not isinstance(items, list):
        return {**out, "error": "the model gave no rubric items (its answer was not the JSON asked for)",
                "raw": (resp.text or "")[:2000]}
    kept, dropped = verify_rubric([i for i in items if isinstance(i, dict)], trajectories)
    return {**out, "items": kept, "dropped": dropped}
