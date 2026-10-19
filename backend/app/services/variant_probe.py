"""Valid-variant probe (4.0 category A): is the grader too strict?

A model reads instruction.md and the reference solution and proposes a few
small edits to solution/ that change the *form* of the answer (a number
format, the other allowed identifier, an order the instruction leaves free)
while staying correct under the instruction. Each proposal must quote the
instruction sentence that allows it. The caller runs the Oracle on a copy of
the task with each edit applied; a variant the verifier rejects means the
grader accepts fewer forms than the instruction does.

The model never sees tests/, so it cannot aim at a known weak spot, and a
proposal whose quote is not in the instruction is dropped, not run."""

import json
import re
from pathlib import Path

from app.services.judge_files import read_text_files
from app.services.llm.base import Message
from app.services.llm.factory import get_llm_client

MAX_VARIANTS = 4
MAX_BREAKS = 6

SYSTEM_PROMPT = f"""You help find graders that are stricter than their task instruction. You are \\
shown a task's instruction.md and its reference solution (solution/). The reference solution \\
produces one correct answer in one particular form.

Propose up to {MAX_VARIANTS} variants of the reference solution that change only the FORM of the answer \\
and are still fully correct under the instruction: for example a different but equivalent number \\
format (11800.0 vs 11800.00), the other identifier or spelling the instruction says is acceptable, \\
a different order where the instruction leaves order free, different whitespace, or a different \\
allowed option. Rules:
- Every variant must be allowed by a sentence in instruction.md. Copy that sentence exactly into \\
"instruction_quote". If the instruction allows only one form, propose nothing.
- Do not change what the answer means, and do not weaken or skip any requirement.
- Make the smallest edit. Express it as exact find/replace pairs on files under solution/. \\
"find" must be text that appears exactly once in that file.
- Prefer variants most likely to expose a grader that compares against one exact form.

Respond with ONLY JSON:
{{"variants": [{{"description": "what changes", "instruction_quote": "exact sentence from instruction.md", \\
"edits": [{{"file": "solution/solve.sh", "find": "exact text", "replace": "new text"}}]}}],
 "reason": "if no variants, why"}}"""


BREAK_PROMPT = f"""You help find graders that are weaker than their task instruction. You are shown a \
task's instruction.md and its reference solution (solution/), which produces a correct answer.

Propose up to {MAX_BREAKS} deliberately BROKEN versions of the reference solution. Each breaks exactly one \
rule the instruction states, so a correct grader must reject it. Examples: leave an output empty or \
truncated, drop items the instruction says to include, add something the instruction forbids, leave \
one element wrong while the rest is right, weaken a check the instruction requires, skip a required \
step, or produce the right shape with wrong content.
Rules:
- Name the rule being broken by copying the instruction sentence that states it exactly into \
"instruction_quote". Only break rules the instruction really states.
- The broken answer must clearly violate that sentence. Do not propose changes that leave the answer \
correct, and do not break more than one rule at a time.
- Make the smallest edit that breaks the rule, as exact find/replace pairs on files under solution/. \
"find" must appear exactly once in that file.
- Prefer breaks a lenient grader might miss: partial, subtle or single-element breaks over total failure.

Respond with ONLY JSON:
{{"variants": [{{"description": "which rule is broken and how", "instruction_quote": "exact sentence from instruction.md", \
"edits": [{{"file": "solution/solve.sh", "find": "exact text", "replace": "new text"}}]}}],
 "reason": "if none, why"}}"""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


async def propose_variants(task_root: Path, mode: str = "valid") -> dict:
    """{"variants": [...kept...], "dropped": [{"description", "reason"}], "reason": str}."""
    instruction = (task_root / "instruction.md").read_text(errors="replace")
    solution = read_text_files(task_root / "solution") if (task_root / "solution").is_dir() else ""
    client = get_llm_client()
    response = await client.complete(
        [
            Message(role="system", content=BREAK_PROMPT if mode == "broken" else SYSTEM_PROMPT),
            Message(
                role="user",
                content=f"=== instruction.md ===\n{instruction}\n\n=== solution/ ===\n{solution}\n",
            ),
        ],
        tools=[],
    )
    match = re.search(r"\{.*\}", response.text, re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}

    kept: list[dict] = []
    dropped: list[dict] = []
    normalized_instruction = _norm(instruction)
    for raw in (data.get("variants") or [])[: MAX_BREAKS if mode == "broken" else MAX_VARIANTS]:
        if not isinstance(raw, dict):
            continue
        description = str(raw.get("description", "variant"))[:300]
        quote = str(raw.get("instruction_quote", ""))
        edits = raw.get("edits")
        if not quote or _norm(quote) not in normalized_instruction:
            dropped.append({"description": description, "reason": "its quote is not in instruction.md"})
        elif not isinstance(edits, list) or not edits:
            dropped.append({"description": description, "reason": "no edits given"})
        else:
            kept.append(
                {
                    "description": description,
                    "instruction_quote": quote[:500],
                    "edits": [
                        {k: str(e.get(k, "")) for k in ("file", "find", "replace")}
                        for e in edits
                        if isinstance(e, dict)
                    ],
                }
            )
    return {"variants": kept, "dropped": dropped, "reason": str(data.get("reason", ""))[:500]}


def apply_edits(copy_root: Path, edits: list[dict]) -> str | None:
    """Apply find/replace edits to a copy of the task. None on success, else why not."""
    root = copy_root.resolve()
    for edit in edits:
        target = (copy_root / edit["file"]).resolve()
        if root not in target.parents or target.relative_to(root).parts[0] != "solution":
            return f"edit touches {edit['file']}, outside solution/"
        if not target.is_file():
            return f"{edit['file']} does not exist"
        text = target.read_text(errors="replace")
        if not edit["find"] or text.count(edit["find"]) != 1:
            return f"the text to replace does not appear exactly once in {edit['file']}"
        target.write_text(text.replace(edit["find"], edit["replace"], 1))
    return None
