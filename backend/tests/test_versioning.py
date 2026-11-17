"""Batch 3: version class and change control between two uploads (S9, 15.18, 15.25, 15.30, 15.34).
v1/v2 copies of one task with known changes: tests only, instruction only, canary changed."""

import shutil
from pathlib import Path

from app.services import flag_rules as fr
from app.services import versioning
from tests.test_flags import PRIVATE, make_task


def pair(tmp_path: Path, v1: str = "1.0.0", v2: str = "1.0.1") -> tuple[Path, Path]:
    old = make_task(tmp_path / "v1", test_py="def test_a():\n    assert True\n", version=v1,
                    files={"tests/expected.json": '{"a": 1}\n'})
    new = tmp_path / "v2"
    shutil.copytree(old, new)
    toml = (new / "task.toml").read_text().replace(f'version = "{v1}"', f'version = "{v2}"')
    (new / "task.toml").write_text(toml)
    return old, new


def flags_for(old: Path, new: Path) -> dict[str, list[fr.Finding]]:
    results = fr.evaluate(fr.load_context(new, previous_root=old))
    return {rule: res.findings for rule, res in results.items() if res.findings}


def test_tests_only_change_is_minor(tmp_path):
    old, new = pair(tmp_path, "1.0.0", "1.1.0")
    (new / "tests" / "test_outputs.py").write_text("def test_a():\n    assert 1 == 1\n")
    cmp = versioning.compare(old, new)
    assert cmp["suggested_class"] == "minor" and cmp["declared_class"] == "minor"
    assert "Regrade" in cmp["redo"]
    got = flags_for(old, new)
    assert "15.18" not in got and "15.30" not in got and "15.34" not in got


def test_tests_change_declared_as_patch_is_flagged(tmp_path):
    old, new = pair(tmp_path, "1.0.0", "1.0.1")
    (new / "tests" / "test_outputs.py").write_text("def test_a():\n    assert 1 == 1\n")
    got = flags_for(old, new)
    assert "declared patch, the files call for minor" in got["15.18"][0].evidence[0]


def test_instruction_only_change_is_major_and_needs_review(tmp_path):
    old, new = pair(tmp_path, "1.0.0", "2.0.0")
    (new / "instruction.md").write_text((new / "instruction.md").read_text() + "The program must be one file.\n")
    cmp = versioning.compare(old, new)
    assert cmp["suggested_class"] == "major" and "agent trials again" in cmp["redo"]
    got = flags_for(old, new)
    assert "15.18" not in got
    assert any("must be one file" in e for e in got["15.30"][0].evidence)
    assert "instruction.md" in got["15.34"][0].evidence


def test_instruction_change_declared_minor_is_flagged(tmp_path):
    old, new = pair(tmp_path, "1.0.0", "1.1.0")
    (new / "instruction.md").write_text("Something else.\n")
    assert "declared minor, the files call for major" in flags_for(old, new)["15.18"][0].evidence[0]


def test_canary_change_is_a_locked_item(tmp_path):
    old, new = pair(tmp_path, "1.0.0", "1.0.1")
    (new / "tests" / "notes.txt").write_text(f"# harbor-canary GUID {PRIVATE}\n")
    got = flags_for(old, new)
    assert any(e.startswith("canary GUIDs") for e in got["15.34"][0].evidence)


def test_timeouts_artifacts_and_expected_values_are_locked(tmp_path):
    old, new = pair(tmp_path, "1.0.0", "1.1.0")
    toml = (new / "task.toml").read_text()
    toml = toml.replace('artifacts = ["/app/out.json"]', 'artifacts = ["/app/out.json", "/app/log.txt"]')
    toml = toml.replace("[agent]\ntimeout_sec = 600.0", "[agent]\ntimeout_sec = 900.0")
    (new / "task.toml").write_text(toml)
    (new / "tests" / "expected.json").write_text('{"a": 2}\n')
    evidence = flags_for(old, new)["15.34"][0].evidence
    assert any(e.startswith("artifacts allowlist") for e in evidence)
    assert "[agent] timeout_sec: 600.0 -> 900.0" in evidence
    assert "expected values: tests/expected.json" in evidence


def test_no_version_is_flagged_and_not_bumped_is_flagged(tmp_path):
    old, new = pair(tmp_path, "1.0.0", "1.0.0")
    (new / "solution" / "solve.sh").write_text("#!/bin/bash\necho changed\n")
    got = flags_for(old, new)
    assert "declared none, the files call for patch" in got["15.18"][0].evidence[0]
    bare = make_task(tmp_path / "bare")
    (bare / "task.toml").write_text((bare / "task.toml").read_text().replace('version = "1.0.0"\n', ""))
    assert fr.STATIC_RULES["15.25"](fr.load_context(bare)).findings


def test_without_an_earlier_upload_the_version_rules_say_so(tmp_path):
    results = fr.evaluate(fr.load_context(make_task(tmp_path / "only")))
    for rule in ("15.18", "15.30", "15.34"):
        assert results[rule].findings == [] and "no earlier upload" in results[rule].note
