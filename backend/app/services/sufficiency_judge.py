"""LLM-as-judge sufficiency check: does the agent-visible material
(instruction.md + environment/) actually contain or make derivable every
requirement the hidden tests/ grade on? Mirrors the real platform's
task_specification / instruction-sufficiency gate. A single non-agentic
completion call — no tools, no multi-turn loop — so it's far cheaper than
an agent trial."""
import json
import re
from pathlib import Path

from app.services.llm.base import Message
from app.services.llm.factory import get_llm_client

MAX_FILE_CHARS = 20_000
MAX_TOTAL_CHARS = 120_000

_TEXT_EXTENSIONS = {
    ".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".py", ".sh", ".pl",
    ".csv", ".cfg", ".ini", ".rego",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp",
    ".java", ".go", ".rb", ".rs", ".sql",
}

# Files conventionally named without an extension (path.suffix == "") that
# still need to be visible to the judge — a Makefile or Dockerfile can be
# exactly where a real requirement (a build flag, an install step) lives.
_TEXT_FILENAMES = {"Makefile", "makefile", "Dockerfile", "dockerfile"}

SYSTEM_PROMPT = """You are a strict instruction-sufficiency judge for an automated coding-agent \
benchmark task. You will be shown two things:

1. AGENT-VISIBLE MATERIAL — exactly what the agent sees before attempting the task: the \
instruction text and every file under environment/ (the sandbox it starts from).
2. HIDDEN TEST SUITE — the grading code, which the agent never sees.

Your job: for every discrete requirement, value, or behavior the hidden tests actually check, \
decide whether it is stated or derivable from the agent-visible material alone. Flag anything \
a test requires that is missing, ambiguous, or contradicted in the agent-visible material — \
those are genuine sufficiency gaps, not things the agent merely failed to notice.

Do not penalize the task for requiring careful reading, cross-referencing multiple files, or \
multi-step reasoning — that is legitimate difficulty, not insufficiency. Only flag information \
that is truly unavailable or unresolvable from what the agent can see.

Respond with ONLY a JSON object, no other text, in exactly this shape:
{
  "passed": true or false,
  "verdict": "one paragraph summary",
  "gaps": ["specific gap 1", "specific gap 2", ...]
}
"passed" must be false if gaps is non-empty, true only if you found no genuine gap."""


def _read_text_files(root: Path, max_total: int = MAX_TOTAL_CHARS) -> str:
    """Walk root, read every plausibly-text file, concatenate with path
    headers. Skips binaries and anything over MAX_FILE_CHARS individually;
    stops once max_total is reached across all files combined."""
    chunks: list[str] = []
    total = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in _TEXT_EXTENSIONS and path.name not in _TEXT_FILENAMES:
            continue
        try:
            content = path.read_text(errors="replace")
        except OSError:
            continue
        if len(content) > MAX_FILE_CHARS:
            content = content[:MAX_FILE_CHARS] + "\n... [truncated]"
        rel = path.relative_to(root)
        chunk = f"--- {rel} ---\n{content}\n"
        if total + len(chunk) > max_total:
            chunks.append(f"... [remaining files omitted, budget reached]")
            break
        chunks.append(chunk)
        total += len(chunk)
    return "\n".join(chunks)


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


async def run_sufficiency_judge(task_root: Path) -> dict:
    instruction_path = task_root / "instruction.md"
    environment_dir = task_root / "environment"
    tests_dir = task_root / "tests"

    instruction_text = instruction_path.read_text(errors="replace") if instruction_path.is_file() else ""
    environment_text = _read_text_files(environment_dir) if environment_dir.is_dir() else ""
    tests_text = _read_text_files(tests_dir) if tests_dir.is_dir() else ""

    user_content = (
        "=== AGENT-VISIBLE: instruction.md ===\n"
        f"{instruction_text}\n\n"
        "=== AGENT-VISIBLE: environment/ ===\n"
        f"{environment_text}\n\n"
        "=== HIDDEN: tests/ (the agent never sees this) ===\n"
        f"{tests_text}\n"
    )

    client = get_llm_client()
    response = await client.complete(
        [
            Message(role="system", content=SYSTEM_PROMPT),
            Message(role="user", content=user_content),
        ],
        tools=[],
    )
    return _parse_verdict(response.text)
