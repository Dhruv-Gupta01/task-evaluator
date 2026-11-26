"""Hashed expected answers (docs/hash-rules.md): the static check and the proof run.

The cases come from the trial of 2026-11-26: answers hashed in JSON, a Python constant and a shell
variable; accepted alternatives; "must not match" hashes; and a grader that computes a hash but decides
on a plain value.
"""
import asyncio
import hashlib
import json
from pathlib import Path

from app.services import hash_checks as hc
from app.services.static_checks import StaticCheckReport
from tests._fixtures import make_task


def h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def task(tmp_path: Path, files: dict[str, str], test_py: str = "def test_x():\n    pass\n") -> Path:
    return make_task(tmp_path, test_py=test_py, files=files)


def by_name(root: Path) -> dict:
    return {e.name: e for e in hc.find_entries(root)}


def static(root: Path) -> dict:
    report = StaticCheckReport()
    hc.check_hashed_answers(root, report)
    return {r.name: (r.severity, r.message) for r in report.results}


# ---- finding the entries -------------------------------------------------------------------------

def test_json_keys_lists_and_must_not_entries_are_found(tmp_path):
    data = {"target_sha256": h("a"), "time_sha256": [h("1"), h("2")], "naive_not_sha256": h("b"), "rows": 3}
    found = by_name(task(tmp_path, {"tests/expected.json": json.dumps(data, indent=1)}))
    assert set(found) == {"target_sha256", "time_sha256", "naive_not_sha256"}
    assert found["target_sha256"].must_match and not found["naive_not_sha256"].must_match
    assert len(found["time_sha256"].digests) == 2


def test_a_python_constant_dict_gives_one_entry_per_answer(tmp_path):
    code = f'EXPECTED_SHA256 = {{\n    "odd": "{h("1")}",\n    "even": "{h("2")}",\n}}\n'
    assert set(by_name(task(tmp_path, {}, test_py=code))) == {"EXPECTED_SHA256.odd", "EXPECTED_SHA256.even"}


def test_a_shell_variable_is_found(tmp_path):
    sh = f'expected_sha256="{h("Example Domain")}"\n[ "$a" = "$expected_sha256" ] && echo 1\n'
    assert set(by_name(task(tmp_path, {"tests/test.sh": sh}))) == {"expected_sha256"}


def test_lock_files_image_digests_and_computed_values_are_not_entries(tmp_path):
    files = {
        "tests/uv.lock": f'sha256 = "{h("w")}"\nwheel_sha256 = "{h("w")}"\n',
        "tests/Dockerfile": f"FROM python@sha256:{h('i')}\n",
    }
    code = 'import hashlib\n\ndef test_x():\n    file_sha256 = hashlib.sha256(b"x").hexdigest()\n    assert file_sha256 == E["a_sha256"]\n'
    assert by_name(task(tmp_path, files, test_py=code)) == {}


# ---- the static check ----------------------------------------------------------------------------

def test_no_hash_at_all_fails(tmp_path):
    severity, message = static(task(tmp_path, {}))["hash_answers"]
    assert severity == "fail" and "_sha256" in message


def test_a_hashed_task_passes_and_says_how_many(tmp_path):
    data = {"a_sha256": h("a"), "b_not_sha256": h("b")}
    severity, message = static(task(tmp_path, {"tests/expected.json": json.dumps(data)}))["hash_answers"]
    assert severity == "info" and "1 must match" in message and "1 must not match" in message


def test_a_plain_answer_is_a_warning_even_beside_a_hash(tmp_path):
    data = {"a_sha256": h("a"), "expected": "the plain answer text"}
    results = static(task(tmp_path, {"tests/expected.json": json.dumps(data)}, test_py="EXPECTED = {'sum': 50}\n"))
    severity, message = results["tests_plain_answers"]
    assert severity == "warn" and "expected.json" in message and "EXPECTED" in message


def test_no_plain_answer_seen_is_not_a_promise(tmp_path):
    severity, message = static(task(tmp_path, {"tests/expected.json": json.dumps({"a_sha256": h("a")})}))["tests_plain_answers"]
    assert severity == "info" and "reviewer" in message


# ---- damaging one entry --------------------------------------------------------------------------

def test_damage_changes_only_that_entry_even_when_digests_repeat(tmp_path):
    data = {"a_sha256": h("same"), "b_sha256": h("same")}
    root = task(tmp_path, {"tests/expected.json": json.dumps(data, indent=1)})
    entry = by_name(root)["b_sha256"]
    changed = json.loads(hc.damaged_text((root / entry.file).read_text(), entry, h("other")))
    assert changed == {"a_sha256": h("same"), "b_sha256": h("other")}


