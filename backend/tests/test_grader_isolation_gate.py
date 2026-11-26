"""Way 2: the "Grader Isolation" gate. Reads the way-1 result, adds `unclear`; nobody can overrule it
stored against a fingerprint of the grader's files. Advisory by default (GRADER_ISOLATION_ENFORCE off)."""
import pytest

from app.services import grader_isolation as gi
from tests.test_verifier_isolation_detection import _task
from tests.test_worker_required import RUN_AS_ROOT, WORKER

QUIET = 'def test_x():\n    assert open("/app/out.txt").read() == "1"\n'


def test_a_task_that_runs_no_agent_code_passes(tmp_path):
    e = gi.evaluate(_task(tmp_path, QUIET))
    assert e["state"] == "ok" and e["analysis"] == "not_required"


def test_a_worker_passes(tmp_path):
    assert gi.evaluate(_task(tmp_path, WORKER))["state"] == "ok"


def test_unprotected_agent_code_is_missing(tmp_path):
    e = gi.evaluate(_task(tmp_path, RUN_AS_ROOT))
    assert e["state"] == "missing" and "tests/test_x.py:4" in e["evidence"]


def test_a_worker_beside_an_unprotected_run_is_unclear(tmp_path):
    body = WORKER + "\n" + RUN_AS_ROOT.split("\n\n", 1)[1].replace("test_x", "test_y")
    assert gi.evaluate(_task(tmp_path, body))["state"] == "unclear"


def test_only_the_python_m_pytest_heuristic_is_unclear_not_missing(tmp_path):
    t = _task(tmp_path, QUIET, "cd /app\npython -m pytest /tests/test_x.py\n")
    (t / "task.toml").write_text('[task]\nname = "terminal-bench/x"\n')  # shared mode
    e = gi.evaluate(t)
    assert e["analysis"] == "missing" and e["state"] == "unclear"


def test_status_is_passed_only_for_ok():
    assert gi.gate_status("ok") == "passed"
    assert gi.gate_status("missing") == "failed" and gi.gate_status("unclear") == "failed"


def test_the_fingerprint_changes_when_the_grader_changes(tmp_path):
    t = _task(tmp_path, QUIET)
    a = gi.fingerprint(t)
    assert a == gi.fingerprint(t)
    (t / "tests" / "test_x.py").write_text(QUIET + "# edit\n")
    assert gi.fingerprint(t) != a
    b = gi.fingerprint(t)
    (t / "solution").mkdir(exist_ok=True)
    (t / "solution" / "solve.sh").write_text("echo hi\n")
    assert gi.fingerprint(t) == b  # the solution is not the grader


@pytest.mark.parametrize("state,enforce,blocks", [
    ("ok", True, False), ("missing", True, True), ("unclear", True, True),
    ("missing", False, False), ("unclear", False, False),
])
def test_it_blocks_only_when_enforced_and_not_ok(state, enforce, blocks):
    assert gi.blocks(state, enforce) is blocks
