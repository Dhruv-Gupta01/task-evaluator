"""At least two different correct solutions per task (authors: docs/oracle-solutions.md).

The Harbor convention: `solution/solve.sh` reads `$SOLUTION` and picks one of several solutions, and the Oracle
is run with `--ae SOLUTION=<value>`. A task declares its correct values in `[metadata] oracle_solutions`; they are
never guessed from the script, which may also hold deliberately wrong ones.

The static check can show that two values are declared and wired up, and catch two that run identical text. It
cannot tell whether two scripts are really different approaches; nothing automatic can.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

ENV = "SOLUTION"
KEY = "oracle_solutions"
MIN_VALUES, MAX_VALUES = 2, 10
MIN_RUNS = 3  # each solution must score 1 this many times before the Oracle gate opens
_VALUE_RE = re.compile(r"[A-Za-z0-9_.\-]{1,40}")
_READS_RE = re.compile(r"\$\{?SOLUTION\b|environ(?:\.get)?[\[(]\s*['\"]SOLUTION['\"]|getenv\(\s*['\"]SOLUTION['\"]")
_ARM_RE = re.compile(r"^\s*\(?([A-Za-z0-9_.\-\"'| ]+)\)(.*?);;", re.M | re.S)


def _toml(task_root: Path) -> dict:
    try:
        return tomllib.loads((task_root / "task.toml").read_text())
    except (OSError, ValueError):
        return {}


def declared_values(task_root: Path) -> list[str]:
    """The distinct, usable values of `[metadata] oracle_solutions`, in the order given; [] when absent or invalid."""
    values = (_toml(task_root).get("metadata") or {}).get(KEY)
    if not isinstance(values, list) or not all(isinstance(v, str) and _VALUE_RE.fullmatch(v) for v in values):
        return []
    return list(dict.fromkeys(values))[:MAX_VALUES]


def _code(text: str) -> str:
    """A script without comments and blank lines, so two copies that differ only there compare equal."""
    return "\n".join(line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#"))


def _arm_text(command: str, solution: Path) -> str:
    """What one `case` arm runs: the command, with any script it names under solution/ read in."""
    out = _code(command)
    for name in re.findall(r"[\w./\-]+\.(?:sh|py)", command):
        path = solution / Path(name).name
        if path.is_file():
            out = out.replace(name, _code(path.read_text(errors="ignore")))
    return out


def _identical_values(task_root: Path, values: list[str]) -> list[str]:
    solve = task_root / "solution" / "solve.sh"
    if not solve.is_file():
        return []
    arms: dict[str, str] = {}
    for labels, command in _ARM_RE.findall(solve.read_text(errors="ignore")):
        for label in labels.split("|"):
            arms[label.strip().strip("\"'")] = command
    texts: dict[str, str] = {}
    for value in values:
        if value in arms:
            text = _arm_text(arms[value], task_root / "solution")
            if text in texts:
                return [texts[text], value]
            texts[text] = value
    return []


def problems(task_root: Path) -> list[str]:
    values = declared_values(task_root)
    found = []
    if len(values) < MIN_VALUES:
        found.append(
            f"task.toml must declare at least {MIN_VALUES} correct solutions as `[metadata] {KEY} = [\"1\", \"2\"]` "
            "(distinct values; letters, digits, _ . -)"
        )
    solution = task_root / "solution"
    reads = solution.is_dir() and any(
        _READS_RE.search(p.read_text(errors="ignore")) for p in solution.rglob("*") if p.is_file() and p.stat().st_size < 1_000_000
    )
    if not reads:
        found.append("solution/solve.sh must read $SOLUTION to pick which solution to run")
    if ENV in ((_toml(task_root).get("solution") or {}).get("env") or {}):
        found.append("remove SOLUTION from [solution.env]: Harbor lets it override the value being tested")
    return found


def identical(task_root: Path) -> list[str]:
    """Where two solutions are the same code (comments, blank lines and indentation ignored): two declared values
    that run the same commands, or two script files under solution/ with the same content. Renamed variables or
    reordered lines are not caught; identical code is all this check claims."""
    found = []
    values = declared_values(task_root)
    if len(values) >= MIN_VALUES and (same := _identical_values(task_root, values)):
        found.append(f"SOLUTION={same[0]} and SOLUTION={same[1]} run the same code")
    solution = task_root / "solution"
    seen: dict[str, str] = {}
    for path in sorted(solution.rglob("*")) if solution.is_dir() else []:
        if not path.is_file() or path.suffix not in (".sh", ".py") or path.stat().st_size > 1_000_000:
            continue
        code = _code(path.read_text(errors="ignore"))
        if not code:
            continue
        rel = path.relative_to(task_root).as_posix()
        if code in seen:
            found.append(f"{seen[code]} and {rel} are the same code")
        else:
            seen[code] = rel
    return found


_SOLVE_NAME_RE = re.compile(r"^(?:solve|solution|sol)[_\-]?(.+)$")


def _arms(task_root: Path) -> list[tuple[list[str], int, int]]:
    """solve.sh's `case` arms as (labels, start, end of what the arm runs)."""
    solve = task_root / "solution" / "solve.sh"
    if not solve.is_file():
        return []
    return [([label.strip().strip("\"'") for label in m[1].split("|")], *m.span(2))
            for m in _ARM_RE.finditer(solve.read_text(errors="replace"))]


