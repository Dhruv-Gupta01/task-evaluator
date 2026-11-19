"""Static Check `harborignore`: dev tooling in the verifier sandbox, and rules that remove something needed."""

from pathlib import Path

from app.services import harborignore_checks as hc
from tests.test_flags import make_task
from tests.test_harborignore import write


class Report:
    def __init__(self):
        self.items: list[tuple[str, str, str]] = []

    def add(self, name, severity, message):
        self.items.append((name, severity, message))

    def of(self, severity):
        return [m for n, s, m in self.items if n == "harborignore" and s == severity]


def run(root: Path) -> Report:
    report = Report()
    hc.check_harborignore(root, report)
    return report


BASE = {"tests/test.sh": "#!/bin/bash\npython3 -m pytest /tests/checks.py\n", "tests/checks.py": "assert True\n",
        "solution/solve.sh": "#!/bin/bash\npython3 /solution/solve.py\n", "solution/solve.py": "print(1)\n"}


def task(tmp_path, extra=None, base=None):
    return write(tmp_path / "t", {**(BASE if base is None else base), **(extra or {})})


def test_a_plain_task_is_quiet(tmp_path):
    assert run(task(tmp_path)).items == []


def test_a_generator_nobody_uses_is_flagged_in_tests(tmp_path):
    warns = run(task(tmp_path, {"tests/generator/make_expected.py": "x = 1\n"})).of("warn")
    assert len(warns) == 1 and "tests/generator/make_expected.py" in warns[0]
    assert "authoring tooling" in warns[0] and "rule such as `generator/` to `tests/.harborignore`" in warns[0]


def test_a_script_named_like_a_generator_is_flagged_without_a_folder(tmp_path):
    warns = run(task(tmp_path, {"tests/gen_fixtures.py": "x = 1\n"})).of("warn")
    assert len(warns) == 1 and "is named like a generator or build script" in warns[0]


def test_a_rule_that_covers_it_silences_the_warning_and_is_listed(tmp_path):
    report = run(task(tmp_path, {"tests/generator/gen.py": "x\n", "tests/.harborignore": "generator/\n"}))
    assert report.of("warn") == [] and report.of("fail") == []
    info = report.of("info")
    assert len(info) == 1 and "keeps 1 file(s) out" in info[0] and "`tests/generator/gen.py`" in info[0]


def test_tooling_the_task_really_uses_is_left_alone(tmp_path):
    used_by_script = task(tmp_path, {"tests/gen_data.py": "x\n", "tests/test.sh": "python3 /tests/gen_data.py\npytest\n"})
    assert run(used_by_script).of("warn") == []
    used_by_folder = write(tmp_path / "u", {**BASE, "tests/generator/gen.py": "x\n", "tests/checks.py": "from generator import gen\n"})
    assert run(used_by_folder).of("warn") == []


def test_tooling_in_solution_is_only_information(tmp_path):
    report = run(task(tmp_path, {"solution/maintainer/rebuild.py": "x\n"}))
    assert report.of("warn") == [] and len(report.of("info")) == 1 and "copied for the oracle" in report.of("info")[0]


def test_excluding_the_test_script_fails(tmp_path):
    fails = run(task(tmp_path, {"tests/.harborignore": "test.sh\n"})).of("fail")
    assert len(fails) == 1 and "tests/test.sh" in fails[0] and "refuses to load" in fails[0]
    assert len(run(task(tmp_path / "s", {"solution/.harborignore": "solve.sh\n"})).of("fail")) == 1


def test_excluding_a_file_the_task_still_names_warns(tmp_path):
    root = task(tmp_path, {"tests/data/expected.json": "{}\n", "tests/.harborignore": "data/\n",
                           "tests/checks.py": "EXPECTED = open('/tests/data/expected.json')\n"})
    warns = run(root).of("warn")
    assert len(warns) == 1 and "tests/data/expected.json" in warns[0] and "tests/checks.py" in warns[0]
    assert "the verifier will not have this file" in warns[0]


def test_an_excluded_file_nobody_names_is_only_listed(tmp_path):
    report = run(task(tmp_path, {"tests/notes.md": "n\n", "tests/.harborignore": "notes.md\n"}))
    assert report.of("warn") == [] and report.of("fail") == [] and "`tests/notes.md`" in report.of("info")[0]


def test_an_excluded_build_file_is_not_a_problem(tmp_path):
    root = task(tmp_path, {"tests/Dockerfile": "FROM scratch\n", "tests/.harborignore": "Dockerfile\n",
                           "tests/test.sh": "docker build -f Dockerfile .\n"})
    assert run(root).of("warn") == []


def test_long_lists_are_capped(tmp_path):
    root = task(tmp_path, {f"tests/generator/g{i}.py": "x\n" for i in range(14)})
    report = run(root)
    assert len(report.of("warn")) == 10 and "and 4 more file(s)" in report.of("info")[-1]


def test_step_folders_are_checked_too(tmp_path):
    root = task(tmp_path, {"steps/s1/tests/test.sh": "x\n", "steps/s1/tests/generator/g.py": "x\n"}, base={})
    assert any("steps/s1/tests/generator/g.py" in m for m in run(root).of("warn"))


def test_it_runs_inside_static_checks(tmp_path, monkeypatch):
    from app.services import static_checks, versions

    monkeypatch.setattr(versions, "_harbor_has_command", lambda *a, **k: False)  # no real harbor lint here
    root = make_task(tmp_path / "t", files={"tests/generator/gen.py": "x = 1\n"})
    report = static_checks.run_static_checks(root)
    assert any(r.name == "harborignore" and r.severity == "warn" for r in report.results)
