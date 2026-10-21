"""Edge-case generator (4.0 category B): are the obvious cases tested?

Call 1 sees only instruction.md and lists edge cases an expert would handle.
Call 2 sees those cases and the tests and says, for each, whether a test would
fail an answer that mishandles it. The first call never sees the tests, so it
cannot list only what the tests already cover."""

import json
import re
from pathlib import Path

from app.services.hygiene_checks import _named_paths, _resolve, _text_files
from app.services.judge_files import read_text_files
from app.services.llm.base import Message
from app.services.llm.factory import get_llm_client

MAX_CASES = 10
MAX_CASES_WITH_SPEC = 16
_SPEC_NAME = re.compile(r"format|spec|schema|grammar|protocol|contract|convention|legend|codes?\b|reference", re.I)
_MAX_SPEC_FILES = 3
_MAX_SPEC_CHARS = 40_000
_SPEC_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".csv", ".tsv", ".xml", ".rst", ".cfg"}


def spec_files(task_root: Path) -> list[tuple[str, str]]:
    """(relative path, text) of the agent-visible files that define what an answer may contain:
    files the instruction names, plus environment/ files named like a format or spec. The
    options such a file lists (15 adducts, every status code) are edge cases too."""
    env = task_root / "environment"
    chosen: dict[Path, None] = {}
    try:
        instruction = (task_root / "instruction.md").read_text(errors="replace")
    except OSError:
        instruction = ""
    for ref in _named_paths(instruction):
        hit = _resolve(task_root, ref)
        if hit is not None and env in hit.parents and hit.suffix.lower() in _SPEC_SUFFIXES:
            chosen[hit] = None
    for path, _ in _text_files(env):
        if len(chosen) >= _MAX_SPEC_FILES:
            break
        if path.suffix.lower() in _SPEC_SUFFIXES and _SPEC_NAME.search(path.name):
            chosen[path] = None
    out = []
    for path in list(chosen)[:_MAX_SPEC_FILES]:
        try:
            out.append((path.relative_to(task_root).as_posix(), path.read_text(errors="replace")[:_MAX_SPEC_CHARS]))
        except OSError:
            continue
    return out

LIST_PROMPT = f"""You are an expert reviewing a task for a coding-agent benchmark. From the instruction \
alone, list up to {MAX_CASES} edge cases that a careful solver must handle correctly: empty or missing \
input, duplicates, boundary values, ordering and ties, malformed or unusual input, units and rounding, \
cases the instruction explicitly warns about. Only include cases that apply to this instruction and \
have a clear correct outcome under it.

If SPEC FILES follow the instruction, they define what an answer may contain (an output format, a list of \
codes, allowed values). Treat every option, value, variant or field such a file says is allowed as something \
a correct solution must handle: list the ones most likely to be mishandled (up to {MAX_CASES_WITH_SPEC} cases in \
all), and start the case with "(from <file name>)".

Respond with ONLY JSON: {{"cases": [{{"case": "short description", "expected": "what a correct solution does"}}]}}"""

COVER_PROMPT = """You are checking test coverage. You get a list of edge cases and a task's hidden tests. \
For each case decide whether some test would FAIL a solution that mishandles it. A test that only \
runs on inputs not exercising the case does not cover it.

You also get the REFERENCE SOLUTION. For each case also say whether the reference solution itself handles \
it ("reference_handles"): a case the reference does not handle is one nobody has shown can be solved.

Respond with ONLY JSON: {"cases": [{"case": "<same text>", "covered": true or false, \
"test": "name of the covering test or null", "reference_handles": true or false, "reason": "one sentence"}]}"""


def _json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


async def run_edge_case_check(task_root: Path) -> dict:
    instruction = (task_root / "instruction.md").read_text(errors="replace")
    tests = read_text_files(task_root / "tests") if (task_root / "tests").is_dir() else ""
    solution = read_text_files(task_root / "solution") if (task_root / "solution").is_dir() else ""
    specs = spec_files(task_root)
    spec_text = "".join(f"\n\n=== SPEC FILE: {rel} ===\n{text}" for rel, text in specs)
    limit = MAX_CASES_WITH_SPEC if specs else MAX_CASES
    client = get_llm_client()

    listed = _json(
        (
            await client.complete(
                [Message(role="system", content=LIST_PROMPT), Message(role="user", content=instruction + spec_text)],
                tools=[],
            )
        ).text
    )
    cases = [c for c in (listed.get("cases") or []) if isinstance(c, dict) and c.get("case")][:limit]
    if not cases:
        return {"cases": [], "error": "the model listed no edge cases"}

    checked = _json(
        (
            await client.complete(
                [
                    Message(role="system", content=COVER_PROMPT),
                    Message(
                        role="user",
                        content=(
                            f"=== EDGE CASES ===\n{json.dumps(cases, indent=1)}\n\n=== TESTS ===\n{tests}"
                            f"\n\n=== REFERENCE SOLUTION ===\n{solution}"
                        ),
                    ),
                ],
                tools=[],
            )
        ).text
    )
    by_case = {str(c.get("case")): c for c in (checked.get("cases") or []) if isinstance(c, dict)}
    out = []
    for c in cases:
        verdict = by_case.get(str(c["case"]), {})
        out.append(
            {
                "case": str(c["case"])[:300],
                "expected": str(c.get("expected", ""))[:300],
                "covered": bool(verdict.get("covered")),
                "test": verdict.get("test"),
                "reason": str(verdict.get("reason", "not assessed"))[:300],
                # False when the Oracle itself does not handle the case: nobody has shown it can be solved.
                "reference_handles": verdict.get("reference_handles") if isinstance(verdict.get("reference_handles"), bool) else None,
            }
        )
    return {"cases": out, "spec_files": [rel for rel, _ in specs]}
