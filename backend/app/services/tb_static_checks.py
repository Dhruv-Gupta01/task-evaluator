"""Terminal-Bench's own official static checks (vendor/tb_checks/, see
PROVENANCE.md there) -- framework-compliance rules (canary present, verifier
runs separately, task fields/README sections valid, no absolute paths, etc.)
defined and maintained upstream by the Terminal-Bench project itself. This is
a different, mostly non-overlapping rule set from static_checks.py's own
platform-specific hygiene checks (instruction formatting, Dockerfile
pinning) -- both run, neither replaces the other."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from functools import lru_cache
from dataclasses import dataclass, field
from pathlib import Path

_TASK_NAME_RE = re.compile(r"^([^/]+)/(.+)$")

_CHECKS_DIR = Path(__file__).resolve().parent.parent.parent / "vendor" / "tb_checks" / "scripts"
_TIMEOUT_SEC = 30


@dataclass
class TbCheckResult:
    name: str
    passed: bool
    output: str
    # The check could not run here (a tool it needs is not installed); neither a pass nor a fail.
    skipped: bool = False


@dataclass
class TbStaticCheckReport:
    results: list[TbCheckResult] = field(default_factory=list)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if not r.passed and not r.skipped)

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def skip_count(self) -> int:
        return sum(1 for r in self.results if r.skipped)

    def as_text(self) -> str:
        lines = []
        for r in self.results:
            label = "SKIP" if r.skipped else "PASS" if r.passed else "FAIL"
            lines.append(f"[{label}] {r.name}")
            # Advisory checks (check-cpu-threads) print WARN lines and still exit 0.
            if r.output and (not r.passed or _WARN_LINE_RE.search(r.output)):
                lines.append(r.output.strip())
        return "\n".join(lines)


_WARN_LINE_RE = re.compile(r"^WARN ", re.M)
# Tools a vendored check needs that are not part of a normal install (upstream CI
# installs them in its workflow).
_MISSING_TOOL_RE = re.compile(r"(dockerfile-pin): (?:command )?not found")


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def _number(value, divisor: float = 1) -> str:
    value = float(value) / divisor
    return str(int(value)) if value.is_integer() else str(value)


def expected_readme_metadata(task_root: Path) -> str | None:
    """The `## Task Metadata` block check-task-fields.sh compares the README
    with, built from task.toml the same way the script builds it. The script
    stops at the first mismatch, so an author fixing it one line at a time
    needs several runs; this shows every expected line at once. None when
    task.toml lacks a field the block needs (the script reports that itself)."""
    try:
        data = tomllib.loads((task_root / "task.toml").read_text())
        meta, agent, env = data["metadata"], data["agent"], data["environment"]
        names, emails = _as_list(meta["author_name"]), _as_list(meta["author_email"])
        author = ", ".join(f"{n} ({e})" for n, e in zip(names, emails))
        if meta.get("author_organization"):
            author += " | " + ", ".join(dict.fromkeys(_as_list(meta["author_organization"])))
        lines = [
            f"- **Author:** {author}",
            f"- **Category:** `{meta['category']}`",
            "- **Tags:** " + " ".join(f"<code>{t}</code>" for t in meta["tags"]),
            f"- **Expert time:** {_number(meta['expert_time_estimate_hours'])} hours",
            f"- **Agent timeout:** {_number(agent['timeout_sec'], 3600)} hours",
            f"- **CPUs:** {_number(env['cpus'])}",
            f"- **Memory:** {_number(env['memory_mb'], 1024)} GB",
        ]
        if env.get("gpus", 0):
            lines.append(f"- **GPUs:** {_number(env['gpus'])}")
    except (OSError, KeyError, TypeError, ValueError, tomllib.TOMLDecodeError):
        return None
    return "## Task Metadata\n\n" + "\n".join(lines)


_PLACEHOLDER_RE = re.compile(r"\bREPLACE_WITH_[A-Z0-9_]+")


def placeholders(task_root: Path) -> list[str]:
    """`REPLACE_WITH_...` template values left in the files authors fill in."""
    found: list[str] = []
    for rel in ("task.toml", "README.md", "instruction.md"):
        path = task_root / rel
        if path.is_file():
            for name in dict.fromkeys(_PLACEHOLDER_RE.findall(path.read_text(errors="replace"))):
                found.append(f"{name} in {rel}")
    return found


def _explain_task_fields(task_root: Path, output: str) -> str:
    notes: list[str] = []
    left = placeholders(task_root)
    if left:
        notes.append(
            "Template placeholders are still in the task: " + ", ".join(left)
            + ". Replace them first; an 'invalid GitHub username' failure below is usually this."
        )
    expected = expected_readme_metadata(task_root)
    if expected:
        notes.append(
            "README.md must contain exactly these lines, built from task.toml (the script below "
            "stops at the first mismatch):\n\n" + expected
        )
    return "\n\n".join(notes + [output]) if notes else output


def _expected_slug(task_root: Path) -> str:
    """check-task-package-name.sh derives the task's expected slug from its
    own directory's basename -- meaningful for Terminal-Bench's own layout
    (tasks/<slug>/), but this platform always extracts into a generic
    submissions/{id}/extracted/ folder, never named after the task's real
    slug. So the slug is read back out of task.toml's own declared [task]
    name ("terminal-bench/<slug>") instead, and used as the symlink's name
    below -- otherwise this check would fail for every single submission
    on this platform, regardless of whether the task is actually named
    correctly. Falls back to task_root's own basename if task.toml can't be
    read, so the check still runs (and reports accurately) either way."""
    try:
        data = tomllib.loads((task_root / "task.toml").read_text())
        name = (data.get("task") or {}).get("name", "")
        m = _TASK_NAME_RE.match(name)
        if m:
            return m.group(2)
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return task_root.name


_CANARY_FAIL_RE = re.compile(r"^FAIL (\S+?):", re.M)


def _label_contested_canary(output: str) -> str:
    """Upstream proposes dropping the canary requirement for environment/ files
    (issues #2200, #2210) because a canary in a file the agent can read leaks
    into its context. Until the vendored copy is refreshed, a canary failure
    that points only at environment/ files is marked contested, not hidden."""
    failing = _CANARY_FAIL_RE.findall(output)
    if failing and all("/environment/" in f for f in failing):
        return (
            "CONTESTED: every file named below is under environment/, and upstream is discussing "
            "dropping the canary rule for those files. Treat this as a question for a reviewer, not a "
            "clear failure.\n" + output
        )
    return output


@lru_cache
def _bash() -> str:
    """The bash to run the scripts with. On Windows a bare "bash" resolves to
    System32's WSL launcher before PATH is searched, and WSL cannot open
    Windows paths, so Git for Windows' own bash is used instead."""
    if os.name != "nt":
        return "bash"
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parent.parent
        for candidate in (root / "bin" / "bash.exe", root.parent / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
            if candidate.is_file():
                return str(candidate)
    return shutil.which("bash") or "bash"


def _link_task(target: Path, link: Path) -> None:
    """A symlink where the OS allows one. Windows needs admin rights or
    developer mode for that, so there a directory junction (no privilege
    needed) is used, and a plain copy as the last resort."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError:
        pass
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    except (ImportError, OSError):
        shutil.copytree(target, link, symlinks=True)


def vendor_info() -> dict:
    """The upstream commit the vendored checks come from (read from PROVENANCE.md) and
    how many check scripts are vendored, so every result says which rule set it ran."""
    info: dict = {"commit": None, "date": None, "scripts": len(list(_CHECKS_DIR.glob("check-*.sh")))}  # the scripts that run; check-cpu-threads.py is a helper
    try:
        text = (_CHECKS_DIR.parent / "PROVENANCE.md").read_text()
    except OSError:
        return info
    m = re.search(r"Commit:\s*`([0-9a-f]{7,40})`(?:\s*\(([\d-]+)\))?", text)
    if m:
        info["commit"], info["date"] = m.group(1), m.group(2)
    return info


def run_tb_static_checks(task_root: Path) -> TbStaticCheckReport:
    """Runs every vendored check-*.sh against task_root, one process each,
    same as Terminal-Bench's own CI does (.github/workflows/static-checks.yml).

    Some of these scripts internally re-split a path on an unquoted variable
    (e.g. check-separate-verifier.sh's `for task_dir in $TASK_DIRS`, no
    quotes), which breaks on any whitespace in the path -- true of this repo's
    own checkout ("Task Evaluator", with a space) on at least one machine.
    That's a bug in the vendored, upstream script, not something to patch in
    a "verbatim, unmodified" vendor copy -- so a space-free link to
    task_root is created instead and passed to every script, named using
    _expected_slug so check-task-package-name.sh compares against the
    task's own declared name rather than this platform's generic folder
    layout."""
    report = TbStaticCheckReport()
    if not _CHECKS_DIR.is_dir():
        report.results.append(
            TbCheckResult("vendor_missing", False, f"{_CHECKS_DIR} not found")
        )
        return report

    with tempfile.TemporaryDirectory(prefix="tb-checks-") as tmp:
        clean_path = Path(tmp) / _expected_slug(task_root)
        _link_task(task_root.resolve(), clean_path)
        try:
            _run_scripts(task_root, clean_path, report, _script_env(Path(tmp)))
        finally:
            # Remove the link itself first, so cleaning up the temp dir can
            # never reach through it into the task.
            if clean_path.is_symlink():
                clean_path.unlink()
            elif os.path.isjunction(clean_path):
                os.rmdir(clean_path)
    return report


def _script_env(tmp: Path) -> dict[str, str]:
    """PATH with `python3` resolving to this backend's own interpreter. The
    scripts run `python3 -I` (check-compose-volumes parses compose files with
    PyYAML), and isolated mode skips the user site, so a machine whose python3
    has PyYAML only as a user install (or not at all) failed that check on
    every compose file with "No module named 'yaml'". The backend's
    interpreter has PyYAML (a declared dependency)."""
    shim = tmp / "python-shim"
    shim.mkdir()
    python3 = shim / "python3"
    python3.write_text(f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "$@"\n')
    python3.chmod(0o755)
    return {**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ.get('PATH', '')}"}


def _run_scripts(task_root: Path, clean_path: Path, report: TbStaticCheckReport, env: dict[str, str]) -> None:
    # The trailing slash matters: clean_path is a symlink, and `find <symlink>` without it does not
    # descend, so check-canary, check-compose-volumes and check-test-file-references found no files
    # and passed vacuously (found by comparing the guide with the platform's own output).
    for script in sorted(_CHECKS_DIR.glob("check-*.sh")):
        try:
            proc = subprocess.run(
                [_bash(), str(script), str(clean_path) + "/"],
                capture_output=True, text=True, timeout=_TIMEOUT_SEC, env=env,
            )
            passed = proc.returncode == 0
            output = (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            passed = False
            output = f"timed out after {_TIMEOUT_SEC}s"
        missing = _MISSING_TOOL_RE.search(output) if not passed else None
        if missing:
            report.results.append(
                TbCheckResult(
                    script.stem, False,
                    f"not run: needs the `{missing.group(1)}` CLI on PATH (see vendor/tb_checks/PROVENANCE.md)",
                    skipped=True,
                )
            )
            continue
        if not passed and script.stem == "check-canary":
            output = _label_contested_canary(output)
        if not passed and script.stem == "check-task-fields":
            output = _explain_task_fields(task_root, output)
        report.results.append(TbCheckResult(script.stem, passed, output))
