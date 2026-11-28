"""`.harborignore`: which files of a task Harbor really hands to the verifier and the oracle.

Harbor (0.25 and newer) reads gitignore-style `.harborignore` files inside `tests/` and `solution/`
(and `steps/*/tests/`, `steps/*/solution/`) and leaves the matching files out when it uploads
`tests/` into the verifier sandbox, uploads `solution/` for the oracle, or packages the task for
Harbor Hub. Real runs therefore already honour it, because they go through Harbor. The platform's
own copies of a task (the Tier 0 replica verifier, the Tier 1 hint, the verifier fingerprint) must
use the same rule, or they show an attacker files the real verifier never contains.

`TaskIgnore` is a close port of `harbor.models.task.file_filter.TaskFileFilter`. Keep it in step
with that class; `tests/test_harborignore.py` compares the two on a fixture whenever Harbor's own
code can be imported.

Differences from Harbor on purpose:
  * `Dockerfile` and `docker-compose.yaml` directly under `tests/` (or `environment/`) stay in a copy
    made for building even when a rule excludes them: Harbor builds from them but does not upload
    them (see `harbor.lint.task._is_task_build_file`).
  * Nothing here raises on a broken `.harborignore`; an unreadable or invalid file is treated as
    "no rules" so one odd file cannot stop a stage (Harbor's linter reports it).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from pathspec import GitIgnoreSpec

IGNORE_FILENAME = ".harborignore"
# Where Harbor reads `.harborignore` rules, and the only places it applies them.
_SCOPES = GitIgnoreSpec.from_lines(("/tests/", "/solution/", "/steps/*/tests/", "/steps/*/solution/"))
_BUILD_FILES = {"Dockerfile", "docker-compose.yaml"}


class TaskIgnore:
    def __init__(self, task_dir: Path | str):
        self.task_dir = Path(os.path.abspath(task_dir))
        self._specs: dict[Path, GitIgnoreSpec | None] = {}

    def _rules(self, directory: Path) -> GitIgnoreSpec | None:
        """The rules of `.harborignore` in `directory`, or None outside the places Harbor reads them."""
        if directory in self._specs:
            return self._specs[directory]
        spec: GitIgnoreSpec | None = None
        try:
            rel = directory.relative_to(self.task_dir)
        except ValueError:
            self._specs[directory] = None
            return None
        if _SCOPES.match_file(rel.as_posix() + "/"):
            path = directory / IGNORE_FILENAME
            patterns: list[str] = []
            try:
                if path.is_file() and not path.is_symlink():
                    patterns = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                patterns = []
            try:
                spec = GitIgnoreSpec.from_lines(patterns)
            except Exception:  # an invalid pattern: no rules from this file
                spec = GitIgnoreSpec.from_lines([])
        self._specs[directory] = spec
        return spec

    def _matches(self, path: Path) -> bool:
        rel = path.relative_to(self.task_dir)
        if rel.name == IGNORE_FILENAME:
            return True
        suffix = "/" if path.is_dir() else ""
        if not _SCOPES.match_file(rel.as_posix() + suffix):
            return False
        specs: list[tuple[Path, GitIgnoreSpec]] = []
        candidate = self.task_dir
        for part in rel.parts:
            spec = self._rules(candidate)
            if spec is not None:
                specs.append((candidate, spec))
            candidate /= part
            suffix = "/" if candidate != path or path.is_dir() else ""
            if not _SCOPES.match_file(candidate.relative_to(self.task_dir).as_posix() + suffix):
                continue
            excluded = False
            for directory, spec in specs:
                match = spec.check_file(candidate.relative_to(directory).as_posix() + suffix)
                if match.include is not None:
                    excluded = match.include
            if excluded:
                return True
        return False

    def is_excluded(self, path: Path | str) -> bool:
        """Whether Harbor leaves this path out (also for a symlink whose target inside the task is out)."""
        path = Path(os.path.abspath(path))
        if not path.is_relative_to(self.task_dir):
            return False
        if self._matches(path):
            return True
        resolved = path.resolve()
        return resolved != path and resolved.is_relative_to(self.task_dir) and self._matches(resolved)

    def has_rules(self) -> bool:
        """Whether any `.harborignore` sits in a place Harbor reads it (a cheap test before filtering)."""
        for top in ("tests", "solution"):
            base = self.task_dir / top
            if base.is_dir() and any(base.rglob(IGNORE_FILENAME)):
                return True
        steps = self.task_dir / "steps"
        return steps.is_dir() and any(steps.glob(f"*/*/**/{IGNORE_FILENAME}"))

    def is_build_file(self, path: Path | str) -> bool:
        """`tests/Dockerfile` and friends: Harbor builds from them even when a rule excludes them."""
        p = Path(os.path.abspath(path))
        if p.name not in _BUILD_FILES:
            return False
        try:
            parts = p.relative_to(self.task_dir).parent.parts
        except ValueError:
            return False
        return parts in (("environment",), ("tests",)) or (len(parts) == 3 and parts[0] == "steps" and parts[2] == "tests")

    def excluded_files(self, scope: str | None = None) -> list[str]:
        """Files under `tests/` and `solution/` (and the step folders) that Harbor leaves out, as
        task-relative posix paths, not counting the `.harborignore` files themselves."""
        found: list[str] = []
        roots = [self.task_dir / scope] if scope else [self.task_dir / "tests", self.task_dir / "solution"]
        steps = self.task_dir / "steps"
        if steps.is_dir() and not scope:
            for step in sorted(p for p in steps.iterdir() if p.is_dir()):
                roots += [step / "tests", step / "solution"]
        for root in roots:
            if not root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                here = Path(dirpath)
                kept = []
                for d in sorted(dirnames):
                    if self.is_excluded(here / d):
                        # the whole folder is out: list its files, do not descend
                        for sub, _, files in os.walk(here / d):
                            for f in files:
                                if f != IGNORE_FILENAME:
                                    found.append((Path(sub) / f).relative_to(self.task_dir).as_posix())
                    else:
                        kept.append(d)
                dirnames[:] = kept
                for f in filenames:
                    if f != IGNORE_FILENAME and self.is_excluded(here / f):
                        found.append((here / f).relative_to(self.task_dir).as_posix())
        return sorted(set(found))


def copy_tests(task_root: Path | str, dest: Path | str) -> None:
    """Copy `tests/` to `dest` the way Harbor uploads it into the verifier: without the files a
    `.harborignore` rule excludes and without the `.harborignore` files. A task with no rules is
    copied exactly as before. Build files (`tests/Dockerfile`, `tests/docker-compose.yaml`) are kept
    because the verifier image is built from them."""
    task_root = Path(task_root)
    src = task_root / "tests"
    ignore = TaskIgnore(task_root)
    if not ignore.has_rules():
        shutil.copytree(src, dest, symlinks=True)
        return

    def skip(dirpath: str, names: list[str]) -> list[str]:
        here = Path(dirpath)
        return [
            n for n in names
            if (n == IGNORE_FILENAME or ignore.is_excluded(here / n)) and not ignore.is_build_file(here / n)
        ]

    shutil.copytree(src, dest, symlinks=True, ignore=skip)
