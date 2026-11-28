"""Grader rules from the post-fixes branch's Flags (services/grader_rules.py), run as Static Checks here. Every rule
has a fixture it must fire on and one it must not fire on (the post-fixes tests, unchanged); then the mapping to
Static Checks: no rule fails (15.14 is exact but warns, since every real Terminal-Bench task carries only the public
GUID), warn for every heuristic rule, info for 15.24's near-limit case."""

from pathlib import Path

import pytest

from app.services import grader_rules as gr
from tests._fixtures import make_task

PRIVATE = "0f0e0d0c-1111-2222-3333-444455556666"


def fired(root: Path, rule: str, **ctx) -> list[gr.Finding]:
    return gr.STATIC_RULES[rule](gr.load_context(root, **ctx)).findings


PSQL = 'import subprocess\ndef q(sql):\n    return subprocess.run(["psql", "-c", sql])\n'


def test_15_2_unqualified_function_in_agent_database(tmp_path):
    bad = make_task(tmp_path / "a", PSQL + 'q(f"SELECT md5(string_agg(x::text, \',\')) FROM ledger.{t} x")\n')
    good = make_task(tmp_path / "b", PSQL + 'q("SELECT pg_catalog.md5(pg_catalog.string_agg(x::text, \',\')) FROM t x")\n')
    assert "md5, string_agg" in fired(bad, "15.2")[0].evidence[0]
    assert fired(good, "15.2") == []


def test_15_4_root_unpack_of_agent_archive(tmp_path):
    code = 'import subprocess\nOUT = "/app/output"\nsubprocess.run(f"tar -xzf {OUT}/data.tgz -C /tmp/x", shell=True)\n'
    assert fired(make_task(tmp_path / "a", code), "15.4")
    dropped = code.replace("tar -xzf", "runuser -u postgres -- tar -xzf")
    assert fired(make_task(tmp_path / "b", dropped), "15.4") == []
    assert fired(make_task(tmp_path / "c", code, verifier_user="nobody"), "15.4") == []


def test_15_5_superuser_statement_in_agent_database(tmp_path):
    assert fired(make_task(tmp_path / "a", PSQL + 'q("CREATE EXTENSION pgcrypto")\n'), "15.5")
    assert fired(make_task(tmp_path / "b", PSQL + 'q("SELECT 1")\n'), "15.5") == []


RUN_PROGRAM = (
    'import subprocess\nPROG = "/app/prog.py"\n'
    'def test_x():\n    subprocess.run(["python3", "-I", "-S", PROG], user=65534, timeout=5)\n'
)


def test_15_7_isolated_interpreter_not_in_instruction(tmp_path):
    loose = "Implement `/app/prog.py`, a program using only the standard library that runs as `python3 /app/prog.py`.\n"
    told = loose + "It must be a single self-contained file.\n"
    findings = fired(make_task(tmp_path / "a", RUN_PROGRAM, instruction=loose), "15.7")
    assert findings and any(e.startswith("-I ") for e in findings[0].evidence)
    assert not any(e.startswith("-S ") for e in findings[0].evidence), "-S is disclosed by 'standard library'"
    assert fired(make_task(tmp_path / "b", RUN_PROGRAM, instruction=told), "15.7") == []


def test_15_14_public_canary_only(tmp_path):
    assert fired(make_task(tmp_path / "a"), "15.14")
    both = make_task(tmp_path / "b", files={"tests/notes.txt": f"# harbor-canary GUID {PRIVATE}\n"})
    assert fired(both, "15.14") == []


def test_15_16_rubric_in_reward(tmp_path):
    sh = "#!/bin/bash\nscore=$(python3 /tests/grade.py /tests/rubric.md)\necho $score > /logs/verifier/reward.txt\n"
    assert fired(make_task(tmp_path / "a", test_sh=sh), "15.16")
    assert fired(make_task(tmp_path / "b"), "15.16") == []


def test_15_21_path_from_environment(tmp_path):
    assert fired(make_task(tmp_path / "a", 'import os\nD = os.environ.get("TEST_DIR", "/tests")\n'), "15.21")
    assert fired(make_task(tmp_path / "b", 'import os\np = os.environ.get("PATH")\n'), "15.21") == []


def test_15_22_agent_program_without_memory_bound(tmp_path):
    assert fired(make_task(tmp_path / "a", RUN_PROGRAM), "15.22")
    bounded = "import resource\nresource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))\n" + RUN_PROGRAM
    assert fired(make_task(tmp_path / "b", bounded), "15.22") == []


BUDGET = (
    "import subprocess\nfrom pathlib import Path\nimport pytest\n"
    'CASES = sorted(Path("/tests/cases").glob("*.json"))\nLIMIT = 30\n'
    '@pytest.mark.parametrize("case", CASES)\n'
    'def test_case(case):\n    subprocess.run(["python3", "/app/p.py", str(case)], timeout=LIMIT)\n'
)


