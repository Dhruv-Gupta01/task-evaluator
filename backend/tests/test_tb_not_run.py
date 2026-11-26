"""A Terminal-Bench check that could not run is never counted as a pass."""
import os
from pathlib import Path

from app.services import tb_static_checks as tb


def report(*results):
    return tb.TbStaticCheckReport(results=[tb.TbCheckResult(f"c{i}", p, "", skipped=s) for i, (p, s) in enumerate(results)])


def test_all_ran_and_passed_is_a_pass():
    assert tb.stage_outcome(report((True, False), (True, False))) == ("passed", 1)


def test_a_failure_is_a_fail():
    assert tb.stage_outcome(report((False, False), (False, True))) == ("failed", 0)


def test_a_check_that_did_not_run_is_not_a_pass():
    assert tb.stage_outcome(report((True, False), (False, True))) == ("failed", None)


def test_the_installed_tool_folder_is_on_the_scripts_path(tmp_path: Path):
    assert str(tb.TOOLS_BIN) in tb._script_env(tmp_path)["PATH"].split(os.pathsep)
