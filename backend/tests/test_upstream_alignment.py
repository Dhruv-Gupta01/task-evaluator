"""Vendored checks and rubric at upstream bdf6034, check-lockfiles, warning-level rubric
criteria, and a check that could not run (dockerfile-pin missing, timed out) never a pass."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services import tb_static_checks, vendor_info
from app.services.tb_static_checks import TbCheckResult, TbStaticCheckReport

PINNED = "bdf60347ddb22be127cf3af14215c0385ea08705"
PINNED_IMAGE = "ubuntu:22.04@sha256:" + "b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"


def test_checks_are_the_pinned_upstream_set():
    info = vendor_info.checks_info()
    assert info["commit"] == PINNED
    assert info["scripts"] == 29
    names = {p.name for p in vendor_info.CHECKS_DIR.iterdir()}
    assert {"check-lockfiles.sh", "check-lockfiles.py", "check-image-digests.sh"} <= names


def test_rubric_has_37_criteria_two_of_them_warnings():
    info = vendor_info.rubric_info()
    assert info["commit"] == PINNED
    assert info["criteria"] == 37
    assert info["warning_criteria"] == ["effective_instruction_length", "score_provenance"]


def _harbor_python() -> Path | None:
    try:
        tool_dir = subprocess.run(["uv", "tool", "dir"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    for rel in ("harbor/Scripts/python.exe", "harbor/bin/python"):
        if tool_dir and (Path(tool_dir) / rel).exists():
            return Path(tool_dir) / rel
    return None


def test_harbor_check_accepts_the_rubric_with_severity():
    python = _harbor_python()
    if python is None:
        pytest.skip("harbor is not installed as a uv tool")
    code = (
        "import json, sys\n"
        "from harbor.cli.quality_checker.models import load_rubric\n"
        "from harbor.analyze.models import build_check_response_model\n"
        "r = load_rubric(__import__('pathlib').Path(sys.argv[1]))\n"
        "model = build_check_response_model(r)\n"
        "print(json.dumps({'criteria': len(r.criteria), 'fields': sorted(model.model_fields)}))\n"
    )
    proc = subprocess.run(
        [str(python), "-c", code, str(vendor_info.RUBRIC_FILE)],
        capture_output=True, text=True, timeout=120, env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["criteria"] == 37
    assert "score_provenance" in out["fields"]


def test_failed_warning_criterion_does_not_block():
    results = [{"checks": {
        "verifiable": {"outcome": "pass"},
        "score_provenance": {"outcome": "fail"},
        "anti_cheat_robustness": {"outcome": "fail"},
        "gpu_types": {"outcome": "not_applicable"},
    }}]
    summary = vendor_info.rubric_summary(results, ["effective_instruction_length", "score_provenance"])
    assert summary == {
        "blocking_fails": ["anti_cheat_robustness"],
        "warnings": ["score_provenance"],
        "passed": 1,
        "not_applicable": 1,
    }


@pytest.mark.parametrize(
    "results,expected",
    [
        ([TbCheckResult("a", True, "")], ("passed", 1)),
        ([TbCheckResult("a", True, ""), TbCheckResult("b", False, "", skipped=True)], ("failed", None)),
        ([TbCheckResult("a", False, ""), TbCheckResult("b", False, "", skipped=True)], ("failed", 0)),
    ],
)
def test_a_check_that_did_not_run_is_never_a_pass(results, expected):
    assert tb_static_checks.stage_outcome(TbStaticCheckReport(results=results)) == expected


def _task(root: Path, tests_dockerfile: str, env_from: str) -> Path:
    (root / "environment").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "solution").mkdir()
    (root / "task.toml").write_text('[task]\nname = "terminal-bench/fixture-task"\n')
    (root / "instruction.md").write_text("Do the thing.\n")
    (root / "environment" / "Dockerfile").write_text(f"FROM {env_from}\nWORKDIR /app\n")
    (root / "tests" / "Dockerfile").write_text(tests_dockerfile)
    (root / "tests" / "requirements.txt").write_text("pytest==8.4.1\n")
    (root / "tests" / "test.sh").write_text("#!/bin/bash\necho 1 > /logs/verifier/reward.txt\n")
    (root / "solution" / "solve.sh").write_text("#!/bin/bash\ntrue\n")
    return root


UNLOCKED = (
    f"FROM {PINNED_IMAGE}\n"
    "COPY requirements.txt /tmp/requirements.txt\n"
    "# the line below installs without a lock\n"
    "RUN pip install --require-hashes -r /tmp/requirements.txt\n"
    "COPY . /tests/\n"
)
NO_INSTALLS = f"FROM {PINNED_IMAGE}\nCOPY . /tests/\n"


def _by_name(report: TbStaticCheckReport) -> dict[str, TbCheckResult]:
    return {r.name: r for r in report.results}


@pytest.fixture(scope="module")
def unlocked_report(tmp_path_factory) -> TbStaticCheckReport:
    task = _task(tmp_path_factory.mktemp("unlocked") / "fixture-task", UNLOCKED, "ubuntu:22.04")
    return tb_static_checks.run_tb_static_checks(task)


def test_all_29_checks_run(unlocked_report):
    assert len(unlocked_report.results) == 29


def test_lockfiles_fails_at_the_install_line(unlocked_report):
    result = _by_name(unlocked_report)["check-lockfiles"]
    assert not result.passed and not result.skipped
    assert "Dockerfile:4" in result.output.replace("\\", "/")
    assert "uv export --locked" in result.output


def test_lockfiles_passes_without_installs(tmp_path):
    task = _task(tmp_path / "fixture-task", NO_INSTALLS, PINNED_IMAGE)
    proc = subprocess.run(
        [tb_static_checks._bash(), str(vendor_info.CHECKS_DIR / "check-lockfiles.sh"), str(task) + "/"],
        capture_output=True, text=True, timeout=60, env=tb_static_checks._script_env(tmp_path),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_image_digests_runs_and_fails_an_unpinned_from(unlocked_report):
    if not any((tb_static_checks.TOOLS_BIN / n).exists() for n in ("dockerfile-pin", "dockerfile-pin.exe")):
        pytest.skip("dockerfile-pin not installed (scripts/install_dockerfile_pin.py)")
    result = _by_name(unlocked_report)["check-image-digests"]
    assert not result.skipped
    assert not result.passed
    assert "ubuntu:22.04" in result.output


def test_without_dockerfile_pin_the_stage_is_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(tb_static_checks, "TOOLS_BIN", tmp_path / "empty")
    keep = [d for d in os.environ["PATH"].split(os.pathsep) if not shutil.which("dockerfile-pin", path=d)]
    monkeypatch.setenv("PATH", os.pathsep.join(keep))
    task = _task(tmp_path / "fixture-task", NO_INSTALLS, PINNED_IMAGE)
    report = tb_static_checks.run_tb_static_checks(task)
    digests = _by_name(report)["check-image-digests"]
    assert digests.skipped and not digests.passed
    assert "[NOT RUN] check-image-digests" in report.as_text()
    assert tb_static_checks.stage_outcome(report)[0] == "failed"


def test_a_timed_out_check_is_not_run_not_failed(tmp_path, monkeypatch):
    import subprocess

    # Every script "times out". A tiny real timeout is racy: the two trivial checks
    # (check-no-cheat-dir, check-task-changelog) can finish inside 1 ms and pass.
    def _always_times_out(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 0)

    monkeypatch.setattr(tb_static_checks.subprocess, "run", _always_times_out)
    task = _task(tmp_path / "fixture-task", NO_INSTALLS, PINNED_IMAGE)
    report = tb_static_checks.run_tb_static_checks(task)
    assert report.fail_count == 0 and report.pass_count == 0
    assert report.skip_count == 29
    assert "timed out" in report.results[0].output
    assert tb_static_checks.stage_outcome(report) == ("failed", None)
