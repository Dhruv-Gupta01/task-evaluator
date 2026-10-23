"""Can the agent find the answer in the files it is given (4.0 item 61, 4.1
category C)?

Plain file comparison, no LLM, no Docker. It looks at what the agent can read
(environment/, instruction.md and, when Build recorded one, the file list of
the built image) for:
- names that suggest answers (expected*, golden*, *.orig, *.bak, .git);
- exact copies of files from solution/ or tests/;
- distinctive values the tests assert (long numbers, hashes, long strings);
- symlinks in environment/ (a link into tests/ or solution/ hands the agent the answer;
  the extractor keeps a note of them in zip_entries.json, see utils/ziputil);
- version and commit banners that point to the public source of the task or its answer;
- file timestamps that stand out (dated in the future, or years older than the rest of
  environment/, as a file copied in from somewhere else would be).

Advisory and noisy by design: a test input is supposed to be visible, so an
exact copy of a tests/ input or a value that is really an input will show up.
Every finding names the files so a human can tell."""

import hashlib
import posixpath
import re
import statistics
import time
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
_BANNER_RE = re.compile(
    r"(?i)\b(?:git[_ -]?(?:commit|sha|hash|rev(?:ision)?)|commit(?:[_ -]?(?:sha|hash|id))?|revision|build[_ -]?id)\b[\"']?\s*[:=]\s*[\"']?[0-9a-f]{7,40}\b"
    r"|\bbuilt\s+from\s+(?:commit|tag|branch)\b"
    r"|github\.com/[\w.-]+/[\w.-]+/(?:tree|blob|commit)/[0-9a-f]{7,40}"
)
_TEXT_BANNER_SUFFIXES = {".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".cfg", ".ini", ".py", ".sh", ".js", ".ts", ".rs", ".go", ".c", ".h", ".html", ""}
_ZIP_UNSET_BEFORE = 347155200  # 1981-01-01: a zip without real dates stores 1980-01-01
_OLD_AGAINST_MEDIAN_DAYS = 365
_MIN_FILES_FOR_TIMESTAMP_OUTLIER = 5


def _zip_rel(name: str) -> str | None:
    """A zip entry name from environment/ on (the task may sit under one top folder)."""
    for folder in ("environment", "tests", "solution"):
        if name.startswith(folder + "/") or ("/" + folder + "/") in name:
            return folder + "/" + name.split(folder + "/", 1)[1]
    return None


def _symlink_findings(entries: list[dict]) -> list[tuple[str, str, str, list[str]]]:
    out: list[tuple[str, str, str, list[str]]] = []
    into_answer: list[str] = []
    other: list[str] = []
    for e in entries:
        target = e.get("symlink")
        rel = _zip_rel(e["name"]) if target else None
        if not rel or not rel.startswith("environment/"):
            continue
        resolved = target if target.startswith("/") else posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
        parts = resolved.strip("/").split("/")
        label = f"{rel} -> {target}"
        if "tests" in parts or "solution" in parts:
            into_answer.append(label)
        else:
            other.append(label)
    if into_answer:
        out.append((
            "warn", "symlink_to_answer",
            "A symlink in environment/ points into tests/ or solution/. The agent's container may follow it to the "
            "answer; the link target text is all this scan can see, so check what it resolves to in the image.",
            into_answer,
        ))
    if other:
        out.append((
            "info", "symlink_in_environment",
            "Symlinks in environment/. Fine when they link inside the task; check none reaches a path the agent "
            "should not read.",
            other,
        ))
    return out


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


def scan(task_root: Path, image_files: list[str] | None = None, zip_entries: list[dict] | None = None) -> dict:
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

    # Symlinks and timestamps: the extractor loses both, so they come from the zip's own entry list
    # (None for an upload from before it was recorded, in which case nothing is reported).
    if zip_entries:
        for severity, kind, message, files in _symlink_findings(zip_entries):
            add(severity, kind, message, files)
        env_times = [
            (rel, e["mtime"]) for e in zip_entries
            if "mtime" in e and not e.get("symlink") and (rel := _zip_rel(e["name"])) and rel.startswith("environment/")
            and e["mtime"] >= _ZIP_UNSET_BEFORE
        ]
        future = [rel for rel, t in env_times if t > time.time() + 86400]
        if future:
            add("info", "future_timestamp", "Files in environment/ are dated in the future; the clock of the machine that built the task was off, or the dates were set by hand.", future)
        if len(env_times) >= _MIN_FILES_FOR_TIMESTAMP_OUTLIER:
            median = statistics.median(t for _, t in env_times)
            old = [rel for rel, t in env_times if median - t > _OLD_AGAINST_MEDIAN_DAYS * 86400]
            if old:
                add(
                    "info", "old_timestamp",
                    f"These environment/ files are more than {_OLD_AGAINST_MEDIAN_DAYS} days older than the rest. They may have "
                    "been copied in from somewhere else (an upstream repository, a published solution); check where they came from.",
                    old,
                )

    # Version and commit banners that point to a public source.
    banners = []
    for p in visible:
        if p.suffix.lower() in _TEXT_BANNER_SUFFIXES and p.name != "Dockerfile":
            text = _read(p)
            if text and (m := _BANNER_RE.search(text)):
                banners.append(f"{p.relative_to(task_root).as_posix()}: {m.group(0)[:80]}")
    if banners:
        add("info", "version_banner", "Files in environment/ carry a commit, revision or source URL. Someone could use it to find the task's public origin or the answer in its history.", banners)

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