def _own_arm(arms: list[tuple[list[str], int, int]], value: str) -> tuple[list[str], int, int] | None:
    return next((a for a in arms if value in a[0]), None) or next((a for a in arms if "*" in a[0]), None)


def files_of_other_solutions(task_root: Path, value: str) -> set[str]:
    """Files under solution/ that only a different declared solution runs, so editing one changes nothing when
    SOLUTION=value. Found from solve.sh's `case` arms (following the files they name) and from file names like
    solve_2.sh. A file this cannot place counts as shared."""
    solution = task_root / "solution"
    if not solution.is_dir():
        return set()
    files = {p.relative_to(task_root).as_posix(): p for p in solution.rglob("*") if p.is_file()}
    texts = {rel: p.read_text(errors="ignore") for rel, p in files.items() if p.stat().st_size < 1_000_000}
    solve_text = texts.get("solution/solve.sh", "")
    arms = _arms(task_root)

    def closure(text: str) -> set[str]:
        found: set[str] = set()
        queue = [text]
        while queue:
            current = queue.pop()
            for rel, path in files.items():
                if rel != "solution/solve.sh" and rel not in found and re.search(
                    rf"(?<![\w.\-]){re.escape(path.name)}(?![\w\-])", current
                ):
                    found.add(rel)
                    queue.append(texts.get(rel, ""))
        return found

    def used_by(v: str) -> set[str]:
        arm = _own_arm(arms, v)
        used = closure(solve_text[arm[1]:arm[2]]) if arm else set()
        for rel, path in files.items():
            named = _SOLVE_NAME_RE.match(path.stem)
            if named and named[1] == v:
                used |= {rel} | closure(texts.get(rel, ""))
        return used

    others = set().union(*(used_by(v) for v in declared_values(task_root) if v != value), set())
    return others - used_by(value)


def unused_edit(task_root: Path, value: str, file: str, find: str) -> str | None:
    """Why an edit would change nothing when SOLUTION=value, or None."""
    target = (task_root / file).resolve()
    rel = target.relative_to(task_root.resolve()).as_posix()
    why = f"it changes code that SOLUTION={value} does not run"
    if rel in files_of_other_solutions(task_root, value):
        return f"{why} ({rel})"
    if rel != "solution/solve.sh":
        return None
    at = target.read_text(errors="replace").find(find)
    arms = _arms(task_root)
    own = _own_arm(arms, value)
    if any(start <= at < end and (own is None or start != own[1]) for _, start, end in arms):
        return f"{why} (another `case` arm of {rel})"
    return None


def check_identical_solutions(task_root: Path, report) -> None:
    found = identical(task_root)
    if found:
        report.add("identical_solutions", "fail",
                   "; ".join(found) + ". Two copies of one solution are not two solutions (docs/oracle-solutions.md).")
    else:
        report.add("identical_solutions", "info", "No two solutions are the same code. Renamed or reordered code is not detected.")


def check_oracle_solutions(task_root: Path, report) -> None:
    found = problems(task_root)
    if found:
        report.add("oracle_solutions", "fail", "; ".join(found) + " (docs/oracle-solutions.md).")
    else:
        report.add(
            "oracle_solutions", "info",
            f"Declared correct solutions: {', '.join(declared_values(task_root))}. The Oracle runs each at least {MIN_RUNS} times; "
            "every run must score 1.",
        )


def oracle_problems(task_root: Path) -> list[str]:
    """Everything that stops this task's Oracle from being a gate at all."""
    return problems(task_root) + identical(task_root)