def test_15_24_case_budget(tmp_path):
    files = {f"tests/cases/c{i}.json": "{}" for i in range(10)}
    over = make_task(tmp_path / "a", BUDGET, verifier_timeout=200, files=files)
    findings = fired(over, "15.24")
    assert findings and findings[0].severity is None and "10 x 30 s = 300 s of 200 s" in findings[0].evidence[0]
    near = make_task(tmp_path / "b", BUDGET, verifier_timeout=360, files=files)
    assert fired(near, "15.24")[0].severity == gr.ADVISORY
    roomy = make_task(tmp_path / "c", BUDGET, verifier_timeout=1000, files=files)
    assert fired(roomy, "15.24") == []


def test_15_26_private_canary_reused(tmp_path):
    root = make_task(tmp_path / "a", files={"tests/notes.txt": f"# harbor-canary GUID {PRIVATE}\n"})
    assert fired(root, "15.26", other_canaries={PRIVATE: ["terminal-bench/other-task"]})
    assert fired(root, "15.26", other_canaries={}) == []


def test_15_35_unbounded_read_of_agent_artifact(tmp_path):
    code = 'import json\nfrom pathlib import Path\nOUT = Path("/app/out")\ndef test_r():\n    return json.loads((OUT / "r.json").read_text())\n'
    assert fired(make_task(tmp_path / "a", code), "15.35")
    bounded = code.replace("def test_r():\n", "def test_r():\n    assert (OUT / 'r.json').stat().st_size < 1 << 20\n")
    assert fired(make_task(tmp_path / "b", bounded), "15.35") == []
    test_data = 'from pathlib import Path\nT = Path("/tests/x.json")\ndef f():\n    return T.read_text()\n'
    assert fired(make_task(tmp_path / "c", test_data), "15.35") == []


def test_a_broken_rule_is_reported_not_hidden(tmp_path, monkeypatch):
    def boom(ctx):
        raise RuntimeError("bad input")

    monkeypatch.setitem(gr.STATIC_RULES, "15.2", boom)
    results = gr.evaluate(gr.load_context(make_task(tmp_path / "a")))
    assert results["15.2"].note == "rule error: RuntimeError: bad input"
    assert results["15.14"].findings


# ---- as Static Checks -----------------------------------------------------------------------

class Report:
    def __init__(self):
        self.items: list[tuple[str, str, str]] = []

    def add(self, name, severity, message):
        self.items.append((name, severity, message))

    def named(self, name):
        return [(s, m) for n, s, m in self.items if n == name]


def checks(root: Path, **kw) -> Report:
    report = Report()
    gr.check_grader_rules(root, report, **kw)
    return report


def test_the_public_canary_only_rule_warns_and_nothing_fails(tmp_path):
    report = checks(make_task(tmp_path / "a"))
    [(severity, message)] = report.named("canary_public_only")
    assert severity == "warn" and "(15.14)" in message
    assert all(s != "fail" for _, s, _ in report.items)


def test_a_task_with_a_private_canary_passes_the_canary_check(tmp_path):
    root = make_task(tmp_path / "a", files={"tests/notes.txt": f"# harbor-canary GUID {PRIVATE}\n"})
    assert checks(root).named("canary_public_only") == []


def test_heuristic_rules_warn_never_fail(tmp_path):
    sh = "#!/bin/bash\nscore=$(python3 /tests/grade.py /tests/rubric.md)\necho $score > /logs/verifier/reward.txt\n"
    root = make_task(tmp_path / "a", RUN_PROGRAM, test_sh=sh, files={"tests/notes.txt": f"# harbor-canary GUID {PRIVATE}\n"})
    report = checks(root)
    for name in ("rubric_feeds_reward", "verifier_no_memory_bound"):
        assert report.named(name) and all(s == "warn" for s, _ in report.named(name)), name


def test_the_time_budget_is_warn_when_over_and_info_when_close(tmp_path):
    files = {f"tests/cases/c{i}.json": "{}" for i in range(10)}
    over = checks(make_task(tmp_path / "a", BUDGET, verifier_timeout=200, files=files)).named("verifier_time_budget")
    near = checks(make_task(tmp_path / "b", BUDGET, verifier_timeout=360, files=files)).named("verifier_time_budget")
    assert [s for s, _ in over] == ["warn"] and [s for s, _ in near] == ["info"]


def test_canary_reuse_needs_the_other_uploads(tmp_path):
    root = make_task(tmp_path / "a", files={"tests/notes.txt": f"# harbor-canary GUID {PRIVATE}\n"})
    assert checks(root).named("canary_reused") == []
    [(severity, message)] = checks(root, other_canaries={PRIVATE: ["terminal-bench/other"]}).named("canary_reused")
    assert severity == "warn" and "terminal-bench/other" in message


def test_a_rule_that_crashes_is_listed_as_info(tmp_path, monkeypatch):
    def boom(ctx):
        raise RuntimeError("bad input")

    monkeypatch.setitem(gr.STATIC_RULES, "15.2", boom)
    [(severity, message)] = checks(make_task(tmp_path / "a")).named("verifier_unqualified_sql")
    assert severity == "info" and "could not run" in message
