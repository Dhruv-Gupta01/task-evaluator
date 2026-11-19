"""The Harbor and Reward Kit versions this platform expects, in one place.

Reward Kit's `Worker` (the low-privilege runner for the agent's code) first
shipped in Harbor 0.25.0, so a task whose grader uses it cannot be graded
properly by an older Harbor. Terminal-Bench 4.1 pins one Reward Kit version in
every task; a task on another version is worth a look, because a new Reward Kit
release can change scores. Change `REWARDKIT_VERSION` here, on purpose, when the
platform moves to a newer one.

Nothing in this module installs or upgrades anything."""

import re
import subprocess
from functools import lru_cache

from app.config import get_settings

# The Reward Kit version Terminal-Bench 4.1 tasks pin (harbor-rewardkit==0.2.4).
REWARDKIT_VERSION = "0.2.4"
# First Harbor release with Reward Kit's Worker (2026-10-25).
HARBOR_MIN_FOR_WORKER = "0.25.0"
# The version the rest of the platform (network policy, agent options) was tested with.
HARBOR_TESTED = "0.23.0"

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    match = _VERSION_RE.search(text or "")
    return tuple(int(g) for g in match.groups()) if match else None  # type: ignore[return-value]


def at_least(version: str | None, minimum: str) -> bool | None:
    """True/False, or None when the version text has no X.Y.Z in it."""
    have, need = parse_version(version), parse_version(minimum)
    if have is None or need is None:
        return None
    return have >= need


@lru_cache(maxsize=8)
def _harbor_has_command(*command: str) -> bool:
    try:
        out = subprocess.run(
            [get_settings().harbor_bin, *command, "--help"], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def harbor_version() -> str:
    from app.services import harbor_runner  # late: harbor_runner imports this module

    return harbor_runner._harbor_version()


@lru_cache(maxsize=1)
def analyze_bin_info() -> dict | None:
    """The separate analyze/check binary (HARBOR_ANALYZE_BIN) and its version, or None when it is not set."""
    binary = get_settings().harbor_analyze_bin
    if not binary:
        return None
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15)
        version = (out.stdout or out.stderr or "").strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        version = "unknown (binary not found)"
    return {"bin": binary, "version": version}


def worker_flag(harbor: str | None = None) -> str | None:
    """A warning when this Harbor cannot run Reward Kit's Worker, else None."""
    version = harbor if harbor is not None else harbor_version()
    if at_least(version, HARBOR_MIN_FOR_WORKER) is False:
        return (
            f"this task's grader uses Reward Kit's Worker, which needs Harbor {HARBOR_MIN_FOR_WORKER} or newer; "
            f"this run used Harbor {version}. A grader error or a failed Oracle here may come from the Harbor "
            "version, not from the task."
        )
    return None


def info() -> dict:
    """What the Versions panel and GET /versions show."""
    version = harbor_version()
    worker_ok = at_least(version, HARBOR_MIN_FOR_WORKER)
    notes: list[str] = []
    if worker_ok is False:
        notes.append(
            f"Harbor {version} is older than {HARBOR_MIN_FOR_WORKER}: tasks whose grader uses Reward Kit's "
            "Worker cannot be graded as designed. Upgrade on purpose (it can change network handling and "
            f"agent options; the platform was tested with {HARBOR_TESTED}) and re-run a known task first."
        )
    elif worker_ok is None:
        notes.append("Could not read Harbor's version (is the harbor CLI installed?).")
    analyze = analyze_bin_info()
    if analyze:
        notes.append(
            f"`harbor analyze` and `harbor check` (Failure Analysis, the Cheat Trial's hacking check, Rubric "
            f"Check) run with a separate Harbor {analyze['version']} ({analyze['bin']}); everything else "
            f"uses Harbor {version}."
        )
    elif at_least(version, "0.25.0") and at_least(version, "0.26.0") is False:
        notes.append(
            f"Harbor {version}'s `analyze` and `check` fail with DownloadVerifierDirError (fixed in 0.26.0); "
            "set HARBOR_ANALYZE_BIN to a Harbor 0.26+ binary for Failure Analysis, the Cheat Trial's hacking "
            "check and Rubric Check."
        )
    if not _harbor_has_command("lint"):
        notes.append(f"`harbor lint` is not available in Harbor {version} (it needs {HARBOR_MIN_FOR_WORKER}+).")
    return {
        "harbor": {
            "version": version,
            "tested_with": HARBOR_TESTED,
            "min_for_reward_kit_worker": HARBOR_MIN_FOR_WORKER,
            "worker_supported": worker_ok,
            "lint_available": _harbor_has_command("lint"),
            "regrade_available": _harbor_has_command("job", "regrade"),
        },
        "analyze": analyze,
        "reward_kit": {"pinned_version": REWARDKIT_VERSION},
        "notes": notes,
    }
