"""Flag 15.39: expected answers stored in clear text under tests/ (Blocker where hashing fits).

Why: Tier 1 gives the attacker a read-only copy of tests/, and a grader whose data files hold the answers
can be beaten by copying them (found live on int8). Where the grader compares exact, unguessable values the
answers should be stored as SHA-256 fingerprints and the grader should hash the agent's output. Where hashing
cannot apply (tolerances, a generator in tests/, tiny answer spaces) the finding is advisory and says why.
Hashing is not the whole fix: readable inputs plus a reachable answer source still leak.
"""
import hashlib
import json

import pytest

from app.services import flag_rules as fr
from tests.test_flags import fired, make_task

BIG = "the quick brown fox jumps over the lazy dog 0123456789"


def case(expected=BIG, **extra):
    return json.dumps({"input": "x", "expected": expected, **extra})


def run(tmp_path, files, test_py="def test_a():\n    assert True\n"):
    return fired(make_task(tmp_path, test_py=test_py, files=files), "15.39")


def test_rule_is_catalogued_as_a_blocker_static_rule():
    rule = fr.RULES["15.39"]
    assert rule.severity == fr.BLOCKER and rule.method == "static"
    assert fr.group_of("15.39", "static") == "static"


def test_plain_exact_answers_are_a_blocker_with_evidence_and_the_fix(tmp_path):
    [f] = run(tmp_path, {"tests/data/case1.json": case()})
    assert f.severity is None  # the rule's own severity: Blocker
    assert "tests/data/case1.json" in f.evidence[0] and "expected" in f.evidence[0]
    assert "_sha256" in f.detail and "hash" in f.detail.lower()
    assert "reachable" in f.detail  # says hashing alone does not close an answer source the agent can query


def test_nested_lists_and_jsonl_are_read(tmp_path):
    files = {
        "tests/cases.json": json.dumps({"cases": [{"input": 1, "expected_output": {"rows": list(range(30))}}]}),
        "tests/more.jsonl": case() + "\n" + case(BIG + " two"),
    }
    [f] = run(tmp_path, files)
    assert any("cases.json" in e for e in f.evidence) and any("more.jsonl" in e for e in f.evidence)


@pytest.mark.parametrize("name", ["expected", "answer", "ground_truth", "golden", "reference_output", "expected_output", "transform_expect", "compose_expect", "expectedResult", "oracle_answers"])
def test_answer_like_key_names(tmp_path, name):
    assert run(tmp_path, {"tests/d.json": json.dumps({name: BIG})})


def test_already_hashed_answers_are_quiet(tmp_path):
    digest = hashlib.sha256(BIG.encode()).hexdigest()
    assert run(tmp_path, {"tests/d.json": json.dumps({"expected_sha256": digest, "input": "x"})}) == []
    assert run(tmp_path, {"tests/d.json": json.dumps({"expected": digest})}) == []
    assert run(tmp_path, {"tests/d.json": json.dumps({"expected": BIG, "expected_sha256": digest})}) == []


@pytest.mark.parametrize("value", ["yes", True, 3, "ok", "pass", 1.5])
def test_tiny_or_guessable_answers_are_not_counted(tmp_path, value):
    assert run(tmp_path, {"tests/d.json": json.dumps({"expected": value})}) == []


def test_a_task_with_no_data_files_is_quiet_and_says_what_it_looked_at(tmp_path):
    root = make_task(tmp_path)
    result = fr.STATIC_RULES["15.39"](fr.load_context(root))
    assert result.findings == [] and "0 data file" in result.note


def test_a_tolerance_grader_gets_an_advisory_not_a_blocker(tmp_path):
    test_py = "import math\n\ndef test_a():\n    assert math.isclose(1.0, 1.0, rel_tol=1e-6)\n"
    [f] = run(tmp_path, {"tests/d.json": case()}, test_py)
    assert f.severity == fr.ADVISORY and f.key == "not_hashable"
    assert "tolerance" in f.detail


def test_a_generator_in_tests_gets_an_advisory(tmp_path):
    [f] = run(tmp_path, {"tests/d.json": case(), "tests/generator/gen_cases.py": "print(1)\n"})
    assert f.severity == fr.ADVISORY and "generator" in f.detail


def test_the_test_data_folder_only_not_solution_or_environment(tmp_path):
    files = {"solution/expected.json": case(), "environment/data.json": case()}
    assert run(tmp_path, files) == []


def test_unreadable_json_is_skipped(tmp_path):
    assert run(tmp_path, {"tests/bad.json": "{not json"}) == []


def test_it_runs_with_the_other_static_rules(tmp_path):
    root = make_task(tmp_path, files={"tests/d.json": case()})
    assert "15.39" in fr.evaluate(fr.load_context(root))
    assert fr.evaluate(fr.load_context(root))["15.39"].findings


def test_input_like_keys_are_not_answers(tmp_path):
    assert run(tmp_path, {"tests/d.json": json.dumps({"transform": [BIG] * 3, "input": BIG, "compose": BIG, "name": BIG})}) == []
