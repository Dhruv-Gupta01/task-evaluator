"""Item 5, step 1: a shared-verifier task grades in the agent's own container, so routes that a separate verifier
closes stay open (Singularity-Bench NANO-K10 / CLUSTER-G05 leftover processes, TERM-K01 shadowed programs,
HARNESS-G13 planted pytest hooks). Tier 0 skips such tasks, so nothing probes them. Say so, once, with the routes."""
from pathlib import Path

from app.services.static_checks import StaticCheckReport
from app.services.verifier_isolation import run_isolation_checks

SEPARATE = '[task]\nname = "terminal-bench/x"\n\n[verifier]\nenvironment_mode = "separate"\n'
SHARED = '[task]\nname = "terminal-bench/x"\n'
SEPARATE_BY_SECTION = '[task]\nname = "terminal-bench/x"\n\n[verifier.environment]\ncpus = 1\n'


def task(tmp_path: Path, toml: str, test_sh: str = "pytest /tests/test_x.py\n") -> Path:
    for rel, text in {
        "task.toml": toml,
        "environment/Dockerfile": "FROM ubuntu:24.04\nWORKDIR /app\n",
        "tests/test.sh": test_sh,
        "tests/test_x.py": "def test_ok():\n    assert True\n",
    }.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def result(root: Path):
    report = StaticCheckReport()
    run_isolation_checks(root, report)
    return {r.name: (r.severity, r.message) for r in report.results}.get("verifier_shared_mode_routes")


def test_a_shared_verifier_is_warned_with_the_open_routes(tmp_path):
    severity, message = result(task(tmp_path, SHARED))
    assert severity == "warn"
    for route in ("left running", "PATH", "conftest.py", "sitecustomize"):
        assert route in message, route
    assert "Tier 0" in message and "separate" in message


def test_the_message_says_what_to_do(tmp_path):
    _, message = result(task(tmp_path, SHARED))
    assert 'environment_mode = "separate"' in message


def test_a_separate_verifier_gets_nothing(tmp_path):
    assert result(task(tmp_path, SEPARATE)) is None


def test_a_verifier_environment_section_also_counts_as_separate(tmp_path):
    assert result(task(tmp_path, SEPARATE_BY_SECTION)) is None


def test_a_task_with_no_test_sh_is_not_called_shared_by_guesswork(tmp_path):
    """No grader script means nothing to warn about yet; the missing file is reported elsewhere."""
    root = task(tmp_path, SHARED)
    (root / "tests" / "test.sh").unlink()
    assert result(root) is None


def test_the_warning_appears_once_per_task(tmp_path):
    report = StaticCheckReport()
    run_isolation_checks(task(tmp_path, SHARED), report)
    assert [r.name for r in report.results].count("verifier_shared_mode_routes") == 1
