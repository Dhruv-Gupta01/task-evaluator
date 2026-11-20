"""Static detection of where a grader runs agent-written code (verifier_isolation).

Patterns found by reading how 33 upstream Terminal-Bench tasks ran submitted code before they moved
to Reward Kit Workers: compilers and provers, commands built from named constants and lists, helper
functions around subprocess, artifacts a compiler wrote from agent sources, and the Worker itself.
"""

from pathlib import Path

from app.services.static_checks import StaticCheckReport
from app.services.verifier_isolation import run_isolation_checks

TOML = '[task]\nname = "terminal-bench/x"\n\n[verifier]\nenvironment_mode = "separate"\n'
DOCKERFILE = "FROM ubuntu:24.04\nWORKDIR /app\n"


def _task(tmp_path: Path, test_py: str, test_sh: str = "pytest /tests/test_x.py\n") -> Path:
    for rel, text in {
        "task.toml": TOML,
        "environment/Dockerfile": DOCKERFILE,
        "tests/test.sh": test_sh,
        "tests/test_x.py": test_py,
    }.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


def _results(task: Path) -> dict[str, tuple[str, str]]:
    report = StaticCheckReport()
    run_isolation_checks(task, report)
    return {r.name: (r.severity, r.message) for r in report.results}


def _runs_agent_code(task: Path) -> bool:
    found = _results(task)["verifier_runs_agent_code"]
    return not found[1].startswith("No place found")


def test_coq_compile_of_the_agents_project_is_agent_code(tmp_path):
    task = _task(
        tmp_path,
        'import subprocess\nAPP_DIR = "/app"\n\ndef test_compiles():\n'
        '    subprocess.run(["coqc", "-Q", ".", "Top", "Main.v"], cwd=str(APP_DIR))\n',
    )
    severity, message = _results(task)["verifier_runs_agent_code"]
    assert severity == "warn" and "root" in message


def test_compiling_agent_sources_then_running_the_binary(tmp_path):
    task = _task(
        tmp_path,
        'import subprocess\nfrom pathlib import Path\nAPP = Path("/app")\n\n'
        "def build(tmp):\n"
        '    binary = tmp / "h"\n'
        '    sources = sorted(str(p) for p in (APP / "src").glob("*.cc"))\n'
        '    cmd = ["g++", "-std=c++20", "-I", str(APP / "include"), *sources, "-o", str(binary)]\n'
        "    subprocess.run(cmd, check=True)\n"
        "    return subprocess.run([str(binary)])\n",
    )
    assert _results(task)["verifier_runs_agent_code"][0] == "warn"


def test_launcher_held_in_a_constant(tmp_path):
    task = _task(
        tmp_path,
        'import subprocess\nSAGE = shutil.which("sage")\nSOLVER = "/app/solver.sage"\n\n'
        "def run(args):\n"
        "    return subprocess.Popen([SAGE, SOLVER, *map(str, args)])\n",
    )
    assert _results(task)["verifier_runs_agent_code"][0] == "warn"


def test_runuser_constant_with_agent_command_is_a_privilege_drop(tmp_path):
    task = _task(
        tmp_path,
        'import subprocess\nRUNUSER = "/usr/sbin/runuser"\n\n'
        'def run():\n    command = ["/app/run.sh"]\n'
        '    return subprocess.Popen([RUNUSER, "-u", "nobody", "--", *command], cwd="/app")\n',
    )
    severity, message = _results(task)["verifier_runs_agent_code"]
    assert severity == "info" and "separate user" in message


def test_helper_function_around_subprocess(tmp_path):
    task = _task(
        tmp_path,
        'import subprocess\n\ndef _run(cmd, **kwargs):\n    return subprocess.run(cmd, capture_output=True, **kwargs)\n\n'
        'def test_release():\n    _run(["node", "/app/dist/client.js"])\n',
    )
    assert _results(task)["verifier_runs_agent_code"][0] == "warn"


def test_worker_run_is_recognised_and_not_reported_as_no_agent_code(tmp_path):
    task = _task(
        tmp_path,
        "from rewardkit import Worker, WorkerError\n\n"
        "def test_probe(tarball):\n"
        '    return Worker(env={"PATH": "/usr/bin"}).run(PROBE, files=[tarball], timeout_sec=60)\n',
    )
    found = _results(task)
    assert found["verifier_runs_agent_code_in_worker"][0] == "info"
    assert "tests/test_x.py:4" in found["verifier_runs_agent_code_in_worker"][1]
    assert "verifier_runs_agent_code" not in found
    assert "verifier_tests_readable" not in found


def test_reading_the_agents_files_is_not_running_them(tmp_path):
    task = _task(
        tmp_path,
        'import subprocess\n\ndef test_git():\n    subprocess.run(["git", "-C", "/app", "rev-parse", "HEAD"])\n'
        '    subprocess.run(["g++", "-std=c++20", "/tests/own_harness.cc", "-o", "/tmp/h"])\n',
    )
    assert not _runs_agent_code(task)
