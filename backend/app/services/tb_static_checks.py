"""Terminal-Bench's own official static checks (vendor/tb_checks/, see
PROVENANCE.md there) -- framework-compliance rules (canary present, verifier
runs separately, task fields/README sections valid, no absolute paths, etc.)
defined and maintained upstream by the Terminal-Bench project itself. This is
a different, mostly non-overlapping rule set from static_checks.py's own
platform-specific hygiene checks (instruction formatting, Dockerfile
pinning) -- both run, neither replaces the other."""
import re
import subprocess
import tempfile
import tomllib
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


@dataclass
class TbStaticCheckReport:
    results: list[TbCheckResult] = field(default_factory=list)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    def as_text(self) -> str:
        lines = []
        for r in self.results:
            lines.append(f"[{'PASS' if r.passed else 'FAIL'}] {r.name}")
            if not r.passed and r.output:
                lines.append(r.output.strip())
        return "\n".join(lines)


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


def run_tb_static_checks(task_root: Path) -> TbStaticCheckReport:
    """Runs every vendored check-*.sh against task_root, one process each,
    same as Terminal-Bench's own CI does (.github/workflows/static-checks.yml).

    Some of these scripts internally re-split a path on an unquoted variable
    (e.g. check-separate-verifier.sh's `for task_dir in $TASK_DIRS`, no
    quotes), which breaks on any whitespace in the path -- true of this repo's
    own checkout ("Task Evaluator", with a space) on at least one machine.
    That's a bug in the vendored, upstream script, not something to patch in
    a "verbatim, unmodified" vendor copy -- so a space-free symlink to
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
        clean_path.symlink_to(task_root.resolve(), target_is_directory=True)

        for script in sorted(_CHECKS_DIR.glob("check-*.sh")):
            try:
                proc = subprocess.run(
                    ["bash", str(script), str(clean_path)],
                    capture_output=True, text=True, timeout=_TIMEOUT_SEC,
                )
                passed = proc.returncode == 0
                output = (proc.stdout or "") + (proc.stderr or "")
            except subprocess.TimeoutExpired:
                passed = False
                output = f"timed out after {_TIMEOUT_SEC}s"
            report.results.append(TbCheckResult(script.stem, passed, output))
    return report
