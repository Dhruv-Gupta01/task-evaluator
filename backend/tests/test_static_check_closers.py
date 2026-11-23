"""NR-17, NR-18 and NR-85: three more Static Checks (advisory, no Docker, no model).

NR-17  verifier_trusts_printed_text   a grader that runs the agent's program and decides from a success word the
                                      program prints. Code the grader runs can print any text (mvcc-lsm-compaction).
NR-18  lean_output_parsed             a Lean grader that decides from text in Lean's own output. Submitted Lean can
                                      write text into that output (#eval, run_cmd, macros) (takens-embedding-lean).
NR-85  timing_single_sample           a test that asserts on one timing measurement, no repeats, no median or minimum.

Each has cases that must stay quiet as well, because these are heuristics and false alarms are the risk."""
from pathlib import Path

import pytest

from app.services import reproducibility_checks
from tests.test_verifier_isolation_detection import _results, _task

RUN = 'import subprocess\n\ndef test_x():\n    proc = subprocess.run(["python3", "/app/solution.py"], capture_output=True, text=True)\n    out = proc.stdout\n'


# ---- NR-17: printed success markers ---------------------------------------------------------

@pytest.mark.parametrize("assertion", [
    'assert "ALL TESTS PASSED" in out',
    'assert proc.stdout.strip() == "OK"',
    'assert "success" in proc.stdout.lower()',
    'assert "✓" in out',
    'assert re.search(r"PASS", out)',
    'if "DONE" in out:\n        return',
])
def test_a_success_word_printed_by_agent_code_is_flagged(tmp_path, assertion):
    found = _results(_task(tmp_path, "import re\n" + RUN + f"    {assertion}\n")).get("verifier_trusts_printed_text")
    assert found is not None, assertion
    severity, message = found
    assert severity == "warn" and "test_x.py" in message
    assert "print" in message and "computes" in message  # says what to do instead


@pytest.mark.parametrize("assertion", [
    "assert float(out) == 3.0",
    'assert "error" not in out',
    'assert out.splitlines()[0] == "42"',
    "assert proc.returncode == 0",
])
def test_grading_from_values_or_negative_checks_is_quiet(tmp_path, assertion):
    assert "verifier_trusts_printed_text" not in _results(_task(tmp_path, RUN + f"    {assertion}\n"))


def test_a_marker_check_on_a_trusted_programs_output_is_quiet(tmp_path):
    """pytest's own 'passed' line is the grader's tool talking, not code the agent wrote."""
    body = 'import subprocess\n\ndef test_x():\n    proc = subprocess.run(["pytest", "/tests/test_inner.py"], capture_output=True, text=True)\n    assert "passed" in proc.stdout\n'
    assert "verifier_trusts_printed_text" not in _results(_task(tmp_path, body))


def test_a_task_that_runs_no_agent_code_is_quiet(tmp_path):
    body = 'def test_x():\n    out = "ALL TESTS PASSED"\n    assert "ALL TESTS PASSED" in out\n'
    assert "verifier_trusts_printed_text" not in _results(_task(tmp_path, body))


def test_each_file_is_reported_once_with_its_lines(tmp_path):
    body = RUN + '    assert "PASSED" in out\n    assert "OK" in proc.stdout\n'
    severity, message = _results(_task(tmp_path, body))["verifier_trusts_printed_text"]
    assert message.count("tests/test_x.py") == 1 or "test_x.py:" in message


# ---- NR-18: Lean output parsed --------------------------------------------------------------

def lean_task(tmp_path, test_py="", test_sh="lake build\n"):
    t = _task(tmp_path, test_py or "def test_x():\n    assert True\n", test_sh)
    (t / "solution").mkdir(exist_ok=True)
    (t / "solution" / "Main.lean").write_text("theorem t : True := trivial\n")
    return t


@pytest.mark.parametrize("test_sh", [
    'lake build 2>&1 | grep -q "does not depend on any axioms"\n',
    'lean Main.lean | grep "propext"\n',
    'out=$(lake build 2>&1)\necho "$out" | grep -q "Build completed"\n',
])
def test_a_shell_grader_that_greps_lean_output_is_flagged(tmp_path, test_sh):
    severity, message = _results(lean_task(tmp_path, test_sh=test_sh))["lean_output_parsed"]
    assert severity == "warn"
    assert "#eval" in message and "output" in message


def test_a_python_grader_that_looks_for_text_in_lean_output_is_flagged(tmp_path):
    body = ('import subprocess\n\ndef test_x():\n    proc = subprocess.run(["lake", "build"], capture_output=True, text=True)\n'
            '    assert "no axioms" in proc.stdout\n')
    assert "lean_output_parsed" in _results(lean_task(tmp_path, test_py=body))


