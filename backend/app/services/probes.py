"""Oracle/Nop probes (batch 4): each one changes a temporary copy of the task and runs it
through the real verifier, so the platform sees how the grader behaves, not how it reads.

- control: the untouched Oracle. A probe only counts as held if this reached 1 (15.33).
- data_removal (CK-A, 15.1): one data folder the tests read is deleted. Reward 1 = fail-open.
- hostile (CK-C, 15.3): the solution runs, then an artifact is replaced by a FIFO, a symlink,
  a directory or a 4 GB sparse file. Anything but reward 0 inside the timeout is the finding.
- null_rung1 / null_rung2 (E2, 15.37): plain Nop, and empty files at every artifact path.
"""

import re
import shutil
import tomllib
from pathlib import Path

HOSTILE_KINDS = ("fifo", "symlink", "directory", "oversized")
_HOSTILE_CMD = {
    "fifo": 'mkfifo "$A"',
    "symlink": 'ln -s /etc/passwd "$A"',
    "directory": 'mkdir -p "$A"',
    "oversized": 'truncate -s 4G "$A"',
}
_NOT_DATA = {"__pycache__", "generator", "generators", "authoring", ".pytest_cache"}


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
    """Folders under tests/ whose name the verifier's own files mention, i.e. data it reads."""
    tests = root / "tests"
    if not tests.is_dir():
        return []
    text = ""
    for p in [tests / "test.sh", *tests.glob("*.py")]:
        if p.is_file():
            text += p.read_text(encoding="utf-8", errors="replace")
    return sorted(
        d.name for d in tests.iterdir()
        if d.is_dir() and d.name not in _NOT_DATA and any(d.rglob("*"))
        and re.search(rf"[\"'/]{re.escape(d.name)}[\"'/]", text)
    )


def plan(root: Path, kinds: tuple[str, ...] = ("control", "data_removal", "hostile", "null"),
         hostile_kinds: tuple[str, ...] = HOSTILE_KINDS, all_artifacts: bool = False) -> list[dict]:
    """The probes to run, in order. Hostile probes use the first artifact unless all_artifacts."""
    probes: list[dict] = []
    if "control" in kinds:
        probes.append({"name": "control", "kind": "control", "agent": "oracle"})
    if "data_removal" in kinds:
        probes += [{"name": f"remove tests/{d}", "kind": "data_removal", "agent": "oracle", "folder": d}
                   for d in data_folders(root)]
    arts = artifacts(root)
    if "hostile" in kinds:
        for art in arts if all_artifacts else arts[:1]:
            probes += [{"name": f"{k} at {art}", "kind": "hostile", "agent": "oracle", "artifact": art, "hostile": k}
                       for k in hostile_kinds]
    if "null" in kinds:
        probes.append({"name": "null rung 1 (nop)", "kind": "null_rung1", "agent": "nop"})
        if arts:
            probes.append({"name": "null rung 2 (empty artifacts)", "kind": "null_rung2", "agent": "oracle"})
    return probes


def _wrap_solution(copy_root: Path, tail: str, run_original: bool) -> None:
    sol = copy_root / "solution"
    original = sol / "solve.sh"
    if run_original and original.exists():
        original.rename(sol / "solve_probe_original.sh")
    body = "#!/bin/bash\n"
    if run_original:
        body += 'bash "$(dirname "$0")/solve_probe_original.sh"\n'
    body += tail + "exit 0\n"
    sol.mkdir(exist_ok=True)
    (sol / "solve.sh").write_bytes(body.encode())
    (sol / "solve.sh").chmod(0o755)


def apply(copy_root: Path, probe: dict) -> None:
    """Changes a copy of the task for one probe (control and rung 1 change nothing)."""
    kind = probe["kind"]
    if kind == "data_removal":
        shutil.rmtree(copy_root / "tests" / probe["folder"])
    elif kind == "hostile":
        tail = f'A="{probe["artifact"]}"\nmkdir -p "$(dirname "$A")"\nrm -rf "$A"\n{_HOSTILE_CMD[probe["hostile"]]}\n'
        _wrap_solution(copy_root, tail, run_original=True)
    elif kind == "null_rung2":
        tail = "".join(f'mkdir -p "$(dirname "{a}")"\n: > "{a}"\n' for a in artifacts(copy_root))
        _wrap_solution(copy_root, tail, run_original=False)


def cap_verifier_timeout(copy_root: Path, seconds: float) -> None:
    """Lowers [verifier].timeout_sec in the copy, so a probe that hangs the verifier ends sooner.
    The cap comes from how long the control's verifier took, so a fair run never hits it."""
    path = copy_root / "task.toml"
    text = path.read_text(encoding="utf-8")
    m = re.search(r"(\[verifier\][^\[]*?timeout_sec\s*=\s*)([\d.]+)", text, re.S)
    if m and float(m.group(2)) > seconds:
        path.write_text(text[: m.start(2)] + f"{seconds:.1f}" + text[m.end(2):], encoding="utf-8")


def verdict(probe: dict, reward: float | None, control_reward: float | None) -> str:
    """held / failed_open / invalid_reward / unverified. 'held' needs a control that reached 1 (15.33)."""
    if probe["kind"] == "control":
        return "reached_1" if reward is not None and reward >= 1.0 else "did_not_reach_1"
    if reward is None:
        return "invalid_reward"
    if reward >= 1.0:
        return "failed_open"
    if probe["kind"] != "null_rung1" and (control_reward is None or control_reward < 1.0):
        return "unverified"
    return "held"
