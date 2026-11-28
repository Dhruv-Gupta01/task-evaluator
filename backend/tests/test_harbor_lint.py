"""Static Check `harbor_lint` (reward_kit_checks.check_harbor_lint): Harbor's own linter, run twice
(default rules with the author's settings, then --extend-select ALL), one finding per line."""

import json
from types import SimpleNamespace as NS

import pytest

from app.services import reward_kit_checks as rk


class Report:
    def __init__(self):
        self.items: list[tuple[str, str, str]] = []

    def add(self, name, severity, message):
        self.items.append((name, severity, message))

    def lines(self, severity=None):
        return [m for n, s, m in self.items if n == "harbor_lint" and (severity is None or s == severity)]


def finding(rule, message="a problem", path="task.toml", line=None):
    return {"rule": rule, "message": message, "path": path, **({"line": line} if line else {})}


@pytest.fixture
def lint(tmp_path, monkeypatch):
    """Fake `harbor lint`: `answers` maps 'base' / 'strict' to a list of findings (or None = unparseable)."""
    (tmp_path / "task.toml").write_text('[task]\nname = "x"\n')
    state = NS(answers={"base": [], "strict": []}, calls=[], available=True)
    monkeypatch.setattr(rk.versions, "_harbor_has_command", lambda *a, **k: state.available)
    monkeypatch.setattr(rk.versions, "harbor_version", lambda: "0.20.0")

    def fake_run(cmd, **kwargs):
        state.calls.append(cmd)
        key = "strict" if "--extend-select" in cmd else "base"
        answer = state.answers[key]
        if answer is None:
            return NS(stdout="Error: bad option", stderr="usage", returncode=2)
        return NS(stdout=json.dumps(answer), stderr="", returncode=1 if answer else 0)

    monkeypatch.setattr(rk.subprocess, "run", fake_run)
    state.root = tmp_path
    return state


def run(state):
    report = Report()
    rk.check_harbor_lint(state.root, report)
    return report


def test_says_so_when_this_harbor_has_no_lint(lint):
    lint.available = False
    report = run(lint)
    assert lint.calls == []
    assert report.items[0][1] == "info" and "not available in Harbor 0.20.0" in report.items[0][2]


def test_no_findings_anywhere(lint):
    report = run(lint)
    assert report.lines() == ["`harbor lint` found no problems."] and report.items[0][1] == "info"


def test_default_rule_findings_are_warnings_with_file_and_line(lint):
    lint.answers["base"] = [finding("shared-verifier", "Verifier shares the sandbox", path=str(lint.root / "task.toml"), line=7)]
    report = run(lint)
    assert report.items == [("harbor_lint", "warn", "[shared-verifier] task.toml:7: Verifier shares the sandbox")]


def test_the_stricter_run_adds_only_new_findings_as_info(lint):
    shared = finding("shared-verifier", "shared")
    lint.answers["base"] = [shared]
    lint.answers["strict"] = [shared, finding("unpinned-python-dependency", "pin it", path="tests/Dockerfile")]
    report = run(lint)
    assert report.lines("warn") == ["[shared-verifier] task.toml: shared"]
    info = report.lines("info")
    assert len(info) == 1 and info[0].startswith("[unpinned-python-dependency] tests/Dockerfile: pin it")
    assert "a stricter Harbor rule, not in its default set" in info[0]


def test_root_agent_is_left_to_the_agent_user_check(lint):
    lint.answers["strict"] = [finding("root-agent", "runs as root")]
    report = run(lint)
    assert not any("root-agent" in m for m in report.lines())
    assert report.lines() == ["`harbor lint` found no problems."]


def test_a_rule_the_author_ignored_is_shown_and_labelled(lint):
    (lint.root / "task.toml").write_text('[task]\nname = "x"\n[metadata.harbor.lint]\nignore = ["compose-writable-bind-mount"]\n')
    lint.answers["strict"] = [finding("compose-writable-bind-mount", "writable mount", path="environment/docker-compose.yaml")]
    info = run(lint).lines("info")
    assert len(info) == 1 and "the task's own lint settings ignore this rule" in info[0]
    assert "stricter Harbor rule" not in info[0]


def test_long_lists_are_capped_with_a_count(lint):
    lint.answers["base"] = [finding(f"rule-{i}", "m") for i in range(30)]
    lint.answers["strict"] = lint.answers["base"] + [finding(f"extra-{i}", "m") for i in range(20)]
    report = run(lint)
    warns, infos = report.lines("warn"), report.lines("info")
    assert len(warns) == 26 and warns[-1] == "... and 5 more `harbor lint` finding(s)"
    assert len(infos) == 16 and infos[-1] == "... and 5 more stricter `harbor lint` finding(s)"


def test_an_unparseable_default_run_is_reported_not_raised(lint):
    lint.answers["base"] = None
    report = run(lint)
    assert len(lint.calls) == 1  # the stricter run is not tried
    assert report.items[0][1] == "info" and "could not run" in report.items[0][2] and "Error: bad option" in report.items[0][2]


def test_a_failed_stricter_run_is_noted_when_nothing_else_was_found(lint):
    lint.answers["strict"] = None
    assert run(lint).lines() == ["`harbor lint` found no problems (the stricter run could not be done)."]


def test_the_task_path_comes_before_the_rule_options(lint):
    """--select/--extend-select take several values, so a path after them is read as a rule name."""
    run(lint)
    base, strict = lint.calls
    assert base[1:3] == ["lint", f"{lint.root}/"] and base[3] == "--json"
    assert strict[1:3] == ["lint", f"{lint.root}/"] and strict[-2:] == ["--extend-select", "ALL"]
    assert strict.index(f"{lint.root}/") < strict.index("--extend-select")


def test_a_lint_process_that_cannot_start_is_reported(lint, monkeypatch):
    def boom(cmd, **kwargs):
        raise FileNotFoundError("harbor")

    monkeypatch.setattr(rk.subprocess, "run", boom)
    report = run(lint)
    assert report.items[0][1] == "info" and "could not run" in report.items[0][2]
