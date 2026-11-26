"""Hashed expected answers (rules for authors: docs/hash-rules.md).

Two halves, because reading code cannot show what a grader does:

* `check_hashed_answers` (static, in Static Checks): finds the `_sha256` / `_not_sha256` entries under
  tests/ and fails a task that has none. A search for plain answers is a warning only: nothing automatic can
  tell a plain answer from a test input, so "none seen" is never a promise.
* `run_proof` (needs Docker, after the Oracle passes): damages one entry at a time on a temporary copy and
  runs the reference solution. An entry that does not change the score is not used by the grader.

Neither half shows that every answer is hashed, or that a short answer cannot be guessed from its digest.
A person has to.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

_SUFFIXES = (".json", ".jsonl", ".py", ".sh", ".js", ".ts", ".toml", ".yaml", ".yml", ".txt")
_SKIP_PARTS = {"vendor", "node_modules", "__pycache__", ".git"}
_MAX_BYTES = 1_000_000

_HEX = r"[0-9a-f]{64}"
# a name ending _sha256 / _not_sha256, as a JSON key, a constant or a shell variable, then ':' or '='
_NAME_RE = re.compile(r"""(["']?)(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*?_(?:not_)?sha256)\1\s*(?:=(?!=)|:)\s*""", re.I)
_SCALAR_RE = re.compile(rf"""(["']?)({_HEX})\1(?![0-9a-f])""", re.I)
_LIST_RE = re.compile(rf"""\[\s*(?:["']{_HEX}["']\s*,?\s*)+\]""", re.I)
_PAIR_RE = re.compile(rf"""\s*,?\s*["']([^"'\n]+)["']\s*:\s*(\[\s*(?:["']{_HEX}["']\s*,?\s*)+\]|["']{_HEX}["'])""", re.I)
_ANSWER_RE = re.compile(r"expect|answer|ground_?truth|golden|reference|oracle", re.I)
DAMAGED = hashlib.sha256(b"damaged by the task evaluator").hexdigest()


@dataclass(frozen=True)
class Entry:
    file: str                       # relative to the task root
    name: str
    must_match: bool
    spans: tuple[tuple[int, int], ...]   # where each digest sits in the file's text
    digests: tuple[str, ...]


def _test_files(task_root: Path):
    tests = task_root / "tests"
    if not tests.is_dir():
        return
    for path in sorted(tests.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _SUFFIXES or _SKIP_PARTS & set(path.parts):
            continue
        if path.stat().st_size > _MAX_BYTES:
            continue
        yield path


def _digests_in(text: str, start: int, end: int) -> list[tuple[int, int]]:
    return [(start + m.start(), start + m.end()) for m in re.finditer(_HEX, text[start:end], re.I)]


def _entry(rel: str, name: str, must: bool, text: str, spans: list[tuple[int, int]]) -> Entry:
    return Entry(rel, name, must, tuple(spans), tuple(text[a:b] for a, b in spans))


def _entries_in(rel: str, text: str) -> list[Entry]:
    found: list[Entry] = []
    for m in _NAME_RE.finditer(text):
        name, pos = m.group(2), m.end()
        must = not name.lower().endswith("_not_sha256")
        if text.startswith("{", pos):                       # NAME = {"answer": "<hex>", ...}
            cursor = pos + 1
            while pair := _PAIR_RE.match(text, cursor):
                spans = _digests_in(text, pair.start(2), pair.end(2))
                found.append(_entry(rel, f"{name}.{pair.group(1)}", must, text, spans))
                cursor = pair.end()
        elif lst := _LIST_RE.match(text, pos):
            found.append(_entry(rel, name, must, text, _digests_in(text, lst.start(), lst.end())))
        elif scalar := _SCALAR_RE.match(text, pos):
            found.append(_entry(rel, name, must, text, [(scalar.start(2), scalar.end(2))]))
    return found


def find_entries(task_root: Path) -> list[Entry]:
    out: list[Entry] = []
    for path in _test_files(task_root):
        try:
            text = path.read_bytes().decode()
        except (OSError, UnicodeDecodeError):
            continue
        out.extend(_entries_in(path.relative_to(task_root).as_posix(), text))
    return out


# ---- plain answers (a warning, never a verdict) ----------------------------------------------------

def _walk(value, path: str, inside: bool, hits: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower().endswith("sha256"):
                continue
            _walk(item, f"{path}.{key}" if path else str(key), inside or bool(_ANSWER_RE.search(str(key))), hits)
    elif isinstance(value, list):
        for item in value:
            _walk(item, path + "[]", inside, hits)
    elif inside and value is not None and not isinstance(value, bool):
        hits.add(path)


def plain_answers(task_root: Path) -> list[str]:
    """Places that look like an expected answer in clear text: answer-like keys (or every value of a file named
    like one) in JSON, and answer-like module constants in Python. Inline asserts and shell strings are not read."""
    out: list[str] = []
    for path in _test_files(task_root):
        rel = path.relative_to(task_root).as_posix()
        try:
            if path.suffix == ".json":
                hits: set[str] = set()
                _walk(json.loads(path.read_text()), "", bool(_ANSWER_RE.search(path.stem)), hits)
                out.extend(f"{rel}: {key}" for key in sorted(hits))
            elif path.suffix == ".py":
                for node in ast.parse(path.read_text()).body:
                    if not (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)):
                        continue
                    name = node.targets[0].id
                    if _ANSWER_RE.search(name) and not name.lower().endswith("sha256"):
                        ast.literal_eval(node.value)   # only literals: a computed value is not a stored answer
                        out.append(f"{rel}: {name}")
        except (OSError, UnicodeDecodeError, ValueError, SyntaxError):
            continue
    return out


def _listed(items: list[str], limit: int = 8) -> str:
    return ", ".join(items[:limit]) + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


def check_hashed_answers(task_root: Path, report) -> None:
    entries = find_entries(task_root)
    if not entries:
        report.add(
            "hash_answers", "fail",
            "No hashed expected answer under tests/. Every exact expected answer must be stored as a SHA-256 digest "
            "under a name ending `_sha256` (must match) or `_not_sha256` (must not match), and the grader must compare "
            "digests (docs/hash-rules.md). A task that cannot hash needs a reviewer's exemption.",
        )
    else:
        must = sum(e.must_match for e in entries)
        report.add(
            "hash_answers", "info",
            f"{len(entries)} hashed answer(s) found: {must} must match, {len(entries) - must} must not match "
            f"({_listed(sorted({e.file for e in entries}))}). Run the hash proof to show the grader really uses them.",
        )
    plain = plain_answers(task_root)
    if plain:
        report.add(
            "tests_plain_answers", "warn",
            f"Values under tests/ that look like expected answers in clear text: {_listed(plain)}. Hash them and remove "
            "the plain copy, or confirm they are inputs. (Heuristic: a person decides.)",
        )
    else:
        report.add(
            "tests_plain_answers", "info",
            "No plain answer seen. This is not proof: inline asserts, shell strings and unusual names are not read, "
            "so a reviewer still has to confirm no answer is left in clear text.",
        )


# ---- the proof run ----------------------------------------------------------------------------------

def damaged_text(text: str, entry: Entry, digest: str) -> str:
    """The file's text with every digest of this one entry replaced."""
    for start, end in sorted(entry.spans, reverse=True):
        text = text[:start] + digest + text[end:]
    return text


async def run_proof(
    task_root: Path,
    run_oracle: Callable[[Path, int], Awaitable[float | None]],
    work: Path,
    max_runs: int = 60,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """Damage one entry at a time and run the reference solution (`run_oracle(copy_root, index)` -> reward).

    A must-match entry gets a wrong digest and the score must fall. A must-not-match entry gets the digest of a
    right answer and the score must fall; the platform cannot know which right answer it is compared against, so
    it tries the must-match digests in turn (same file first). Results per entry: used, not_used, unconfirmed
    (no right answer made it bite), no_result (no reward), not_run (run limit)."""
    entries = find_entries(task_root)
    results = [{"name": e.name, "file": e.file, "must_match": e.must_match, "result": "not_run", "note": ""} for e in entries]
    runs = 0

    def report(status: str, verdict: str, note: str = "") -> dict:
        return {"status": status, "verdict": verdict, "entries": results, "runs": runs, "total": len(entries), "note": note}

    if not entries:
        return report("failed", "no_hashes", "No `_sha256` entry under tests/, so there is nothing to prove.")

    async def score(entry: Entry, digest: str) -> float | None:
        nonlocal runs
        runs += 1
        copy_root = work / f"proof{runs}-src"
        shutil.rmtree(copy_root, ignore_errors=True)
        shutil.copytree(task_root, copy_root, symlinks=True)
        target = copy_root / entry.file
        target.write_bytes(damaged_text(target.read_bytes().decode(), entry, digest).encode())
        try:
            return await run_oracle(copy_root, runs)
        finally:
            shutil.rmtree(copy_root, ignore_errors=True)

    for entry, result in zip(entries, results):
        if runs >= max_runs:
            result["note"] = f"the limit of {max_runs} runs was reached"
            continue
        if entry.must_match:
            reward = await score(entry, DAMAGED)
            if reward is None:
                result["result"], result["note"] = "no_result", "the run gave no reward"
            elif reward < 1:
                result["result"], result["note"] = "used", "a wrong digest lowered the score"
            else:
                result["result"], result["note"] = "not_used", "the score stayed 1 with a wrong digest"
        else:
            right = [e for e in entries if e.must_match]
            right.sort(key=lambda e: e.file != entry.file)
            candidates = list(dict.fromkeys(d for e in right for d in e.digests))
            result["result"] = "unconfirmed"
            result["note"] = "no must-match digest placed here lowered the score" if candidates else "no must-match digest to try"
            for digest in candidates:
                if runs >= max_runs:
                    result["note"] = f"the limit of {max_runs} runs was reached"
                    break
                reward = await score(entry, digest)
                if reward is None:
                    result["result"], result["note"] = "no_result", "the run gave no reward"
                    break
                if reward < 1:
                    result["result"], result["note"] = "used", "a right answer's digest lowered the score"
                    break
        if progress:
            progress(report("running", ""))

    states = {r["result"] for r in results}
    if states == {"used"}:
        return report("passed", "all_used")
    if "not_used" in states:
        return report("failed", "unused_hash", "The grader does not use at least one stored digest.")
    return report("failed", "needs_review", "At least one digest could not be proven either way.")
