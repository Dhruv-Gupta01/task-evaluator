"""Verifier mutants: the positive controls and the fault injections.

A probe that scores 0 against the real verifier proves nothing on its own, because a
probe that fails for an unrelated reason looks identical to one that was correctly
defeated. A mutant is a copy of the verifier changed in exactly one way, so the same
probe can be run against it. There are two kinds, and they mean opposite things:

  role="defence"  A protection is REMOVED. The probe must score 0 against the real
                  verifier and 1 against this mutant. Then the defence is demonstrated:
                  the probe really does reach it, and the real verifier really does stop
                  it. A mutant that scores 0 means the probe is probably broken, so the
                  result is "unproven", not evidence.

  role="fault"    A FAULT is injected (a data folder missing from the verifier image, an
                  empty fixture set). Nothing is being tested for the probe's sake. The
                  question is whether the verifier fails closed. The probe must score 0
                  against this mutant too. A 1 means the verifier fails open: it pays full
                  reward when its own inputs are gone, which no agent caused but any
                  packaging mistake can. That is a finding (verdict "fail_open").

Mutants are never shipped with the task and a mutant's reward is never counted as a cheat.
Only the real verifier scoring 1 is "exploited".

Two ways to build one:
  source  - exact string substitution inside a file under tests/
  context - paths removed from the tests/ build context before the image is built
  literal - a large module-level constant in a tests/ .py file replaced by an empty one
            (generated, see literal_candidates)

Substitutions assert that they matched exactly once. A mutant that silently fails to
apply would turn its control into a false negative, which is the one failure mode this
whole mechanism exists to prevent.

Mutants are keyed to a task by its slug (the last part of `[task] name`), so they can only
apply to the task they were written for. Add a task by adding entries below and a probe
family in probes/.
"""
from __future__ import annotations

import ast
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.services.harborignore import TaskIgnore


@dataclass(frozen=True)
class Mutant:
    mid: str
    task: str
    defence: str
    kind: str                      # "source" | "context" | "literal"
    role: str = "defence"          # "defence" | "fault"
    target: str = ""               # path relative to tests/
    old: str = ""
    new: str = ""
    drop: tuple[str, ...] = ()     # for kind="context"

    def apply(self, tests_dir: Path) -> None:
        if self.kind == "context":
            for rel in self.drop:
                p = tests_dir / rel
                if not p.exists():
                    raise AssertionError(f"mutant {self.mid}: {rel} not in build context")
                shutil.rmtree(p) if p.is_dir() else p.unlink()
            return
        if self.kind == "literal":
            _empty_literal(tests_dir / self.target, self.old, self.mid)
            return
        if self.kind != "source":
            raise ValueError(f"mutant {self.mid}: unknown kind {self.kind!r}")
        path = tests_dir / self.target
        if not path.is_file():
            raise AssertionError(f"mutant {self.mid}: missing target {self.target}")
        text = path.read_text(encoding="utf-8")
        hits = text.count(self.old)
        if hits != 1:
            raise AssertionError(
                f"mutant {self.mid}: anchor matched {hits} times in {self.target}, "
                f"expected exactly 1. The verifier changed; update the mutant."
            )
        path.write_text(text.replace(self.old, self.new), encoding="utf-8")


REGISTRY: dict[str, Mutant] = {}


def _reg(m: Mutant) -> Mutant:
    REGISTRY[m.mid] = m
    return m


# ------------------------------------------------------------------- int8-runtime-port

# FINDING I1 (docs: "int8-runtime-port: what to change"). An empty parameter set is
# skipped by pytest, not failed, so with the fixtures gone only two tests survive and
# both pass for an empty /app/kref.py. Fixed by asserting the fixture count at import,
# after which this mutant must score 0.
_reg(Mutant(
    mid="int8:testdata-missing",
    task="int8-runtime-port",
    role="fault",
    defence="FINDING I1: nothing asserts that the 129 fixtures are present at import time",
    kind="context", drop=("fixtures",),
))

# /tests is chmod 700 before pytest, so the agent's kref.py (run as nobody) cannot read the
# fixtures that hold the expected output. Removing the chmod must make the lookup probe win.
_reg(Mutant(
    mid="int8:no-tests-lock",
    task="int8-runtime-port",
    role="defence",
    defence="chmod 700 /tests keeps the expected outputs unreadable by the agent's program",
    kind="source", target="test.sh",
    old="chmod 700 /tests /logs/verifier",
    new="chmod 700 /logs/verifier  # MUTANT int8:no-tests-lock",
))

