"""Leakage-artifact scan: flags tell-tale phrases that only appear in raw,
unedited LLM chat output — assistant meta-commentary, disclaimers, unfilled
template placeholders. This is deliberately NOT a general "was this
AI-written" detector: that category of tool has real, documented
false-positive rates and isn't reliable enough to gate a real candidate's
submission on. This check answers a narrower, much safer question instead —
did whoever submitted this even read what they pasted in — and is advisory
only. A match here is worth a human look, not an automatic rejection."""
import re
from dataclasses import dataclass
from pathlib import Path

MAX_FILE_CHARS = 200_000

_TEXT_EXTENSIONS = {
    ".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".py", ".sh", ".pl",
    ".csv", ".cfg", ".ini", ".rego",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp",
    ".java", ".go", ".rb", ".rs", ".sql",
}
_TEXT_FILENAMES = {"Makefile", "makefile", "Dockerfile", "dockerfile"}

# Every pattern here is a near-unique fingerprint of raw, unedited LLM chat
# output — phrasing a human author of their own work would not naturally
# produce by accident. Kept narrow and specific on purpose: a broad/generic
# phrase (e.g. "in summary") would flag ordinary technical writing and defeat
# the whole point of keeping the false-positive rate low.
_LEAKAGE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"as an ai (language model|assistant)", re.I), "assistant self-reference"),
    (re.compile(r"i(?:'m| am) (?:just )?an ai\b", re.I), "assistant self-reference"),
    (re.compile(r"i (?:don't|do not) have (?:access to|the ability to) (?:real-time|the internet|browsing)", re.I), "assistant capability disclaimer"),
    (re.compile(r"i cannot browse the internet", re.I), "assistant capability disclaimer"),
    (re.compile(r"as of my (?:last )?(?:knowledge cutoff|training data)", re.I), "assistant knowledge-cutoff disclaimer"),
    (re.compile(r"i'm sorry,? but (?:as an ai|i cannot|i can't)", re.I), "assistant refusal/apology"),
    (re.compile(r"i apologize for (?:any confusion|the (?:inconvenience|error))", re.I), "assistant apology"),
    (re.compile(r"^(?:certainly|sure)[!,] here'?s?\b", re.I | re.M), "chat-opener leaked into file"),
    (re.compile(r"i hope this helps[!.]?\s*$", re.I | re.M), "chat sign-off leaked into file"),
    (re.compile(r"(?:let me know|feel free to (?:ask|reach out)) if you (?:have any (?:questions|concerns)|need (?:any )?(?:further|more) (?:help|assistance|clarification))", re.I), "chat sign-off leaked into file"),
    (re.compile(r"\[(?:your name|insert [^\]]{1,40}|company name|todo:?\s*fill[^\]]*)\]", re.I), "unfilled template placeholder"),
    (re.compile(r"\{\{\s*your[_ ][a-z_]+\s*\}\}", re.I), "unfilled template placeholder"),
    (re.compile(r"as a large language model", re.I), "assistant self-reference"),
]


@dataclass
class Finding:
    file: str
    line: int
    pattern: str
    excerpt: str


def _iter_text_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in _TEXT_EXTENSIONS and path.name not in _TEXT_FILENAMES:
            continue
        yield path


def _scan_file(path: Path, root: Path) -> list[Finding]:
    try:
        content = path.read_text(errors="replace")
    except OSError:
        return []
    if len(content) > MAX_FILE_CHARS:
        content = content[:MAX_FILE_CHARS]

    findings: list[Finding] = []
    rel = str(path.relative_to(root))
    for pattern, label in _LEAKAGE_PATTERNS:
        for match in pattern.finditer(content):
            line_no = content.count("\n", 0, match.start()) + 1
            line_start = content.rfind("\n", 0, match.start()) + 1
            line_end = content.find("\n", match.start())
            if line_end == -1:
                line_end = len(content)
            excerpt = content[line_start:line_end].strip()
            if len(excerpt) > 160:
                excerpt = excerpt[:160] + "…"
            findings.append(Finding(file=rel, line=line_no, pattern=label, excerpt=excerpt))
    return findings


def run_leakage_scan(task_root: Path) -> dict:
    """Synchronous, no LLM call — just a regex pass over every text file in
    the submission. Returns {"passed": bool, "findings": [...], "verdict": str}.
    "passed" here means "clean" (no leakage found); this is advisory, not a
    gate — task_runner records it as its own Run so it's visible without
    blocking any other stage."""
    all_findings: list[Finding] = []
    for path in _iter_text_files(task_root):
        all_findings.extend(_scan_file(path, task_root))

    if not all_findings:
        return {
            "passed": True,
            "verdict": "No LLM leakage artifacts found in submitted text/code files.",
            "findings": [],
        }

    lines = [
        f"{f.file}:{f.line} — {f.pattern} — \"{f.excerpt}\""
        for f in all_findings
    ]
    return {
        "passed": False,
        "verdict": (
            f"Found {len(all_findings)} phrase(s) consistent with unedited LLM chat output. "
            "This is advisory only, not proof of misconduct — verify manually before acting on it."
        ),
        "findings": lines,
    }
