"""G28 to G30: Oracle probes judged by a real Harbor Oracle trial on a changed copy of the task, once per declared
solution (`[metadata] oracle_solutions`, passed as SOLUTION=<value>).

Taken from the post-fixes branch's separate Probes stage (its rules 15.1, 15.3 and 15.8). The rest of that stage is
already in Tier 0 (control G13, FIFO G8, symlink G7, empty artifacts G0/G14, and Nop is its own gate), so only the
three new probes are here:

  G28  data removed   one tests/ data folder the grader names is deleted, and the author's real solution runs. A
                      reward of 1 is `fail_open`: the grader passes when its expected data is missing (15.1). When
                      it also collected fewer tests than the solution's passing gate runs, the note says so (15.8).
                      Unlike G0, the output is the real one, so a grader that first checks "output exists" and then
                      skips the comparison is caught.
  G29  directory      after the real solution, the first artifact is replaced by a directory (15.3).
  G30  4 GB file      after the real solution, the first artifact is replaced by a 4 GB sparse file (15.3). The
                      task's own verifier timeout applies: a shorter cap turned a slow but sound grader into a false
                      finding on the post-fixes branch.

For G29 and G30 reward 1 is `exploited` (garbage scored full marks) and no reward is `crashed_on_bad_input` (the
grader broke instead of scoring 0, so Harbor records an error, not a 0). A held result counts only when that
solution scored 1 in the Oracle gate; otherwise it is listed as unproven.
"""

import re
import shutil
import tomllib
from pathlib import Path

from app.services import harborignore, solution_variants

_NOT_DATA = {"__pycache__", "generator", "generators", "authoring", ".pytest_cache"}
_PLANT = {"directory": 'mkdir -p "$A"', "oversized": 'truncate -s 4G "$A"'}
_TITLE = {
    "directory": "a directory in place of the first artifact, after the real solution",
    "oversized": "a 4 GB sparse file in place of the first artifact, after the real solution",
}
_PID = {"directory": "G29", "oversized": "G30"}
ORIGINAL = "solve_probe_original.sh"


def artifacts(root: Path) -> list[str]:
    try:
        data = tomllib.loads((root / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    out = []
    for a in data.get("artifacts") or []:
        path = a if isinstance(a, str) else str((a or {}).get("source", ""))
        if path.startswith("/"):
            out.append(path)
    return out


def data_folders(root: Path) -> list[str]:
    """Folders under tests/ that the grader's own files name (so it reads them) and Harbor uploads. A folder a
    `.harborignore` rule leaves out never reaches the verifier, so removing it would prove nothing."""
    tests = root / "tests"
    if not tests.is_dir():
        return []
    text = ""
    for p in [tests / "test.sh", *tests.glob("*.py")]:
        if p.is_file():
            text += p.read_text(encoding="utf-8", errors="replace")
    ignore = harborignore.TaskIgnore(root)
    return sorted(
        d.name for d in tests.iterdir()
        if d.is_dir() and d.name not in _NOT_DATA and any(f.is_file() for f in d.rglob("*"))
        and not ignore.is_excluded(d)
        and re.search(rf"[\"'/]{re.escape(d.name)}[\"'/]", text)
    )


def plan(root: Path) -> list[dict]:
    """Every probe to run, in order: each data folder, then the directory and the 4 GB file at the first
    artifact, each once per declared solution (once with SOLUTION unset when the task declares none)."""
    solutions: list[str | None] = list(solution_variants.declared_values(root)) or [None]
    at = lambda v: f"@{v}" if v else ""  # noqa: E731
    label = lambda v: f" (SOLUTION={v})" if v else ""  # noqa: E731
    out: list[dict] = []
    for i, folder in enumerate(data_folders(root)):
        out += [{"pid": f"G28-{i}{at(v)}", "kind": "data_removal", "folder": folder, "solution": v,
                 "title": f"tests/{folder} removed, the real solution graded{label(v)}"} for v in solutions]
    arts = artifacts(root)
    if arts:
        for kind in ("directory", "oversized"):
            out += [{"pid": f"{_PID[kind]}{at(v)}", "kind": kind, "artifact": arts[0], "solution": v,
                     "title": f"{_TITLE[kind]}{label(v)}"} for v in solutions]
    return out


def apply(copy_root: Path, entry: dict) -> None:
    """Changes a copy of the task for one probe."""
    if entry["kind"] == "data_removal":
        shutil.rmtree(copy_root / "tests" / entry["folder"])
        return
    sol = copy_root / "solution"
    sol.mkdir(exist_ok=True)
    if (sol / "solve.sh").exists():
        (sol / "solve.sh").rename(sol / ORIGINAL)
    body = (
        "#!/bin/bash\n"
        f'[ -f "$(dirname "$0")/{ORIGINAL}" ] && bash "$(dirname "$0")/{ORIGINAL}"\n'  # SOLUTION is inherited
        f'A="{entry["artifact"]}"\nmkdir -p "$(dirname "$A")"\nrm -rf "$A"\n{_PLANT[entry["kind"]]}\nexit 0\n'
    )
    (sol / "solve.sh").write_text(body, encoding="utf-8")
    (sol / "solve.sh").chmod(0o755)


def judge(entry: dict, reward: float | None, control: dict | None, tests: int | None) -> dict:
    """The verdict, notes and control fields for one probe. `control` is {passed, tests} from that solution's
    Oracle-gate runs (None when it has none); `tests` is how many tests this probe's run collected."""
    which = f"SOLUTION={entry['solution']}" if entry.get("solution") else "the solution"
    control_ok = bool(control and control.get("passed"))
    notes: list[str] = []
    if entry["kind"] == "data_removal":
        what = f"tests/{entry['folder']} removed"
        if reward is not None and reward >= 1.0:
            verdict = "fail_open"
            notes.append(f"15.1: reward 1 with {what}: the grader passes when its expected data is missing, so a "
                         "broken image or a missing folder passes everyone. Assert the expected case count at import.")
            expected = (control or {}).get("tests")
            if tests is not None and expected and tests < expected:
                notes.append(f"15.8: this run collected {tests} test(s), {which}'s passing gate runs {expected}: the "
                             "grader collects fewer cases without its data and does not notice.")
        elif reward is None:
            verdict = "held"
            notes.append(f"no reward with {what}: the grader stopped, which Harbor records as an error, not a pass")
        else:
            verdict = "held"
            notes.append(f"reward {reward} with {what}")
    else:
        what = "a directory" if entry["kind"] == "directory" else "a 4 GB sparse file"
        if reward is not None and reward >= 1.0:
            verdict = "exploited"
            notes.append(f"15.3: reward 1 with {what} at {entry['artifact']}: the grader gave full marks for garbage. "
                         "Stat the path, require a regular file and bound its size.")
        elif reward is None:
            verdict = "crashed_on_bad_input"
            notes.append(f"15.3: no reward with {what} at {entry['artifact']} inside the verifier timeout: Harbor "
                         "records an error, not 0. Stat the path, require a regular file and bound its size.")
        else:
            verdict = "held"
            notes.append(f"reward {reward} with {what} at {entry['artifact']}")
    if verdict == "held" and not control_ok:
        notes.append(f"unproven: {which} never scored 1 in the Oracle gate, so a 0 here proves nothing")
    return {
        "verdict": verdict, "notes": notes,
        "positive_control": f"Oracle gate runs of {which}",
        "positive_control_reward": 1.0 if control_ok else None,
        "positive_control_ok": control_ok,
    }