# The grader runs the agent's kref.py as `nobody` while /tests is root-owned (COPY in
# tests/Dockerfile) and chmod 700. Removing the chmod alone (int8:no-tests-lock) only lets
# the program READ. This one also makes /tests writable, so the tamper probe (I-d) must now
# see its writes succeed. Reward is not the signal: pytest has already collected the cases
# and read each expected value before running kref.py, so tampering in the same run cannot
# raise the score. What matters is that agent code could change the grader's own files.
_reg(Mutant(
    mid="int8:tests-writable",
    task="int8-runtime-port",
    role="defence",
    defence="root ownership plus chmod 700 stop the agent's program from changing /tests",
    kind="source", target="test.sh",
    old="chmod 700 /tests /logs/verifier",
    new="chmod -R a+rwX /tests; chmod 700 /logs/verifier  # MUTANT int8:tests-writable",
))


# ------------------------------------------------------------------------- generic

GENERIC_DROP_DIRS = "generic:drop-dirs"
GENERIC_EMPTY_LITERALS = "generic:empty-literals"
_DROP_PREFIX = "generic:drop-"
_EMPTY_PREFIX = "generic:empty-"
MAX_DROPPED = 8
MAX_LITERALS = 6
# A module-level constant is treated as embedded ground truth when its source is at least this
# long. Short ones are settings (a timeout, a list of three names), long ones are tables.
MIN_LITERAL_CHARS = 300
_SKIP_PARTS = {"node_modules", "__pycache__", "vendor", "venv", ".venv", "site-packages", ".git"}

# Top-level files under tests/ that are code or configuration, not ground truth. Everything
# else (json, csv, txt, xlsx, npy, ttl, ...) counts as data the verifier reads.
_CODE_SUFFIXES = {".py", ".sh", ".toml", ".lock", ".cfg", ".ini", ".md", ".pyc"}
_CODE_NAMES = {"Dockerfile", ".dockerignore", "requirements.txt", "constraints.txt"}


def _is_data_file(p: Path) -> bool:
    return (
        p.is_file()
        and not p.name.startswith(".")
        and p.name not in _CODE_NAMES
        and not p.name.startswith(("requirements", "conda-"))
        and p.suffix.lower() not in _CODE_SUFFIXES
    )


def _line_offsets(data: bytes) -> list[int]:
    """Byte offset where each line starts. ast column offsets are UTF-8 byte offsets, and
    ast breaks lines only on \\n, \\r and \\r\\n, which is what bytes.splitlines does."""
    offsets, total = [], 0
    for line in data.splitlines(keepends=True):
        offsets.append(total)
        total += len(line)
    return offsets


