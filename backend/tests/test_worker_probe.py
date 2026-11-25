"""NR-10: does a Worker really drop privileges in the task's own verifier image?

Reward Kit 0.2.4's Worker checks its own boundary before it runs a command (non-root uid, no groups,
NoNewPrivs, empty capabilities, private paths unreachable) and raises WorkerSetupError when it cannot.
The probe runs a script under a Worker in the verifier image and records what that script could see
and do. These tests cover the parsing and judging, which need no Docker.
"""
import json

from app.services import worker_probe as wp

GOOD = {
    "returncode": 0,
    "stdout": "\n".join([
        "UID=61234", "GID=61234", "GROUPS=61234", "NoNewPrivs=1",
        "CapPrm=0000000000000000", "CapEff=0000000000000000", "CapAmb=0000000000000000",
        "TESTS_LISTABLE=0", "TESTS_READABLE=0", "REWARD_WRITABLE=0", "REWARD_JSON_CREATABLE=0", "REWARD_READABLE=0",
        "ENV=HOME LANG LOGNAME PATH PWD TMPDIR USER", "SPAWNED=1",
    ]),
    "stderr": "", "leftover_pids": [], "reward_after": "SENTINEL",
}


def facts(**changes):
    out = json.loads(json.dumps(GOOD))
    lines = dict(line.split("=", 1) for line in out["stdout"].splitlines())
    for key, value in changes.items():
        if key in lines:
            lines[key] = value
        else:
            out[key] = value
    out["stdout"] = "\n".join(f"{k}={v}" for k, v in lines.items())
    return out


def names(result, ok):
    return {c["name"] for c in result["checks"] if c["ok"] is ok}


def test_a_worker_that_holds_every_boundary_passes():
    result = wp.judge(GOOD)
    assert result["status"] == "passed" and not names(result, False)


def test_a_root_worker_fails():
    result = wp.judge(facts(UID="0", GID="0", GROUPS="0"))
    assert result["status"] == "failed" and "unprivileged_uid" in names(result, False)


def test_supplementary_groups_fail():
    assert "no_supplementary_groups" in names(wp.judge(facts(GROUPS="61234 0")), False)


def test_missing_no_new_privs_fails():
    assert "no_new_privs" in names(wp.judge(facts(NoNewPrivs="0")), False)


def test_retained_capabilities_fail():
    assert "no_capabilities" in names(wp.judge(facts(CapEff="00000000a80425fb")), False)


def test_reading_the_tests_folder_fails():
    assert "tests_not_readable" in names(wp.judge(facts(TESTS_READABLE="1")), False)


def test_writing_the_reward_file_fails_even_when_the_sentinel_changed():
    result = wp.judge(facts(REWARD_WRITABLE="1", reward_after="HACKED"))
    failed = names(result, False)
    assert {"reward_not_writable", "reward_intact"} <= failed


def test_an_inherited_environment_variable_fails():
    assert "env_not_inherited" in names(wp.judge(facts(ENV="HOME PATH ANTHROPIC_API_KEY")), False)


def test_a_leftover_process_fails():
    assert "leftovers_killed" in names(wp.judge(facts(leftover_pids=["412"])), False)


def test_a_setup_error_is_reported_as_the_environment_not_the_task():
    result = wp.judge({"setup_error": "Worker could not install required protections: x"})
    assert result["status"] == "failed" and "worker_started" in names(result, False)
    assert "environment" in result["note"].lower()


def test_the_driver_output_is_read_from_the_marker_line():
    payload = json.dumps({"returncode": 0})
    assert wp.parse_driver_output("noise\nWORKER_PROBE_JSON=" + payload + "\nmore") == {"returncode": 0}
    assert wp.parse_driver_output("no marker here") is None


def test_a_task_without_a_worker_is_skipped(tmp_path):
    from tests._fixtures import make_task
    task = make_task(tmp_path, test_py='def test_x():\n    assert open("/app/o").read()=="1"\n')
    result = wp.run_worker_probe(task)
    assert result["skipped"] and "Worker" in result["note"]


def test_the_leftover_pattern_matches_both_ways_a_background_sleep_shows_in_proc():
    import re
    assert re.search(wp.LEFTOVER_PATTERN, b"sleep\x00600\x00")
    assert re.search(wp.LEFTOVER_PATTERN, b"sh\x00-c\x00sleep 600\x00")
    assert not re.search(wp.LEFTOVER_PATTERN, b"sleep\x0060\x00")


def test_the_driver_is_valid_python_after_substitution():
    import ast
    src = wp.DRIVER.replace("__SCRIPT__", repr(wp.PROBE_SCRIPT)).replace("__LEFTOVER__", repr(wp.LEFTOVER_PATTERN))
    ast.parse(src.replace("__SENTINEL__", wp.SENTINEL).replace("__MARKER__", wp.MARKER))
