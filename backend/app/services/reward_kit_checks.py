"""Is the task's grader set up the way Terminal-Bench 4.1 sets up Reward Kit?

Reward Kit runs inside the task's own grader (tests/test.sh), never inside this
platform, so the platform can only look at the files. These checks are the
review checklist from the Reward Kit proposal: a separate verifier, an exact
Reward Kit version, a scoring rule next to the checks, the agent's code run
through `Worker` with `/tests` and `/logs/verifier` private, a broken `Worker`
setup that ends the run instead of scoring it, and checks with readable names.

Text and AST checks over the task files; no LLM, no Docker. Advisory, never a
gate. A task that does not use Reward Kit at all gets one info line and nothing
else, because plain pytest graders are still allowed. Reward Kit being
absent says nothing about the task being wrong."""

import ast
import json
import re
import subprocess
import tomllib
from pathlib import Path

from app.config import get_settings
from app.services import versions

_MAX_FILE_BYTES = 400_000
_SKIP_DIRS = {"node_modules", ".git", "vendor", "__pycache__", ".venv"}
_TEXT_NAMES = {"dockerfile", "test.sh", "reward.toml", "pyproject.toml", "uv.lock"}
_TEXT_SUFFIXES = {".py", ".sh", ".toml", ".txt", ".cfg", ".lock", ".yaml", ".yml", ".md"}
_GENERIC_NAME = re.compile(r"^(?:test|check|crit|criterion|c|t|x|tmp)_?\d*$", re.I)
_REWARDKIT = re.compile(r"rewardkit", re.I)
# harbor-rewardkit==0.2.4 or harbor-rewardkit>=0.2 (the package name, not the module in `-m rewardkit`).
_PIN_RE = re.compile(r"harbor-rewardkit\s*(==|>=|<=|~=|>|<)?\s*([^\s\"',;)\]]*)", re.I)
_EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[.\w-]*)$")
_GIT_REF = re.compile(r"git\+\S+@[0-9a-f]{7,40}\b", re.I)
_WORKER_CALL = re.compile(r"\bWorker\s*\(")


def _tests_files(task_root: Path) -> list[tuple[Path, str]]:
    """(path, text) of the grader's own text files; vendored folders and big files are skipped."""
    tests = task_root / "tests"
    if not tests.is_dir():
        return []
    out: list[tuple[Path, str]] = []
    for path in sorted(tests.rglob("*")):
        if not path.is_file() or any(part in _SKIP_DIRS for part in path.relative_to(tests).parts):
            continue
        if path.name.lower() not in _TEXT_NAMES and path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            out.append((path, path.read_text(errors="replace")))
        except OSError:
            continue
    return out


def uses_reward_kit(task_root: Path) -> bool:
    return any(_REWARDKIT.search(text) for _, text in _tests_files(task_root))


def uses_worker(task_root: Path) -> bool:
    """True when a grader file creates a Reward Kit `Worker` (the isolation Harbor 0.25 introduced)."""
    return any(_WORKER_CALL.search(text) and _REWARDKIT.search(text) for _, text in _tests_files(task_root))


def _toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _call_args(text: str, start: int) -> str:
    """The text inside the parentheses that open at `start` (balanced; strings are not special-cased)."""
    depth = 0
    for i in range(start, min(len(text), start + 4000)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i]
    return text[start + 1 : start + 4000]


def _criterion_names(files: list[tuple[Path, str]]) -> list[tuple[str, str]]:
    """(function name, file name) for every function decorated with @criterion."""
    names: list[tuple[str, str]] = []
    for path, text in files:
        if path.suffix != ".py" or "criterion" not in text:
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                target = deco.func if isinstance(deco, ast.Call) else deco
                label = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
                if label == "criterion":
                    names.append((node.name, path.name))
    return names


