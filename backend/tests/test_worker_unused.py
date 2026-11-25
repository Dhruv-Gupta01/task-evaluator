"""`verifier_worker_unused` (info): a Worker that protects nothing.

From the review chat: importing Worker without using it gives a false "uses Worker" label, and wrapping
only the grader's own commands in it fails correct work for no gain. Two cases are detectable from the
grader's text: Worker imported but never called, and Worker called where no agent-writable path
appears in the call. Advisory.
"""
from tests.test_verifier_isolation_detection import _results, _task


def unused(task):
    return _results(task).get("verifier_worker_unused")


def test_worker_imported_but_never_called(tmp_path):
    t = _task(tmp_path, 'from rewardkit import Worker\n\ndef test_x():\n    assert open("/app/out.txt").read() == "1"\n')
    found = unused(t)
    assert found and found[0] == "info" and "never called" in found[1]


def test_worker_wrapping_only_the_graders_own_command(tmp_path):
    t = _task(
        tmp_path,
        'from rewardkit import Worker\n\ndef test_x():\n    Worker(cmd=["python3", "/tests/check.py"]).run()\n',
    )
    found = unused(t)
    assert found and found[0] == "info" and "no agent-writable path" in found[1]


def test_worker_running_agent_code_is_quiet(tmp_path):
    t = _task(
        tmp_path,
        'from rewardkit import Worker\n\ndef test_x():\n    Worker(cmd=["python3", "/app/solution.py"]).run()\n',
    )
    assert unused(t) is None


def test_agent_path_held_in_a_variable_is_quiet(tmp_path):
    t = _task(
        tmp_path,
        'from rewardkit import Worker\nSOLUTION = "/app/solution.py"\n\n'
        'def test_x():\n    Worker(cmd=["python3", SOLUTION]).run()\n',
    )
    assert unused(t) is None


def test_no_worker_at_all_is_quiet(tmp_path):
    t = _task(tmp_path, 'def test_x():\n    assert open("/app/out.txt").read() == "1"\n')
    assert unused(t) is None


def test_agent_artifact_passed_to_run_files_is_quiet(tmp_path):
    """The real Reward Kit shape (pg-wal, the Reward Kit test task): the agent's file goes to .run(files=[...])."""
    t = _task(
        tmp_path,
        'from pathlib import Path\nfrom rewardkit import Worker\nOUT = Path("/app/output")\n\n'
        'def probe():\n    tarball = OUT / "recovered.tar.gz"\n'
        '    return Worker(env={"PATH": "/usr/bin"}).run("tar -tf x", files=[tarball], timeout_sec=60)\n',
    )
    assert unused(t) is None


def test_run_on_the_graders_own_script_and_files_is_flagged(tmp_path):
    t = _task(
        tmp_path,
        'from rewardkit import Worker\n\ndef probe():\n    return Worker(env={}).run("echo hi", files=["/tests/data.json"])\n',
    )
    found = unused(t)
    assert found and "no agent-writable path" in found[1]
