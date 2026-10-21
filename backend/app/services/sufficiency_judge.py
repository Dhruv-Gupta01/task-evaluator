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
4. Sometimes AUTHOR NOTES (README.md) and an AGENT IMAGE section. The agent never sees README.md. The \
AGENT IMAGE section comes from the built image and names environment/ files that never reach the agent's \
container (copied only into a build stage, or never copied): treat those as hidden, not agent-visible.

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
- [invisible] Does any rule or value the tests enforce appear only in a file the agent cannot see: \
README.md, solution/, tests/, or an environment/ file the AGENT IMAGE section says is not in the agent's \
container? Name the file. A rule stated only there is a gap even when the task's own files contain it.
- [formula] For every formula, constant, correction or conversion the expected values depend on, say \
whether the instruction states it, the agent-visible files let an expert derive it (say how), or neither. \
Only "neither" is a gap; a step an expert can derive is difficulty. If the author notes say a step is left \
unstated on purpose and explain how to derive it, weigh that explanation, but check the derivation really \
works from the agent-visible material.

- [fields] Make a table of every output field, key, column or value a test checks. For each: what the \
instruction says about it (its meaning, type, unit, allowed values), and what the test actually computes \
or compares. Report a field the instruction never defines, defines differently from the test, or leaves \
open between two meanings (for example which of two quantities a plan or score field holds).
- [docstring] Comments, docstrings and README text inside agent-visible files (starter code, templates, \
data files) are part of the instruction. Report one that contradicts what the tests require or what the \
instruction says (for example "a simpler estimator is sufficient" when the tests need the exact one).
- [numbers] List every number the tests enforce (tolerance, threshold, count, bound, rounding) and say \
whether the instruction or the visible files state it or let an expert derive it. A number a test enforces \
that is stated nowhere visible is a gap; so is one stated with a different value.
- [identifier] For every external identifier, version, accession, standard or coordinate system the task \
names, say whether the agent-visible data agree with it, and recompute simple derived values (a length, a \
count, an offset) to check them. Report data that are modified or mixed (two coordinate systems under one \
real identifier) without being said.
- [admitted] A section of TEST PHRASES may follow: lines in the tests where the author says something is \
not stated, specified or documented. Check each against the instruction; one that is really unstated is a gap.

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


GROUP_PROMPT = """Below is a numbered list of gaps reported by several runs of a task-review judge. \
Different runs word the same problem differently. Group the gaps that describe the SAME underlying \
problem (the same missing test, the same unenforced rule). Gaps about different problems stay in \
separate groups, even when they share words. Every number appears in exactly one group.

Respond with ONLY JSON: {"groups": [[0, 4, 8], [1], ...]}

GAPS:
"""


async def _group_gaps(gaps: list[str]) -> list[list[int]]:
    """Indices of `gaps` grouped by problem. One cheap judge call; falls back to
    one group per distinct text if the call fails or answers inconsistently."""
    fallback = [[i] for i in range(len(gaps))]
    if len(gaps) < 2:
        return fallback
    listing = "\n".join(f"{i}. {g}" for i, g in enumerate(gaps))
    try:
        response = await get_llm_client().complete(
            [Message(role="user", content=GROUP_PROMPT + listing)], tools=[]
        )
        match = re.search(r"\{.*\}", response.text, re.DOTALL)
        groups = json.loads(match.group(0))["groups"] if match else None
        flat = sorted(i for g in groups for i in g)
        if flat == list(range(len(gaps))):
            return [list(g) for g in groups if g]
    except Exception:
        pass
    return fallback


def _merge(verdicts: list[dict], grouping: list[list[int]] | None = None) -> dict:
    """Majority of the runs decides; every gap carries how many runs raised it.
    `grouping` (indices into the runs' gaps, in run order) merges gaps that
    different runs worded differently but that mean the same thing."""
    if len(verdicts) == 1:
        return verdicts[0]
    n = len(verdicts)
    passed_runs = sum(1 for v in verdicts if v["passed"])
    flat: list[tuple[int, str]] = [(r, g) for r, v in enumerate(verdicts) for g in dict.fromkeys(v["gaps"])]
    grouping = grouping or [[i] for i in range(len(flat))]
    merged = []
    for members in grouping:
        runs_raising = {flat[i][0] for i in members}
        text = flat[members[0]][1]
        merged.append((len(runs_raising), f"{text} (raised in {len(runs_raising)} of {n} runs)"))
    gaps = [text for _, text in sorted(merged, key=lambda m: -m[0])]
    passed = passed_runs * 2 > n
    note = f"Judge runs: {n}; {passed_runs} found no gap, {n - passed_runs} found gaps."
    if 0 < passed_runs < n:
        note += " The runs DISAGREE: treat the result as uncertain and read the gaps below."
    verdict = note + " " + verdicts[0]["verdict"] if passed == verdicts[0]["passed"] else note
    return {"passed": passed, "verdict": verdict, "gaps": gaps}


