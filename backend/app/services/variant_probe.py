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

SYSTEM_PROMPT = f"""You help find graders that are stricter than their task instruction. You are \
shown a task's instruction.md and its reference solution (solution/). The reference solution \
produces one correct answer in one particular form.

Propose up to {MAX_VARIANTS} variants of the reference solution that change only the FORM of the answer \
and are still fully correct under the instruction: for example a different but equivalent number \
format (11800.0 vs 11800.00), the other identifier or spelling the instruction says is acceptable, \
a different order where the instruction leaves order free, different whitespace, or a different \
allowed option. Rules:
- Every variant must be allowed by a sentence in instruction.md. Copy that sentence exactly into \
"instruction_quote". If the instruction allows only one form, propose nothing.
- Do not change what the answer means, and do not weaken or skip any requirement.
- Make the smallest edit. Express it as exact find/replace pairs on files under solution/. \
"find" must be text that appears exactly once in that file.
- Prefer variants most likely to expose a grader that compares against one exact form.

Respond with ONLY JSON:
{{"variants": [{{"description": "what changes", "instruction_quote": "exact sentence from instruction.md", \
"edits": [{{"file": "solution/solve.sh", "find": "exact text", "replace": "new text"}}]}}],
 "reason": "if no variants, why"}}"""


BREAK_PROMPT = f"""You help find graders that are weaker than their task instruction. You are shown a \
task's instruction.md and its reference solution (solution/), which produces a correct answer.

Propose up to {MAX_BREAKS} deliberately BROKEN versions of the reference solution. Each breaks exactly one \
rule the instruction states, so a correct grader must reject it. Examples: leave an output empty or \
truncated, drop items the instruction says to include, add something the instruction forbids, leave \
one element wrong while the rest is right, weaken a check the instruction requires, skip a required \
step, or produce the right shape with wrong content. Also consider: corrupt k of n output items while the \
rest stay right (a grader that only checks an aggregate or a pooled statistic misses it); blank the content \
of one field; where the instruction restricts a library or dependency, use it anyway or embed a pre-built \
binary or generated artifact of it; for hardware or simulation tasks, produce undefined (X/Z/NaN) values.
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


EDGE_PROMPT = """You help check whether a grader tests the edge cases an expert would handle. You are \
shown a task's instruction.md, its reference solution (solution/), and edge cases the tests may not cover.

For each edge case, propose one BROKEN version of the reference solution that handles exactly that case \
wrongly and every other input as before, so a grader that tests the case must reject it. Make the smallest \
edit, as exact find/replace pairs on files under solution/; "find" must appear exactly once in that file. If \
the reference solution has no code path you can change for a case, leave that case out.

Each edge case has a number. Respond with ONLY JSON:
{"variants": [{"index": <the edge case number>, "case": "<the edge case text, copied exactly>", \
"description": "how it now mishandles the case", \
"edits": [{"file": "solution/solve.sh", "find": "exact text", "replace": "new text"}]}]}"""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


async def _edge_break_call(instruction: str, solution: str, cases: list[str]) -> dict:
    numbered = [{"index": i, "case": c} for i, c in enumerate(cases)]
    response = await get_llm_client().complete(
        [
            Message(role="system", content=EDGE_PROMPT),
            Message(
                role="user",
                content=(
                    f"=== instruction.md ===\n{instruction}\n\n=== solution/ ===\n{solution}\n\n"
                    f"=== EDGE CASES ===\n{json.dumps(numbered, indent=1)}\n"
                ),
            ),
        ],
        tools=[],
    )
    match = re.search(r"\{.*\}", response.text, re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _match_edge_breaks(data: dict, cases: list[str]) -> list[dict]:
    """Pair each proposed edit with its case by number, falling back to the case text. A model
    that rewords the case text must not lose the edit (it did, and every case showed "no edit")."""
    wanted = {_norm(c): c for c in cases}
    out: list[dict] = []
    seen: set[str] = set()
    for raw in data.get("variants") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("edits"), list):
            continue
        index = raw.get("index")
        case = cases[index] if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(cases) else None
        case = case or wanted.get(_norm(str(raw.get("case", ""))))
        if case is None or case in seen:
            continue
        seen.add(case)
        out.append(
            {
                "case": case,
                "description": str(raw.get("description", ""))[:300],
                "instruction_quote": "",
                "edits": [{k: str(e.get(k, "")) for k in ("file", "find", "replace")} for e in raw["edits"] if isinstance(e, dict)],
            }
        )
    return out


async def propose_edge_breaks(task_root: Path, cases: list[str]) -> list[dict]:
    """E.3: one deliberately wrong solution per edge case, keyed by the case text. Retried once
    when nothing usable comes back, because the answer is occasionally unusable (reworded or cut off)."""
    instruction = (task_root / "instruction.md").read_text(errors="replace")
    solution = read_text_files(task_root / "solution") if (task_root / "solution").is_dir() else ""
    for _ in range(2):
        out = _match_edge_breaks(await _edge_break_call(instruction, solution, cases), cases)
        if out or not cases:
            return out
    return []


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