def test_a_list_is_damaged_as_a_whole(tmp_path):
    root = task(tmp_path, {"tests/expected.json": json.dumps({"t_sha256": [h("1"), h("2"), h("3")]})})
    entry = by_name(root)["t_sha256"]
    changed = json.loads(hc.damaged_text((root / entry.file).read_text(), entry, h("x")))
    assert changed["t_sha256"] == [h("x")] * 3


# ---- the proof run (with a pretend grader) -------------------------------------------------------

def grader(data_file: str, accepts):
    """A fake Oracle: reads the copy's data file and returns 1 or 0 the way a real grader would."""
    calls = []

    async def run(copy_root: Path, index: int):
        calls.append(index)
        return accepts(json.loads((copy_root / data_file).read_text()))

    return run, calls


def prove(root, run, tmp_path, **kw):
    return asyncio.run(hc.run_proof(root, run, tmp_path / "work", **kw))


def results_by_name(out):
    return {e["name"]: e["result"] for e in out["entries"]}


def test_a_used_hash_is_used_and_the_run_passes(tmp_path):
    root = task(tmp_path / "t", {"tests/expected.json": json.dumps({"a_sha256": h("right")})})
    run, _ = grader("tests/expected.json", lambda d: float(d["a_sha256"] == h("right")))
    out = prove(root, run, tmp_path)
    assert out["status"] == "passed" and out["verdict"] == "all_used" and results_by_name(out) == {"a_sha256": "used"}
    assert json.loads((root / "tests/expected.json").read_text()) == {"a_sha256": h("right")}  # the task is untouched


def test_a_hash_nobody_reads_is_not_used_and_fails(tmp_path):
    root = task(tmp_path / "t", {"tests/expected.json": json.dumps({"a_sha256": h("right"), "decoy_sha256": h("d")})})
    run, _ = grader("tests/expected.json", lambda d: float(d["a_sha256"] == h("right")))
    out = prove(root, run, tmp_path)
    assert out["status"] == "failed" and out["verdict"] == "unused_hash"
    assert results_by_name(out) == {"a_sha256": "used", "decoy_sha256": "not_used"}


def test_must_not_entries_are_tested_by_giving_them_the_right_answer(tmp_path):
    data = {"a_sha256": h("right"), "naive_not_sha256": h("wrong")}
    root = task(tmp_path / "t", {"tests/expected.json": json.dumps(data)})
    run, _ = grader("tests/expected.json",
                    lambda d: float(h("right") == d["a_sha256"] and h("right") != d["naive_not_sha256"]))
    out = prove(root, run, tmp_path)
    assert out["status"] == "passed" and results_by_name(out)["naive_not_sha256"] == "used"


def test_a_must_not_entry_that_never_bites_needs_a_person(tmp_path):
    data = {"a_sha256": h("right"), "naive_not_sha256": h("wrong")}
    root = task(tmp_path / "t", {"tests/expected.json": json.dumps(data)})
    run, _ = grader("tests/expected.json", lambda d: float(d["a_sha256"] == h("right")))
    out = prove(root, run, tmp_path)
    assert out["status"] == "failed" and out["verdict"] == "needs_review"
    assert results_by_name(out)["naive_not_sha256"] == "unconfirmed"


def test_a_run_with_no_reward_proves_nothing(tmp_path):
    root = task(tmp_path / "t", {"tests/expected.json": json.dumps({"a_sha256": h("right")})})
    run, _ = grader("tests/expected.json", lambda d: None)
    out = prove(root, run, tmp_path)
    assert out["status"] == "failed" and out["verdict"] == "needs_review" and results_by_name(out) == {"a_sha256": "no_result"}


def test_no_hashes_means_nothing_to_prove(tmp_path):
    root = task(tmp_path / "t", {})
    run, calls = grader("tests/x.json", lambda d: 1.0)
    out = prove(root, run, tmp_path)
    assert out["status"] == "failed" and out["verdict"] == "no_hashes" and calls == []


def test_the_run_limit_leaves_the_rest_not_run(tmp_path):
    data = {f"k{i}_sha256": h(str(i)) for i in range(3)}
    root = task(tmp_path / "t", {"tests/expected.json": json.dumps(data)})
    run, calls = grader("tests/expected.json", lambda d: 0.0)
    out = prove(root, run, tmp_path, max_runs=2)
    assert len(calls) == 2 and out["verdict"] == "needs_review" and list(results_by_name(out).values()).count("not_run") == 1
