"""Can the agent find the answer in the files it is given (4.0 item 61, 4.1
category C)?

Plain file comparison, no LLM, no Docker. It looks at what the agent can read
(environment/, instruction.md and, when Build recorded one, the file list of
the built image) for:
- names that suggest answers (expected*, golden*, *.orig, *.bak, .git);
- exact copies of files from solution/ or tests/;
- distinctive values the tests assert (long numbers, hashes, long strings).

Advisory and noisy by design: a test input is supposed to be visible, so an
exact copy of a tests/ input or a value that is really an input will show up.
Every finding names the files so a human can tell."""

import hashlib
import re
from pathlib import Path

from app.services.hygiene_checks import _own_files

MAX_FINDINGS = 40
MIN_COPY_BYTES = 64
_MAX_SCAN_BYTES = 2_000_000

_ANSWER_NAME_RE = re.compile(
    r"expected|golden|answer|ground[_-]?truth|solution|oracle|reference[_-]?(?:out|answer|result)|"
    r"\.orig$|\.bak$|\.rej$|~$",
    re.I,
)
_HARMLESS_HIDDEN = {".dockerignore", ".gitignore", ".gitkeep", ".gitattributes", ".editorconfig"}

_LONG_NUMBER_RE = re.compile(r"(?<![\w.])-?(?:\d+\.\d{5,}|\d{7,})(?![\w.])")
_HASH_RE = re.compile(r"\b[0-9a-f]{20,}\b")
_COMPARED_STRING_RE = re.compile(r"(?:==|!=|\bin)\s*[rbu]?([\"'])([^\"'\n]{14,200})\1|\b[A-Z][A-Z0-9_]*\s*=\s*[rbu]?([\"'])([^\"'\n]{14,200})\3")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: Path) -> str | None:
    if path.stat().st_size > _MAX_SCAN_BYTES:
        return None
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def _asserted_values(tests: Path) -> dict[str, str]:
    """value -> where in tests/ it is compared against."""
    values: dict[str, str] = {}
    for path in _own_files(tests):
        if path.suffix not in (".py", ".sh", ".js", ".ts", ".json", ".txt", ".csv"):
            continue
        text = _read(path)
        if text is None:
            continue
        rel = path.relative_to(tests.parent).as_posix()
        code = path.suffix in (".py", ".sh", ".js", ".ts")
        for i, line in enumerate(text.splitlines(), 1):
            if code and not re.search(r"assert|expect|==|EXPECTED|\bin\b", line):
                continue
            found = _LONG_NUMBER_RE.findall(line) + _HASH_RE.findall(line)
            if code:
                for m in _COMPARED_STRING_RE.finditer(line):
                    found.append(m.group(2) or m.group(4))
            for value in found:
                # A bare identifier (an op or field name) is vocabulary the agent needs, not an answer.
                if value.startswith("/") or len(set(value)) <= 3 or re.fullmatch(r"[A-Za-z_][\w.-]*", value):
                    continue
                values.setdefault(value, f"{rel}:{i}")
    return values


def scan(task_root: Path, image_files: list[str] | None = None) -> dict:
    env = task_root / "environment"
    visible = [p for p in _own_files(env)] if env.is_dir() else []
    findings: list[dict] = []

    def add(severity: str, kind: str, message: str, files: list[str]) -> None:
        if len(findings) < MAX_FINDINGS:
            findings.append({"severity": severity, "kind": kind, "message": message, "files": files[:10]})

    # Names that suggest an answer, in environment/ and in the built image.
    named = [p.relative_to(task_root).as_posix() for p in visible if _ANSWER_NAME_RE.search(p.name)]
    image_named = [f for f in image_files or [] if _ANSWER_NAME_RE.search(f.rsplit("/", 1)[-1])]
    if named or image_named:
        add(
            "warn", "answer_like_name",
            "Files the agent can read have names that suggest expected outputs or a solution; check what they hold.",
            named + [f"image: {f}" for f in image_named],
        )
    git_dirs = sorted({
        p.relative_to(task_root).as_posix().split("/.git/")[0] + "/.git"
        for p in (env.rglob("*") if env.is_dir() else []) if "/.git/" in p.as_posix()
    }) + [f"image: {f}" for f in image_files or [] if f.endswith("/.git") or "/.git/" in f][:1]
    if git_dirs:
        add("warn", "git_history", "A .git folder is visible to the agent; its history can hold the solution or the original code.", git_dirs)
    hidden = [
        p.relative_to(task_root).as_posix() for p in visible
        if p.name.startswith(".") and p.name not in _HARMLESS_HIDDEN and "/.git/" not in p.as_posix()
    ]
    if hidden:
        add("info", "hidden_files", "Hidden files in environment/; make sure none carries answers or keys.", hidden)

    # Exact copies of solution/ and tests/ files.
    by_digest: dict[str, list[str]] = {}
    for p in visible:
        if p.stat().st_size >= MIN_COPY_BYTES and p.stat().st_size <= _MAX_SCAN_BYTES:
            by_digest.setdefault(_digest(p), []).append(p.relative_to(task_root).as_posix())
    for folder in ("solution", "tests"):
        for p in _own_files(task_root / folder):
            if p.stat().st_size < MIN_COPY_BYTES or p.stat().st_size > _MAX_SCAN_BYTES:
                continue
            copies = by_digest.get(_digest(p))
            if not copies:
                continue
            rel = p.relative_to(task_root).as_posix()
            tail = rel.split("/", 1)[1]
            same_place = ("/" in tail or p.name == "__init__.py") and all(c.endswith("/" + tail) for c in copies)
            if folder == "solution" and same_place:
                add("info", "unchanged_starter_file", f"{rel} is the starter file at the same path, unchanged.", copies)
            elif folder == "solution":
                add("warn", "solution_copy", f"{rel} is copied unchanged into environment/, where the agent can read it.", copies)
            elif _ANSWER_NAME_RE.search(p.name) or re.search(r"output|result", p.name, re.I):
                add("warn", "expected_output_copy", f"{rel} looks like an expected output and is copied unchanged into environment/.", copies)
            else:
                add("info", "test_input_copy", f"{rel} also exists in environment/ (fine for an input the agent should see).", copies)

    # Values the tests compare against that also appear in agent-visible text.
    values = _asserted_values(task_root / "tests") if (task_root / "tests").is_dir() else {}
    corpus: list[tuple[str, str]] = []
    instruction = task_root / "instruction.md"
    if instruction.is_file():
        corpus.append(("instruction.md", instruction.read_text(errors="replace")))
    for p in visible:
        text = _read(p)
        if text is not None:
            corpus.append((p.relative_to(task_root).as_posix(), text))
    for value, where in values.items():
        hits = [name for name, text in corpus if value in text]
        if hits:
            add(
                "warn" if not any(h == "instruction.md" for h in hits) else "info",
                "asserted_value_visible",
                f"`{value[:60]}`, which {where} checks for, also appears in files the agent can read. It may be an "
                "input or a value the instruction gives on purpose; if it is the answer, the task leaks it.",
                hits,
            )

    return {
        "findings": findings,
        "warn_count": sum(1 for f in findings if f["severity"] == "warn"),
        "values_checked": len(values),
        "files_scanned": len(visible),
        "image_listing_used": image_files is not None,
    }
