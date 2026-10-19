"""Edge-case generator (4.0 category B): are the obvious cases tested?

Call 1 sees only instruction.md and lists edge cases an expert would handle.
Call 2 sees those cases and the tests and says, for each, whether a test would
fail an answer that mishandles it. The first call never sees the tests, so it
cannot list only what the tests already cover."""

import json
import re
from pathlib import Path

from app.services.judge_files import read_text_files
from app.services.llm.base import Message
from app.services.llm.factory import get_llm_client

MAX_CASES = 10

LIST_PROMPT = f"""You are an expert reviewing a task for a coding-agent benchmark. From the instruction \
alone, list up to {MAX_CASES} edge cases that a careful solver must handle correctly: empty or missing \
input, duplicates, boundary values, ordering and ties, malformed or unusual input, units and rounding, \
cases the instruction explicitly warns about. Only include cases that apply to this instruction and \
have a clear correct outcome under it.

Respond with ONLY JSON: {{"cases": [{{"case": "short description", "expected": "what a correct solution does"}}]}}"""

COVER_PROMPT = """You are checking test coverage. You get a list of edge cases and a task's hidden tests. \
For each case decide whether some test would FAIL a solution that mishandles it. A test that only \
runs on inputs not exercising the case does not cover it.

Respond with ONLY JSON: {"cases": [{"case": "<same text>", "covered": true or false, \
"test": "name of the covering test or null", "reason": "one sentence"}]}"""


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
    client = get_llm_client()

    listed = _json(
        (
            await client.complete(
                [Message(role="system", content=LIST_PROMPT), Message(role="user", content=instruction)],
                tools=[],
            )
        ).text
    )
    cases = [c for c in (listed.get("cases") or []) if isinstance(c, dict) and c.get("case")][:MAX_CASES]
    if not cases:
        return {"cases": [], "error": "the model listed no edge cases"}

    checked = _json(
        (
            await client.complete(
                [
                    Message(role="system", content=COVER_PROMPT),
                    Message(
                        role="user",
                        content=f"=== EDGE CASES ===\n{json.dumps(cases, indent=1)}\n\n=== TESTS ===\n{tests}",
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
            }
        )
    return {"cases": out}