_ADMISSION_RE = re.compile(
    r"(?:instruction|task|spec(?:ification)?|prompt|readme)\s+(?:does\s+not|doesn['’]t|never|fails\s+to)\s+"
    r"(?:explicitly\s+|clearly\s+)?(?:state|specify|say|mention|document|define|describe)"
    r"|not\s+(?:explicitly\s+)?(?:specified|stated|documented|mentioned|defined)\s+in\s+the\s+(?:instruction|task|spec)"
    r"|(?:left\s+)?implicit(?:ly)?\b|undocumented|unspecified|not\s+specified\b|assum(?:e|es|ed|ing)\s+(?:that\s+)?the\s+agent",
    re.I,
)


def test_phrase_hits(task_root: Path) -> list[str]:
    """Lines under tests/ where the author admits something is unstated (a free
    signal nobody reads): "file:line: text". Plain regex, no LLM; capped."""
    tests = task_root / "tests"
    hits: list[str] = []
    if not tests.is_dir():
        return hits
    for path in sorted(p for p in tests.rglob("*") if p.is_file() and p.stat().st_size < 400_000):
        if any(part in ("node_modules", "__pycache__", ".git", "vendor") for part in path.parts):
            continue
        try:
            text = path.read_text(errors="strict")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if _ADMISSION_RE.search(line):
                hits.append(f"{path.relative_to(task_root).as_posix()}:{i}: {line.strip()[:200]}")
                if len(hits) >= 25:
                    return hits
    return hits


async def run_sufficiency_judge(task_root: Path, runs: int = 1, image_section: str = "") -> dict:
    """`runs` independent judge calls (the later ones see the sections in a
    different order); the majority decides and disagreement is reported.
    `image_section` (image_inventory.judge_section) says which environment/
    files really reach the agent's container; empty when Build recorded no
    image listing, and then every environment/ file counts as visible."""
    instruction_path = task_root / "instruction.md"
    environment_dir = task_root / "environment"
    tests_dir = task_root / "tests"

    instruction_text = instruction_path.read_text(errors="replace") if instruction_path.is_file() else ""
    environment_text = _read_text_files(environment_dir) if environment_dir.is_dir() else ""
    tests_text = _read_text_files(tests_dir) if tests_dir.is_dir() else ""
    solution_dir = task_root / "solution"
    solution_text = _read_text_files(solution_dir) if solution_dir.is_dir() else ""
    readme_path = task_root / "README.md"
    readme_text = readme_path.read_text(errors="replace")[:60_000] if readme_path.is_file() else ""

    visible = (
        "=== AGENT-VISIBLE: instruction.md ===\n"
        f"{instruction_text}\n\n"
        "=== AGENT-VISIBLE: environment/ ===\n"
        f"{environment_text}\n\n"
        f"{image_section}\n"
    )
    hidden = (
        "=== HIDDEN: tests/ (the agent never sees this) ===\n"
        f"{tests_text}\n\n"
        "=== HIDDEN: solution/ (the reference solution the Oracle runs) ===\n"
        f"{solution_text}\n"
    )
    phrases = test_phrase_hits(task_root)
    if phrases:
        hidden += "\n=== TEST PHRASES (lines in tests/ that say something is not stated) ===\n" + "\n".join(phrases) + "\n"
    if readme_text:
        hidden += f"\n=== HIDDEN: README.md (author notes; the agent never sees them) ===\n{readme_text}\n"

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
    flat = [g for v in verdicts for g in dict.fromkeys(v["gaps"])]
    result = _merge(verdicts, await _group_gaps(flat) if len(verdicts) > 1 else None)
    result["test_phrases"] = phrases
    return result
