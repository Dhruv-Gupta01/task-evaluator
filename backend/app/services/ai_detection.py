"""Terminal-Bench's own Layer 3 AI-usage detection (vendor/tb_checks/,
scripts/check_ai_detection.py, see PROVENANCE.md) -- calls the real GPTZero
API to check instruction.md and solution/solve.sh for AI-generated text.
Genuinely optional and off by default in Terminal-Bench's own workflow too
(no GPTZERO_API_KEY secret wired into their CI); this wrapper runs the
vendored script exactly as-is, including its own graceful no-op when no key
is configured, rather than reimplementing any of its logic."""
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings

settings = get_settings()

_SCRIPT = (
    Path(__file__).resolve().parent.parent.parent
    / "vendor" / "tb_checks" / "scripts" / "check_ai_detection.py"
)
_TIMEOUT_SEC = 60


@dataclass
class AiDetectionResult:
    ran: bool  # False only if the vendored script itself is missing
    passed: bool | None  # None when skipped (no key / network error) -- see output
    skipped: bool
    output: str
    error: str | None = None


def run_ai_detection(task_root: Path) -> AiDetectionResult:
    """Runs the vendored check_ai_detection.py against task_root, passing
    GPTZERO_API_KEY through if configured. Exit 0 covers both a genuine pass
    and every graceful skip (no key, network error) -- skipped is inferred
    from the script's own printed text, since it exits 0 either way and the
    distinction matters for an honest verdict here."""
    if not _SCRIPT.is_file():
        return AiDetectionResult(
            ran=False, passed=None, skipped=False, output="", error=f"{_SCRIPT} not found"
        )

    # Deliberately minimal, not host_env_for_subprocess()'s full os.environ --
    # this script sends file contents to a third-party API, so it gets only
    # what it needs (PATH to find python3's own deps, its one real secret)
    # and nothing else from the host that a genuine bug or a future edit to
    # the vendored script could leak alongside the request.
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin"}
    if settings.gptzero_api_key:
        env["GPTZERO_API_KEY"] = settings.gptzero_api_key

    try:
        proc = subprocess.run(
            ["python3", str(_SCRIPT), str(task_root)],
            capture_output=True, text=True, timeout=_TIMEOUT_SEC, env=env,
        )
    except subprocess.TimeoutExpired:
        return AiDetectionResult(
            ran=True, passed=None, skipped=False, output="",
            error=f"timed out after {_TIMEOUT_SEC}s",
        )

    output = (proc.stdout or "") + (proc.stderr or "")
    skipped = "GPTZERO_API_KEY not set" in output or "AI detection skipped" in output
    passed = None if skipped else proc.returncode == 0
    return AiDetectionResult(ran=True, passed=passed, skipped=skipped, output=output)
