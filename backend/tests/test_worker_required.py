"""Way 1 of the Worker flag: one `verifier_worker_required` result per task.

not_required  nothing the agent wrote runs during grading (or it runs inside a VM)
present       agent code runs through Reward Kit's Worker
equivalent    a separate user with no-new-privs, /tests locked and child processes killed
missing       agent code runs in the verifier without any of that (file:line evidence and a fix)
Advisory only: a Static Check, never a gate.
"""
import pytest

from app.services.verifier_isolation import analyze_worker_requirement
from tests.test_verifier_isolation_detection import _results, _task

RUN_AS_ROOT = 'import subprocess\n\ndef test_x():\n    subprocess.run(["python3", "/app/solution.py"])\n'
WORKER = (
    "from rewardkit import Worker\n\n"
    'def test_x():\n    Worker(cmd=["python3", "/app/solution.py"]).run()\n'
)


def finding(task):
    return _results(task)["verifier_worker_required"]


def result_of(task):
    return analyze_worker_requirement(task)["result"]


def test_a_grader_that_only_reads_output_needs_no_worker(tmp_path):
    t = _task(tmp_path, 'def test_x():\n    assert open("/app/out.txt").read() == "1"\n')
    severity, message = finding(t)
    assert result_of(t) == "not_required" and severity == "info"
    assert message.startswith("Worker required: no")


def test_agent_code_run_through_a_worker_is_present(tmp_path):
    t = _task(tmp_path, WORKER)
    assert result_of(t) == "present"
    severity, message = finding(t)
    assert severity == "info" and "tests/test_x.py" in message


def test_agent_code_run_as_root_is_missing_with_evidence_and_a_fix(tmp_path):
    t = _task(tmp_path, RUN_AS_ROOT)
    severity, message = finding(t)
    assert result_of(t) == "missing" and severity == "warn"
    assert message.startswith("Worker required: YES")
    assert "tests/test_x.py:4" in message
    assert "Worker(...).run(...)" in message and "harbor-rewardkit==0.2.4" in message and "reward.toml" in message


def test_importing_agent_code_into_the_grader_is_missing(tmp_path):
    t = _task(tmp_path, 'import sys\nsys.path.insert(0, "/app")\nimport solution\n\ndef test_x():\n    assert solution.f() == 1\n')
    assert result_of(t) == "missing"


def test_an_agent_service_started_from_the_verifier_is_missing(tmp_path):
    t = _task(tmp_path, "def test_x():\n    pass\n", "supervisorctl -c /app/supervisord.conf start api\npytest /tests/test_x.py\n")
    assert result_of(t) == "missing"
    assert "tests/test.sh:1" in finding(t)[1]


@pytest.mark.parametrize("line", ["systemctl start /app/api.service", "docker compose -f /app/docker-compose.yaml up -d"])
def test_other_ways_to_start_an_agent_service_are_found(tmp_path, line):
    assert result_of(_task(tmp_path, "def test_x():\n    pass\n", line + "\npytest /tests/test_x.py\n")) == "missing"


def test_a_service_the_task_owns_is_not_agent_code(tmp_path):
    t = _task(tmp_path, "def test_x():\n    pass\n", "service postgresql start\npytest /tests/test_x.py\n")
    assert result_of(t) == "not_required"


def test_dropping_privileges_without_the_rest_is_still_missing_and_names_what_is_absent(tmp_path):
    body = 'import subprocess\n\ndef test_x():\n    subprocess.run(["setpriv", "--reuid=1000", "python3", "/app/solution.py"])\n'
    t = _task(tmp_path, body)
    severity, message = finding(t)
    assert result_of(t) == "missing"
    assert "/tests" in message and "no-new-privs" in message


def test_a_full_hand_made_sandbox_is_equivalent(tmp_path):
    body = (
        "import os, signal, subprocess\n\ndef test_x():\n"
        '    os.chmod("/tests", 0o700)\n'
        '    p = subprocess.Popen(["setpriv", "--reuid=1000", "--no-new-privs", "python3", "/app/solution.py"], start_new_session=True)\n'
        "    try:\n        p.wait(timeout=60)\n    finally:\n        os.killpg(p.pid, signal.SIGKILL)\n"
    )
    t = _task(tmp_path, body)
    severity, message = finding(t)
    assert result_of(t) == "equivalent" and severity == "info"


def test_agent_code_inside_a_vm_is_not_required(tmp_path):
    t = _task(tmp_path, RUN_AS_ROOT, "qemu-system-x86_64 -kernel /app/kernel -nographic\n")
    assert result_of(t) == "not_required"
    assert "VM" in finding(t)[1]


def test_a_worker_next_to_an_unprotected_run_is_reported_as_partial(tmp_path):
    t = _task(tmp_path, WORKER + "\n" + RUN_AS_ROOT.split("\n\n", 1)[1].replace("test_x", "test_y"))
    severity, message = finding(t)
    assert result_of(t) == "present" and severity == "warn"
    assert "also" in message


def test_the_worker_stops_the_two_old_warnings(tmp_path):
    names = _results(_task(tmp_path, WORKER))
    assert "verifier_tests_readable" not in names
    assert names["verifier_runs_agent_code_in_worker"][0] == "info"


def test_it_never_breaks_the_stage(tmp_path):
    assert "verifier_worker_required" in _results(_task(tmp_path, "def broken(:\n"))