def _check_version_pin(files: list[tuple[Path, str]], report) -> None:
    pins: list[tuple[str, str, str]] = []  # (file, operator, version)
    for path, text in files:
        if path.name == "uv.lock":
            continue
        for match in _PIN_RE.finditer(text):
            op, ver = match.group(1) or "", match.group(2) or ""
            if not op and ver.startswith("@"):
                continue  # harbor-rewardkit @ git+...: judged below by whether it names a commit
            pins.append((path.name, op, ver))
        for git in _GIT_REF.findall(text):
            pins.append((path.name, "@", git.rsplit("@", 1)[1]))
    exact = [p for p in pins if (p[1] == "==" and _EXACT_VERSION.match(p[2])) or p[1] == "@"]
    if not exact:
        where = sorted({p[0] for p in pins if p[1] or p[2]})
        report.add(
            "reward_kit_version_pin",
            "warn",
            "Reward Kit is not pinned to an exact version"
            + (f" (found {', '.join(where)} with a range or no version)" if where else "")
            + ". A new Reward Kit release can change scores: pin harbor-rewardkit==X.Y.Z in tests/pyproject.toml "
            f"and lock it in uv.lock (Terminal-Bench 4.1 pins {versions.REWARDKIT_VERSION}). Never 'latest'.",
        )
        return
    versions_found = sorted({p[2] for p in exact if p[1] == "=="})
    other = [v for v in versions_found if v != versions.REWARDKIT_VERSION]
    if other:
        report.add(
            "reward_kit_version_differs",
            "info",
            f"The task pins Reward Kit {', '.join(other)}; the platform's recorded version is "
            f"{versions.REWARDKIT_VERSION} (what Terminal-Bench 4.1 pins). Not an error, but a different "
            "version can score differently: compare on a few saved trials with a regrade before trusting it.",
        )


