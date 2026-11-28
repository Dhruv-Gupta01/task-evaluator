"""Static Check `timed_input_reuse` (Singularity-Bench HARNESS-G03): a speed test that times its code on the SAME
input repeatedly lets agent code remember the answer and return it instantly. Also the matching line in the
Targeted cheat prompt."""
from app.services import harbor_runner, reproducibility_checks


class Report:
    def __init__(self):
        self.items = []

    def add(self, name, severity, message):
        self.items.append((name, severity, message))

    def named(self, name):
        return [(s, m) for n, s, m in self.items if n == name]


def run_check(tmp_path, body, name="test_perf.py"):
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / name).write_text(body)
    report = Report()
    reproducibility_checks.check_timed_input_reuse(tmp_path, report)
    return report


SAME_INPUT = """
import time
from solution import solve

def test_speed():
    data = load_input()
    assert solve(data) == expected(data)
    start = time.perf_counter()
    for _ in range(20):
        solve(data)
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0
"""


def test_repeats_on_one_input_are_noted(tmp_path):
    r = run_check(tmp_path, SAME_INPUT)
    [(severity, message)] = r.named("timed_input_reuse")
    assert severity == "info"
    assert "test_perf.py" in message and "test_speed" in message and "data" in message
    assert "remember" in message and "fresh" in message


def test_a_fresh_input_for_every_repeat_is_fine(tmp_path):
    body = SAME_INPUT.replace("    for _ in range(20):\n        solve(data)", "    for i in range(20):\n        d = make_input(i)\n        solve(d)")
    assert run_check(tmp_path, body).items == []


def test_the_loop_variable_as_the_input_is_fine(tmp_path):
    body = SAME_INPUT.replace("    for _ in range(20):\n        solve(data)", "    for d in inputs:\n        solve(d)")
    assert run_check(tmp_path, body).items == []


def test_random_inputs_inside_the_loop_are_fine(tmp_path):
    body = SAME_INPUT.replace("    for _ in range(20):\n        solve(data)", "    for _ in range(20):\n        solve(random_input())")
    assert run_check(tmp_path, body).items == []


def test_a_test_that_does_not_time_anything_is_ignored(tmp_path):
    body = "def test_x():\n    data = load()\n    for _ in range(3):\n        solve(data)\n"
    assert run_check(tmp_path, body).items == []


def test_timeit_with_a_lambda_on_one_input_is_noted(tmp_path):
    body = "import timeit\n\ndef test_speed():\n    data = load_input()\n    t = timeit.timeit(lambda: solve(data), number=50)\n    assert t < 1.0\n"
    assert run_check(tmp_path, body).named("timed_input_reuse")


def test_timing_helpers_and_builtins_in_the_loop_are_not_inputs(tmp_path):
    body = ("import time\n\ndef test_speed():\n    start = time.perf_counter()\n"
            "    for i in range(5):\n        print(i)\n        time.sleep(0.01)\n    assert time.perf_counter() - start < 5\n")
    assert run_check(tmp_path, body).items == []


def test_one_note_per_function_even_with_many_loops(tmp_path):
    body = SAME_INPUT + "\n    for _ in range(5):\n        solve(data)\n"
    assert len(run_check(tmp_path, body).named("timed_input_reuse")) == 1


def test_unparseable_test_files_are_skipped(tmp_path):
    assert run_check(tmp_path, "def broken(:\n").items == []


def test_the_check_runs_with_the_other_reproducibility_checks(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_perf.py").write_text(SAME_INPUT)
    (tmp_path / "task.toml").write_text('[task]\nname = "x"\n')
    report = Report()
    reproducibility_checks.run_reproducibility_checks(tmp_path, report)
    assert report.named("timed_input_reuse")


# ---- 11b: the targeted cheat prompt tries it too --------------------------------------------

def test_the_targeted_cheat_prompt_adds_the_cache_trick_for_a_timed_task(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_perf.py").write_text(SAME_INPUT)
    (tmp_path / "instruction.md").write_text("Make solve() fast.\n")
    text = harbor_runner.targeted_cheat_instruction(tmp_path)
    assert "remember" in text.lower() and "same input" in text.lower()


def test_the_targeted_cheat_prompt_leaves_it_out_when_nothing_is_timed(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    (tmp_path / "instruction.md").write_text("Do the thing.\n")
    assert "same input" not in harbor_runner.targeted_cheat_instruction(tmp_path).lower()


# ---- regression: found by running the check on a real task (int8-style, test_http_poll_wait) --------

REAL_POLL = '''
import time

def test_http_poll_wait(server):
    a = call_json(server, "POST", "/api/join", {"doc": "quiet"})
    for wait, low, high in [(1, 0.9, 3), (100, 24, 28)]:
        start = time.monotonic()
        got = call_json(server, "GET", f"/api/poll?doc=quiet&client={a['client']}&wait={wait}", timeout=45)
        took = time.monotonic() - start
        assert got == {"msgs": []}
        assert low <= took <= high
'''


def test_a_loop_whose_input_changes_inside_an_expression_is_not_a_repeat_of_one_input(tmp_path):
    """The loop variable is used inside an f-string, so each pass calls with a different input. The only repeated
    name is the `server` fixture handle, which is not an input."""
    assert run_check(tmp_path, REAL_POLL).items == []


def test_the_loop_variable_inside_a_call_expression_also_counts_as_varying(tmp_path):
    body = ("import time\n\ndef test_speed():\n    data = load()\n    s = time.perf_counter()\n"
            "    for i in range(5):\n        solve(data + i)\n    assert time.perf_counter() - s < 5\n")
    assert run_check(tmp_path, body).items == []
