"""LLM-as-judge sufficiency check: does the agent-visible material
(instruction.md + environment/) actually contain or make derivable every
requirement the hidden tests/ grade on? Mirrors the real platform's
task_specification / instruction-sufficiency gate. A single non-agentic
completion call — no tools, no multi-turn loop — so it's far cheaper than
an agent trial."""
import json
import re
from pathlib import Path

from app.services.judge_files import read_text_files as _read_text_files
from app.services.llm.base import Message
from app.services.llm.factory import get_llm_client

SYSTEM_PROMPT = """You are a strict instruction-sufficiency judge for an automated coding-agent \
benchmark task. You will be shown two things:

1. AGENT-VISIBLE MATERIAL — exactly what the agent sees before attempting the task: the \
instruction text and every file under environment/ (the sandbox it starts from). A FILE LISTING names every file; \
a file whose contents are not shown (binary, truncated, or over the size budget) still exists in the \
sandbox — never report it as missing.
2. HIDDEN TEST SUITE — the grading code, which the agent never sees.
3. REFERENCE SOLUTION (solution/) — what the Oracle runs; the agent never sees it.

Your job: for every discrete requirement, value, or behavior the hidden tests actually check, \
decide whether it is stated or derivable from the agent-visible material alone. Flag anything \
a test requires that is missing, ambiguous, or contradicted in the agent-visible material — \
those are genuine sufficiency gaps, not things the agent merely failed to notice.

Do not penalize the task for requiring careful reading, cross-referencing multiple files, or \
multi-step reasoning — that is legitimate difficulty, not insufficiency. Only flag information \
that is truly unavailable or unresolvable from what the agent can see.

Also work through these checks. Report a problem as a gap, starting the gap with the tag shown; \
if a check finds nothing, say nothing about it:
- [reverse] For every value, option, wording or format the instruction says is allowed or valid, \
name the test that accepts it. If a listed option would be rejected by every test, or a test \
contradicts one of its own earlier checks, report it. Report any test that compares against a \
stored golden file or one exact string when the instruction allows more than one correct form.
- [reading] At each point where an expert could reasonably choose between readings, list the \
readings and say whether the instruction rules the others out. A test that accepts only one of \
several defensible readings is a gap. Separate this from a task that is merely hard: if the \
answer follows from the given information plus standard domain knowledge, it is hard, not ambiguous.
- [hidden-rule] Where do the instruction and files state each tie-break, ordering, \
de-duplication, rounding, unit and convention a test relies on? Report any that exist only in \
the tests. If a procedure document and the checker disagree (for example on field names or \
orientation), report it.
- [stale] Compare each number or count stated in the instruction (for example "20 invoices") \
with the data; report a mismatch.
- [source] For each expected value in the tests, name the line of the spec or data that justifies \
it. Report values with no such line.
- [coverage] List every MUST / MUST NOT / keep / rather-than rule in the instruction and name the \
test that fails when it is broken. A rule no test enforces is a gap.
- [loose] How many wrong outputs could the grader still pass (a tolerance, a count threshold, a \
check on two elements only, a check on types only)? Does any test pass on an empty or unchanged \
output? Report tests that are looser than the rule they claim to enforce.
- [trust] Does any test read a result or flag from a file the agent can write? Does the grader \
measure the real outcome or a modelled shortcut, and does it re-implement the task's own code \
without a test tying the two together?
- [fixed-input] Are the test inputs fixed and visible to the agent, so a hard-coded answer \
would pass?
- [oracle] Does the reference solution compute its output from the inputs, or does it read from \
tests/, embed the expected output, or copy a ground-truth file? Only the latter is a gap.

Respond with ONLY a JSON object, no other text, in exactly this shape:
{
  "passed": true or false,
  "verdict": "one paragraph summary",
  "gaps": ["specific gap 1", "specific gap 2", ...]
}
"passed" must be false if gaps is non-empty, true only if you found no genuine gap."""


def _parse_verdict(text: str) -> dict:
    stripped = text.strip()
    # tolerate a markdown code fence around the JSON
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if match:
        stripped = match.group(0)
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return {
            "passed": False,
            "verdict": "Judge response was not valid JSON; treating as inconclusive/failed.",
            "gaps": [f"Unparseable judge output: {text[:2000]}"],
        }
    return {
        "passed": bool(data.get("passed", False)),
        "verdict": str(data.get("verdict", "")),
        "gaps": [str(g) for g in data.get("gaps", [])] if isinstance(data.get("gaps"), list) else [],
    }


def _merge(verdicts: list[dict]) -> dict:
    """Majority of the runs decides; every gap carries how many runs raised it."""
    if len(verdicts) == 1:
        return verdicts[0]
    passed_runs = sum(1 for v in verdicts if v["passed"])
    n = len(verdicts)
    counts: dict[str, int] = {}
    for v in verdicts:
        for gap in dict.fromkeys(v["gaps"]):
            counts[gap] = counts.get(gap, 0) + 1
    gaps = [f"{gap} (raised in {c} of {n} runs)" for gap, c in sorted(counts.items(), key=lambda kv: -kv[1])]
    passed = passed_runs * 2 > n
    note = f"Judge runs: {n}; {passed_runs} found no gap, {n - passed_runs} found gaps."
    if 0 < passed_runs < n:
        note += " The runs DISAGREE: treat the result as uncertain and read the gaps below."
    verdict = note + " " + verdicts[0]["verdict"] if passed == verdicts[0]["passed"] else note
    return {"passed": passed, "verdict": verdict, "gaps": gaps}


async def run_sufficiency_judge(task_root: Path, runs: int = 1) -> dict:
    """`runs` independent judge calls (the later ones see the sections in a
    different order); the majority decides and disagreement is reported."""
    instruction_path = task_root / "instruction.md"
    environment_dir = task_root / "environment"
    tests_dir = task_root / "tests"

    instruction_text = instruction_path.read_text(errors="replace") if instruction_path.is_file() else ""
    environment_text = _read_text_files(environment_dir) if environment_dir.is_dir() else ""
    tests_text = _read_text_files(tests_dir) if tests_dir.is_dir() else ""
    solution_dir = task_root / "solution"
    solution_text = _read_text_files(solution_dir) if solution_dir.is_dir() else ""

    visible = (
        "=== AGENT-VISIBLE: instruction.md ===\n"
        f"{instruction_text}\n\n"
        "=== AGENT-VISIBLE: environment/ ===\n"
        f"{environment_text}\n\n"
    )
    hidden = (
        "=== HIDDEN: tests/ (the agent never sees this) ===\n"
        f"{tests_text}\n\n"
        "=== HIDDEN: solution/ (the reference solution the Oracle runs) ===\n"
        f"{solution_text}\n"
    )

    client = get_llm_client()
    verdicts = []
    for i in range(max(1, min(runs, 3))):
        user_content = visible + hidden if i % 2 == 0 else hidden + visible
        response = await client.complete(
            [
                Message(role="system", content=SYSTEM_PROMPT),
                Message(role="user", content=user_content),
            ],
            tools=[],
        )
        verdicts.append(_parse_verdict(response.text))
    return _merge(verdicts)