def check_reward_kit(task_root: Path, report) -> None:
    files = _tests_files(task_root)
    if not any(_REWARDKIT.search(text) for _, text in files):
        report.add(
            "reward_kit_not_used",
            "info",
            "The grader does not use Reward Kit (plain pytest or a hand-written test.sh). That is allowed. "
            "Terminal-Bench 4.1 moved 64 of its 66 tasks onto Reward Kit for the isolated runner, the named per-check "
            "results and the clear line between a broken grader and a failed agent.",
        )
        return

    task_toml = _toml(task_root / "task.toml")
    verifier = task_toml.get("verifier") or {}
    separate = verifier.get("environment_mode") == "separate"
    if not separate:
        report.add(
            "reward_kit_separate_verifier",
            "warn",
            "The grader uses Reward Kit but task.toml has no [verifier] environment_mode = \"separate\". "
            "Reward Kit relies on the grader running in its own sandbox, with only the listed output files "
            "crossing over from the agent's container.",
        )
    elif not (task_root / "tests" / "Dockerfile").is_file():
        report.add(
            "reward_kit_verifier_dockerfile",
            "warn",
            "environment_mode = \"separate\" but tests/Dockerfile is missing: Harbor builds the separate grader "
            "image from it (and installs Reward Kit into it).",
        )

    _check_version_pin(files, report)
    pyproject = task_root / "tests" / "pyproject.toml"
    if pyproject.is_file() and not (task_root / "tests" / "uv.lock").is_file():
        report.add(
            "reward_kit_lockfile",
            "warn",
            "tests/pyproject.toml lists the grader's packages but tests/uv.lock is missing: the grader would "
            "install whatever versions are newest at build time.",
        )

    # The scoring rule: reward.toml next to the checks, or aggregation= in the Python API.
    criterion_names = _criterion_names(files)
    reward_tomls = [p for p, _ in files if p.name == "reward.toml"]
    api_aggregation = [t for _, t in files if re.search(r"\bReward\s*\([^)]*aggregation\s*=", t, re.S)]
    if criterion_names and not reward_tomls and not api_aggregation:
        report.add(
            "reward_kit_reward_toml",
            "warn",
            "tests/ has @criterion checks but no reward.toml (and no Reward(..., aggregation=...) call): "
            "nothing says how the checks combine into the score. Terminal-Bench 4.1 uses "
            "[scoring.checks] aggregation = \"all-pass\" in every task.",
        )
    aggregations: list[str] = []
    for path in reward_tomls:
        aggregations += re.findall(r"aggregation\s*=\s*[\"']([^\"']+)[\"']", path.read_text(errors="replace"))
    for text in api_aggregation:
        aggregations += re.findall(r"aggregation\s*=\s*[\"']([^\"']+)[\"']", text)
    off_rule = sorted({a for a in aggregations if a != "all-pass"})
    if off_rule:
        report.add(
            "reward_kit_aggregation",
            "info",
            f"Scoring rule {', '.join(off_rule)} instead of all-pass. Every Terminal-Bench 4.1 task uses all-pass, "
            "so the reward stays exactly 0 or 1; a weighted or threshold rule gives partial credit, which the "
            "task guidelines do not currently allow.",
        )

    # Worker: private paths, and a broken setup must end the run, not score it.
    worker_texts = [(p, t) for p, t in files if _WORKER_CALL.search(t)]
    if worker_texts:
        # Only an explicit private_paths that leaves out a protected path is reported. A Worker()
        # with no private_paths is what 25 of the 33 Terminal-Bench 4.1 tasks that use Worker do
        # (they pass env= or stage files), so its absence is not a finding.
        missing: list[str] = []
        for path, text in worker_texts:
            for m in _WORKER_CALL.finditer(text):
                args = _call_args(text, m.end() - 1)
                if "private_paths" in args and (
                    "/tests" not in args or not ("/logs/verifier" in args or "LOGS" in args)
                ):
                    missing.append(path.name)
        if missing:
            report.add(
                "reward_kit_worker_private_paths",
                "warn",
                f"Worker(private_paths=...) in {', '.join(sorted(set(missing)))} does not name both /tests and "
                "/logs/verifier. Those two paths hold the hidden tests and the score: the agent's code must not "
                "be able to read or write them.",
            )
        handles_setup = any(
            re.search(r"WorkerSetupError|worker-setup-error", t) for _, t in files
        )
        if not handles_setup:
            report.add(
                "reward_kit_setup_error_exit",
                "warn",
                "The grader uses Worker but nothing handles WorkerSetupError (no conftest hook, no "
                "worker-setup-error marker check). If the isolation cannot be set up, the run would score as a "
                "failure instead of ending with exit code 2 and no score, so a broken grader would be blamed "
                "on the agent.",
            )

    # Readable check names: the name is what a reviewer sees in reward-details.json.
    generic = sorted({n for n, _ in criterion_names if _GENERIC_NAME.match(n) or len(n) < 3})
    if generic:
        report.add(
            "reward_kit_check_names",
            "warn",
            f"@criterion checks with unreadable names: {', '.join(generic[:8])}. The function name is the check "
            "name reviewers see; name each one after what it checks (output_exist, freeze_hour).",
        )
    counts: dict[str, int] = {}
    for name, _ in criterion_names:
        counts[name] = counts.get(name, 0) + 1
    duplicated = sorted(n for n, c in counts.items() if c > 1)
    if duplicated:
        report.add(
            "reward_kit_duplicate_check_names",
            "warn",
            f"The same @criterion name is used more than once ({', '.join(duplicated[:8])}): their results "
            "share one name in reward-details.json and a reviewer cannot tell which one failed.",
        )


_LINT_MAX_FINDINGS = 25
_LINT_MAX_EXTRAS = 15
# Reported by the agent-user check (agent_runs_as_root), which says more about it than Harbor does.
_LINT_COVERED_ELSEWHERE = {"root-agent"}


