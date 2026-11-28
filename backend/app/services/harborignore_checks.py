"""Static Check `harborignore`: dev-only files in the verifier and oracle sandboxes, and rules that
take away something the task needs.

Harbor copies `tests/` into the verifier sandbox and `solution/` for the oracle, minus whatever a
`.harborignore` there excludes (see `harborignore.py`). Authoring tooling left in `tests/` (a
generator that can rebuild the expected answers, a maintainer script) is readable by any agent
code the verifier runs, so the Harbor team's hardening note asks to keep it out of the sandbox.
This check, all advisory and with no Docker or LLM:

  * fail  a rule excludes `tests/test.sh` or `solution/solve.sh` (Harbor refuses to load the task);
  * warn  a rule excludes a file another kept file in the task still mentions, so the verifier or
          the oracle will not find it;
  * warn  a file in `tests/` that looks like authoring tooling is copied into the verifier sandbox
          and nothing in the task uses it (info for `solution/`, which only the oracle sees);
  * info  what the rules keep out, so a reviewer can see what the verifier really gets.

Whether a file is "dev tooling" is a name heuristic (generator-style folders and script names), and
"nothing uses it" is a text search for its name, so both are evidence for a human, never proof.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from app.services.harborignore import IGNORE_FILENAME, TaskIgnore

_DEV_DIRS = {"generator", "generators", "authoring", "maintainer", "dev", "devtools"}
_DEV_FILE = re.compile(
    r"^(gen|generate|generator|make|build|create)[_-][\w.-]*\.(py|sh|js|ts)$|[_-](gen|generate|generator)\.(py|sh|js|ts)$",
    re.IGNORECASE,
)
_ENTRYPOINT = {"tests": "test.sh", "solution": "solve.sh"}
_SKIP_PARTS = {"__pycache__", ".pytest_cache", ".git"}
_MAX_TEXT_BYTES = 400_000
_MAX_CORPUS_BYTES = 6_000_000
_MAX_LISTED = 10


def _scope_roots(task_root: Path) -> list[tuple[str, Path]]:
    """(scope name, folder) for every place Harbor reads `.harborignore` rules."""
    roots = [(name, task_root / name) for name in ("tests", "solution")]
    steps = task_root / "steps"
    if steps.is_dir():
        for step in sorted(p for p in steps.iterdir() if p.is_dir()):
            roots += [(name, step / name) for name in ("tests", "solution")]
    return [(n, p) for n, p in roots if p.is_dir()]


def _files(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_PARTS]
        out += [Path(dirpath) / f for f in filenames if f != IGNORE_FILENAME]
    return sorted(out)


def _text(path: Path) -> str:
    try:
        if path.stat().st_size > _MAX_TEXT_BYTES or path.is_symlink():
            return ""
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _mentions(corpus: dict[Path, str], needle: str, exclude: set[Path]) -> Path | None:
    """The first kept file whose text names `needle` as a whole token (not as part of a longer name)."""
    pattern = re.compile(r"(?<![\w.-])" + re.escape(needle) + r"(?![\w-])")
    for path, text in corpus.items():
        if path not in exclude and pattern.search(text):
            return path
    return None


def _dev_like(rel_in_scope: Path) -> str | None:
    """Why this path looks like authoring tooling, or None."""
    for part in rel_in_scope.parts[:-1]:
        if part.lower() in _DEV_DIRS:
            return f"it sits in a `{part}/` folder"
    if _DEV_FILE.search(rel_in_scope.name):
        return f"`{rel_in_scope.name}` is named like a generator or build script"
    return None


def check_harborignore(task_root: Path, report) -> None:
    ig = TaskIgnore(task_root)
    excluded = set(ig.excluded_files())
    scopes = _scope_roots(task_root)
    if not scopes:
        return

    corpus: dict[Path, str] = {}
    total = 0
    every: list[tuple[str, Path, Path]] = []  # (scope name, scope folder, file)
    for name, folder in scopes:
        for f in _files(folder):
            every.append((name, folder, f))
            rel_task = f.relative_to(task_root).as_posix()
            if rel_task not in excluded and total < _MAX_CORPUS_BYTES:
                text = _text(f)
                total += len(text)
                if text:
                    corpus[f] = text

    # info: what the rules keep out
    listed = sorted(excluded)
    if listed:
        shown = ", ".join(f"`{p}`" for p in listed[:_MAX_LISTED])
        more = f" and {len(listed) - _MAX_LISTED} more" if len(listed) > _MAX_LISTED else ""
        report.add(
            "harborignore", "info",
            f"`.harborignore` keeps {len(listed)} file(s) out of the verifier and oracle sandboxes (and Hub uploads): {shown}{more}.",
        )

    # fail / warn: a rule takes away something the task still uses
    for name, folder, f in every:
        rel_task = f.relative_to(task_root).as_posix()
        if rel_task not in excluded:
            continue
        if f.name == _ENTRYPOINT[name] and f.parent == folder:
            report.add(
                "harborignore", "fail",
                f"`{rel_task}` is excluded by a `.harborignore` rule. Harbor refuses to load a task whose "
                f"{'test' if name == 'tests' else 'solution'} script is excluded.",
            )
            continue
        if ig.is_build_file(f):
            continue  # Harbor builds from it even when a rule excludes it
        in_scope = f.relative_to(folder).as_posix()
        user = _mentions(corpus, in_scope, {f}) or (_mentions(corpus, f.name, {f}) if "/" in in_scope else None)
        if user is not None:
            report.add(
                "harborignore", "warn",
                f"`{rel_task}` is excluded by a `.harborignore` rule, but `{user.relative_to(task_root).as_posix()}` "
                f"mentions `{in_scope if in_scope in corpus[user] else f.name}`: the {'verifier' if name == 'tests' else 'oracle'} "
                "will not have this file. Remove the rule or stop using the file.",
            )

    # dev tooling still inside the sandbox
    flagged = 0
    for name, folder, f in every:
        rel_task = f.relative_to(task_root).as_posix()
        if rel_task in excluded:
            continue
        rel_in_scope = f.relative_to(folder)
        why = _dev_like(rel_in_scope)
        if why is None:
            continue
        # the folder it lives in, when that is what made it look like tooling
        dev_dir = next((folder / Path(*rel_in_scope.parts[: i + 1]) for i, p in enumerate(rel_in_scope.parts[:-1]) if p.lower() in _DEV_DIRS), None)
        inside = {p for p in corpus if dev_dir is not None and p.is_relative_to(dev_dir)} or {f}
        needle = dev_dir.name if dev_dir is not None else f.name
        if _mentions(corpus, needle, inside) is not None:
            continue  # something outside it names it, so the task uses it
        flagged += 1
        if flagged > _MAX_LISTED:
            continue
        hint = (f"`{dev_dir.relative_to(folder).as_posix()}/`" if dev_dir is not None else f"`{rel_in_scope.as_posix()}`")
        if name == "tests":
            report.add(
                "harborignore", "warn",
                f"`{rel_task}` looks like authoring tooling ({why}) and is copied into the verifier sandbox, where code the "
                f"verifier runs can read it; nothing else in the task mentions it. If the verifier does not need it, add a "
                f"rule such as {hint} to `tests/.harborignore`.",
            )
        else:
            report.add(
                "harborignore", "info",
                f"`{rel_task}` looks like authoring tooling ({why}) and is copied for the oracle; nothing else in the task "
                f"mentions it. If the oracle does not need it, add {hint} to `solution/.harborignore`.",
            )
    if flagged > _MAX_LISTED:
        report.add("harborignore", "info", f"... and {flagged - _MAX_LISTED} more file(s) that look like authoring tooling.")
