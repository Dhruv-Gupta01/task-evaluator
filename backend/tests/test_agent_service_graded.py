"""`verifier_queries_agent_service`: a shared-verifier grader that queries a localhost service nothing in the
task starts. The only thing that can answer is a program the agent left running, so agent code keeps
running as the agent's user while the grader grades (outside any Worker). From the Worker chat: a server
the agent started cannot be wrapped in Worker; the grader only sends it requests."""
from pathlib import Path

from app.services.static_checks import StaticCheckReport
from app.services.verifier_isolation import run_isolation_checks

SHARED = '[task]\nname = "terminal-bench/x"\n'
SEPARATE = '[task]\nname = "terminal-bench/x"\n\n[verifier]\nenvironment_mode = "separate"\n'
QUERY = 'import requests\n\ndef test_up():\n    assert requests.get("http://localhost:8080/health").status_code == 200\n'


def task(tmp_path: Path, toml=SHARED, test_py=QUERY, test_sh="pytest /tests/test_x.py\n",
         dockerfile="FROM ubuntu:24.04\nWORKDIR /app\n", extra=None) -> Path:
    files = {"task.toml": toml, "environment/Dockerfile": dockerfile, "tests/test.sh": test_sh, "tests/test_x.py": test_py}
    files.update(extra or {})
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def found(root: Path):
    report = StaticCheckReport()
    run_isolation_checks(root, report)
    return {r.name: (r.severity, r.message) for r in report.results}.get("verifier_queries_agent_service")


def test_a_shared_grader_querying_an_unstarted_localhost_service_is_flagged(tmp_path):
    severity, message = found(task(tmp_path))
    assert severity == "warn" and "8080" in message and "agent" in message
    assert "separate" in message


def test_curl_in_test_sh_counts(tmp_path):
    root = task(tmp_path, test_py="def test_x():\n    pass\n", test_sh="curl -sf http://127.0.0.1:9000/ping\npytest /tests/test_x.py\n")
    assert found(root) and "9000" in found(root)[1]


def test_a_socket_connect_counts(tmp_path):
    root = task(tmp_path, test_py='import socket\n\ndef test_x():\n    socket.create_connection(("localhost", 7000), timeout=2)\n')
    assert found(root) and "7000" in found(root)[1]


def test_an_exposed_port_in_the_dockerfile_means_the_task_provides_it(tmp_path):
    assert found(task(tmp_path, dockerfile="FROM ubuntu:24.04\nEXPOSE 8080\nCMD [\"/srv/run.sh\"]\n")) is None


def test_a_compose_service_means_the_task_provides_it(tmp_path):
    compose = "services:\n  main:\n    build: .\n  api:\n    image: x\n    ports:\n      - '8080:8080'\n"
    assert found(task(tmp_path, extra={"environment/docker-compose.yaml": compose})) is None


def test_a_grader_that_starts_the_server_itself_is_not_flagged_here(tmp_path):
    started = 'import subprocess, requests\n\ndef test_up():\n    subprocess.Popen(["python3", "/app/server.py"])\n    assert requests.get("http://localhost:8080/").ok\n'
    assert found(task(tmp_path, test_py=started)) is None


def test_a_server_started_from_test_sh_is_not_flagged_here(tmp_path):
    root = task(tmp_path, test_py="def test_x():\n    pass\n",
                test_sh="python3 /app/server.py &\nsleep 2\ncurl -sf http://localhost:8080/\n")
    assert found(root) is None


def test_a_separate_verifier_cannot_see_a_server_the_agent_left_running(tmp_path):
    assert found(task(tmp_path, toml=SEPARATE)) is None


def test_no_localhost_query_means_nothing_to_say(tmp_path):
    assert found(task(tmp_path, test_py='def test_x():\n    assert open("/app/out.txt").read() == "1"\n')) is None