@pytest.mark.parametrize("test_sh", [
    "lake build\n",
    'lake build 2>&1 | grep -v "warning"\n',
    "lean --run Check.lean && test $? -eq 0\n",
])
def test_a_grader_that_trusts_only_the_exit_code_or_filters_noise_is_quiet(tmp_path, test_sh):
    assert "lean_output_parsed" not in _results(lean_task(tmp_path, test_sh=test_sh))


def test_a_negative_check_on_lean_output_is_quiet(tmp_path):
    body = ('import subprocess\n\ndef test_x():\n    proc = subprocess.run(["lake", "build"], capture_output=True, text=True)\n'
            '    assert "error" not in proc.stdout\n')
    assert "lean_output_parsed" not in _results(lean_task(tmp_path, test_py=body))


def test_a_task_without_lean_files_is_never_checked(tmp_path):
    t = _task(tmp_path, "def test_x():\n    assert True\n", 'lake build 2>&1 | grep -q "axioms"\n')
    assert "lean_output_parsed" not in _results(t)


# ---- NR-85: one timing sample ---------------------------------------------------------------

class Report:
    def __init__(self):
        self.items = []

    def add(self, name, severity, message):
        self.items.append((name, severity, message))

    def named(self, name):
        return [(s, m) for n, s, m in self.items if n == name]


def timing(tmp_path, body, name="test_perf.py"):
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / name).write_text(body)
    report = Report()
    reproducibility_checks.check_single_sample_timing(tmp_path, report)
    return report


SINGLE = """
import time
from solution import solve

def test_speed():
    start = time.perf_counter()
    solve(load())
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0
"""


def test_one_timing_sample_asserted_on_is_noted(tmp_path):
    [(severity, message)] = timing(tmp_path, SINGLE).named("timing_single_sample")
    assert severity == "info"
    assert "test_perf.py" in message and "test_speed" in message
    assert "median" in message or "minimum" in message


def test_the_assert_can_name_the_elapsed_time_inline(tmp_path):
    body = "import time\n\ndef test_speed():\n    t0 = time.perf_counter()\n    work()\n    assert time.perf_counter() - t0 < 1.5\n"
    assert timing(tmp_path, body).named("timing_single_sample")


@pytest.mark.parametrize("variant", [
    # several runs, the minimum
    "import time\n\ndef test_speed():\n    times = []\n    for _ in range(5):\n        s = time.perf_counter()\n        work()\n        times.append(time.perf_counter() - s)\n    assert min(times) < 1.0\n",
    # median of several
    "import time, statistics\n\ndef test_speed():\n    ts = [run_once() for _ in range(5)]\n    s = time.perf_counter()\n    work()\n    e = time.perf_counter() - s\n    assert statistics.median(ts) < 1.0\n",
    # timeit.repeat
    "import timeit\n\ndef test_speed():\n    t = min(timeit.repeat(work, number=1, repeat=5))\n    assert t < 1.0\n",
])
def test_repeats_with_a_minimum_or_median_are_quiet(tmp_path, variant):
    assert timing(tmp_path, variant).items == []


def test_a_timed_block_with_no_assertion_on_it_is_quiet(tmp_path):
    body = "import time\n\ndef test_speed():\n    s = time.perf_counter()\n    work()\n    print(time.perf_counter() - s)\n"
    assert timing(tmp_path, body).items == []


def test_a_test_that_does_not_time_anything_is_ignored(tmp_path):
    assert timing(tmp_path, "def test_x():\n    assert 1 < 2\n").items == []


def test_one_note_lists_every_such_function(tmp_path):
    body = SINGLE + "\n" + SINGLE.replace("test_speed", "test_other")
    [(_, message)] = timing(tmp_path, body).named("timing_single_sample")
    assert "test_speed" in message and "test_other" in message


def test_unparseable_files_are_skipped(tmp_path):
    assert timing(tmp_path, "def broken(:\n").items == []


def test_it_runs_with_the_other_reproducibility_checks(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_perf.py").write_text(SINGLE)
    (tmp_path / "task.toml").write_text('[task]\nname = "x"\n')
    report = Report()
    reproducibility_checks.run_reproducibility_checks(tmp_path, report)
    assert report.named("timing_single_sample")


def test_a_shell_grader_that_greps_the_agents_output_for_a_marker_is_flagged(tmp_path):
    t = _task(tmp_path, "def test_x():\n    assert True\n", 'python3 /app/solution.py | grep -q "ALL TESTS PASSED"\n')
    found = _results(t).get("verifier_trusts_printed_text")
    assert found and "test.sh" in found[1]


def test_a_shell_grader_that_filters_noise_out_is_quiet(tmp_path):
    t = _task(tmp_path, "def test_x():\n    assert True\n", 'python3 /app/solution.py | grep -v "debug"\n')
    assert "verifier_trusts_printed_text" not in _results(t)