def _run_lint(task_root: Path, extra: list[str]) -> tuple[list[dict] | None, str]:
    """`harbor lint --json` on a task. The task path goes first: --select and --extend-select take
    several values, so a path after them is read as one more rule name and the command fails.
    Returns (findings, "") or (None, why it could not run)."""
    cmd = [get_settings().harbor_bin, "lint", f"{task_root}/", "--json", *extra]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}"
    try:
        data = json.loads(out.stdout or "")
    except ValueError:
        data = None
    if isinstance(data, list):
        return data, ""
    return None, ((out.stdout or "") + (out.stderr or "")).strip()[:600]


def _lint_where(finding: dict, task_root: Path) -> str:
    path = str(finding.get("path") or "")
    try:
        path = str(Path(path).resolve().relative_to(task_root.resolve()))
    except (ValueError, OSError):
        pass
    line = finding.get("line")
    return f"{path}:{line}" if path and line else path


def _author_ignored(task_root: Path) -> set[str]:
    """Rules the task's own [metadata.harbor.lint] ignore list switches off."""
    try:
        data = tomllib.loads((task_root / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    lint = (((data.get("metadata") or {}).get("harbor") or {}).get("lint")) or {}
    ignored = lint.get("ignore") if isinstance(lint, dict) else None
    return {str(x) for x in ignored} if isinstance(ignored, list) else set()


def check_harbor_lint(task_root: Path, report) -> None:
    """Harbor's own task linter (Harbor 0.25 and newer), one finding per line.

    Two runs: Harbor's default rules with the task's own `[metadata.harbor.lint]` settings
    (warn: these are what Harbor itself calls a problem), then `--extend-select ALL`, whose extra
    findings are Harbor's stricter rules and anything the author chose to ignore (info, labelled
    as such). Any selection given on the command line overrides the author's `ignore`, so the
    author's settings only hold in the first run. `root-agent` is left to the agent-user check."""
    if not versions._harbor_has_command("lint"):
        report.add(
            "harbor_lint",
            "info",
            f"`harbor lint` is not available in Harbor {versions.harbor_version()} (it needs "
            f"{versions.HARBOR_MIN_FOR_WORKER} or newer), so Harbor's own task check was not run.",
        )
        return
    base, why = _run_lint(task_root, [])
    if base is None:
        report.add("harbor_lint", "info", f"`harbor lint` could not run: {why or 'no output'}")
        return
    strict, _ = _run_lint(task_root, ["--extend-select", "ALL"])
    for f in base[:_LINT_MAX_FINDINGS]:
        where = _lint_where(f, task_root)
        report.add("harbor_lint", "warn", f"[{f.get('rule')}] {where + ': ' if where else ''}{f.get('message')}")
    if len(base) > _LINT_MAX_FINDINGS:
        report.add("harbor_lint", "warn", f"... and {len(base) - _LINT_MAX_FINDINGS} more `harbor lint` finding(s)")
    seen = {(f.get("rule"), f.get("path"), f.get("message")) for f in base}
    ignored = _author_ignored(task_root)
    extras = [
        f for f in (strict or [])
        if (f.get("rule"), f.get("path"), f.get("message")) not in seen and f.get("rule") not in _LINT_COVERED_ELSEWHERE
    ]
    for f in extras[:_LINT_MAX_EXTRAS]:
        where = _lint_where(f, task_root)
        why_extra = (
            "the task's own lint settings ignore this rule"
            if f.get("rule") in ignored
            else "a stricter Harbor rule, not in its default set"
        )
        report.add("harbor_lint", "info", f"[{f.get('rule')}] {where + ': ' if where else ''}{f.get('message')} ({why_extra})")
    if len(extras) > _LINT_MAX_EXTRAS:
        report.add("harbor_lint", "info", f"... and {len(extras) - _LINT_MAX_EXTRAS} more stricter `harbor lint` finding(s)")
    if not base and not extras:
        note = "" if strict is not None else " (the stricter run could not be done)"
        report.add("harbor_lint", "info", f"`harbor lint` found no problems{note}.")
