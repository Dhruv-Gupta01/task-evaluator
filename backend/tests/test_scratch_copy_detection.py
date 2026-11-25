"""A grader that copies the agent's files into a scratch folder and runs them from there.

Found on collab-editor-desync: the verifier does `shutil.copytree(APP, root / "app")`, hands the folder
to a fixture (`work`), and starts `python3 app/server/server.py` with `cwd=work` through a helper.
The classifier only knew agent paths under /app, so it reported "not_required" for a grader that runs
the agent's server. Same behaviour on the main branch; this is the fix and its guards.
"""

from pathlib import Path

from app.services.verifier_isolation import analyze_worker_requirement
from tests._fixtures import make_task

COLLAB_SHAPE = '''
import os, shutil, subprocess, tempfile
from pathlib import Path
import pytest

APP = Path(os.environ.get("WICKPAD_APP", "/app/wickpad"))
PY = ["python3", "-S", "-E", "-B"]


def as_nobody(work):
    return dict(cwd=work, user=65534, group=65534, start_new_session=True)


@pytest.fixture(scope="session")
def work():
    root = Path(tempfile.mkdtemp(prefix="wickpad-"))
    shutil.copytree(APP, root / "app")
    yield root
    shutil.rmtree(root, ignore_errors=True)


def run(work, argv, data):
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, **as_nobody(work))
    proc.communicate(b"{}")


def test_ot(work):
    run(work, PY + ["app/server/ot.py"], {})


def test_server(work):
    proc = subprocess.Popen(PY + ["app/server/server.py", "--port", "1"], **as_nobody(work))
    proc.kill()
'''


def _analyze(tmp_path: Path, test_py: str, test_sh: str = "chmod 700 /tests\npython3 -m pytest /tests/test_outputs.py\n") -> dict:
    return analyze_worker_requirement(make_task(tmp_path, test_py=test_py, test_sh=test_sh))


def test_a_scratch_copy_of_the_agents_app_that_is_run_counts(tmp_path):
    result = _analyze(tmp_path, COLLAB_SHAPE)
    assert result["result"] != "not_required"
    assert any("tests/test_outputs.py" in e for e in result["evidence"])


def test_the_helper_that_drops_privileges_is_seen_as_dropping_them(tmp_path):
    result = _analyze(tmp_path, COLLAB_SHAPE)
    assert "a separate unprivileged user" not in result["absent"]


def test_running_without_dropping_privileges_is_missing(tmp_path):
    undropped = COLLAB_SHAPE.replace("user=65534, group=65534, ", "")
    result = _analyze(tmp_path, undropped, test_sh="python3 -m pytest /tests/test_outputs.py\n")
    assert result["result"] == "missing"
    assert "a separate unprivileged user" in result["absent"]


def test_copying_the_graders_own_data_and_running_it_is_not_agent_code(tmp_path):
    own = COLLAB_SHAPE.replace('shutil.copytree(APP, root / "app")', 'shutil.copytree("/tests/data", root / "app")')
    own = own.replace('APP = Path(os.environ.get("WICKPAD_APP", "/app/wickpad"))\n', "")
    assert _analyze(tmp_path, own)["result"] == "not_required"


def test_a_grader_that_only_reads_the_agents_files_is_not_agent_code(tmp_path):
    reading = '''
from pathlib import Path

def test_output():
    assert Path("/app/out.json").read_text().strip() == "{}"
'''
    assert _analyze(tmp_path, reading)["result"] == "not_required"


def test_copying_the_agents_files_without_running_them_is_not_agent_code(tmp_path):
    copy_only = COLLAB_SHAPE.split("def run(")[0] + '''
def test_copy(work):
    assert (work / "app").exists()
'''
    assert _analyze(tmp_path, copy_only)["result"] == "not_required"