def _module_assignments(tree: ast.Module) -> dict[str, list[ast.expr]]:
    found: dict[str, list[ast.expr]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            found.setdefault(node.targets[0].id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            found.setdefault(node.target.id, []).append(node.value)
    return found


def _empty_for(value: ast.expr) -> str | None:
    if isinstance(value, ast.Dict):
        return "{}"
    if isinstance(value, ast.List):
        return "[]"
    if isinstance(value, ast.Tuple):
        return "()"
    if isinstance(value, ast.Set):
        return "set()"
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return '""'
    if isinstance(value, ast.Constant) and isinstance(value.value, bytes):
        return 'b""'
    return None


def literal_candidates(tests_dir: Path) -> list[tuple[str, str, int]]:
    """Large module-level constants in tests/*.py: (relative path, name, source length).

    Ground truth is sometimes written straight into the test file (an EXPECTED dict, a
    reference table) instead of a data file, so G0's file removal cannot reach it: deleting
    the .py file removes the tests too. This finds the constants to empty instead. Only
    module-level assignments of a dict, list, tuple, set, str or bytes literal, a name
    assigned once, and at least MIN_LITERAL_CHARS long; the biggest MAX_LITERALS win.
    Constants built inside functions or by code are not covered.
    """
    out: list[tuple[str, str, int]] = []
    ignore = TaskIgnore(tests_dir.parent)  # a file a .harborignore rule excludes is not in the real verifier
    for path in sorted(tests_dir.rglob("*.py")):
        rel_parts = path.relative_to(tests_dir).parts
        if any(part in _SKIP_PARTS or part.startswith(".") for part in rel_parts) or ignore.is_excluded(path):
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
            continue
        for name, values in _module_assignments(tree).items():
            if len(values) != 1 or name == "__all__" or _empty_for(values[0]) is None:
                continue
            size = len(ast.get_source_segment(src, values[0]) or "")
            if size >= MIN_LITERAL_CHARS:
                out.append(("/".join(rel_parts), name, size))
    out.sort(key=lambda t: (-t[2], t[0], t[1]))
    return out[:MAX_LITERALS]


def _empty_literal(path: Path, name: str, mid: str) -> None:
    """Replace the value of module-level `name` with an empty literal of the same kind.
    Asserts the name is assigned exactly once and the result still parses, so a mutant that
    silently did nothing, or broke the file's syntax, cannot pass for a real fault check."""
    if not path.is_file():
        raise AssertionError(f"mutant {mid}: missing target {path.name}")
    data = path.read_bytes()
    tree = ast.parse(data.decode("utf-8"))
    values = _module_assignments(tree).get(name, [])
    if len(values) != 1:
        raise AssertionError(
            f"mutant {mid}: {name} is assigned {len(values)} times at module level in {path.name}, expected 1"
        )
    value = values[0]
    empty = _empty_for(value)
    if empty is None:
        raise AssertionError(f"mutant {mid}: {name} is not a plain literal")
    offsets = _line_offsets(data)
    start = offsets[value.lineno - 1] + value.col_offset
    end = offsets[value.end_lineno - 1] + value.end_col_offset
    mutated = data[:start] + empty.encode() + data[end:]
    try:
        ast.parse(mutated.decode("utf-8"))
    except SyntaxError as exc:
        raise AssertionError(f"mutant {mid}: emptying {name} left {path.name} unparseable: {exc}") from exc
    path.write_bytes(mutated)


def expand(tests_dir: Path, ids: tuple[str, ...] | None) -> list[str]:
    """Turn the placeholder `generic:drop-dirs` into one fault mutant id per data item.

    Any task keeps ground truth under tests/ (a fixtures folder, a ground_truth.json, an
    answer key) and the verifier should fail closed when one is missing, so no per-task
    authoring is needed: each top-level folder and each top-level data file is removed in
    turn. Hidden and cache entries are skipped, folders come first, and at most MAX_DROPPED
    are tried because each one costs a full verifier run.
    """
    out: list[str] = []
    for mid in ids or ():
        if mid == GENERIC_EMPTY_LITERALS:
            out += [f"{_EMPTY_PREFIX}{rel}::{name}" for rel, name, _ in literal_candidates(tests_dir)]
            continue
        if mid != GENERIC_DROP_DIRS:
            out.append(mid)
            continue
        ignore = TaskIgnore(tests_dir.parent)
        entries = sorted((p for p in tests_dir.iterdir() if not ignore.is_excluded(p)), key=lambda p: p.name)
        names = [
            p.name for p in entries
            if p.is_dir() and not p.name.startswith((".", "_")) and p.name != "node_modules"
        ] + [p.name for p in entries if _is_data_file(p)]
        out += [_DROP_PREFIX + n for n in names[:MAX_DROPPED]]
    return out


def for_task(task_slug: str) -> dict[str, Mutant]:
    return {k: v for k, v in REGISTRY.items() if v.task == task_slug}


def get(mid: str) -> Mutant:
    if mid.startswith(_EMPTY_PREFIX) and "::" in mid:
        rel, name = mid[len(_EMPTY_PREFIX):].split("::", 1)
        return Mutant(
            mid=mid, task="*", role="fault", kind="literal", target=rel, old=name,
            defence=f"fault: the constant {name} in tests/{rel} is empty",
        )
    if mid.startswith(_DROP_PREFIX):
        name = mid[len(_DROP_PREFIX):]
        return Mutant(
            mid=mid, task="*", role="fault", kind="context", drop=(name,),
            defence=f"fault: tests/{name} is missing from the verifier image",
        )
    if mid not in REGISTRY:
        raise KeyError(f"unknown mutant {mid!r}; known: {sorted(REGISTRY)}")
    return REGISTRY[mid]
