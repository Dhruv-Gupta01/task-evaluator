"""Version class between two uploads of one task (services/versioning.py): which files changed, the bump they
call for, the bump the author declared, what to redo and the locked items that changed. Adapted from the
post-fixes branch, where these were Flags 15.18, 15.25, 15.30 and 15.34; here they are plain warnings on the
Changes banner, with no ruling (only AI Detection has a manual ruling on this branch)."""

import shutil
from pathlib import Path

from app.services import versioning
from tests._fixtures import make_task

PRIVATE = "11111111-2222-4333-8444-555555555555"


def pair(tmp_path: Path, v1: str = "1.0.0", v2: str = "1.0.1") -> tuple[Path, Path]:
    old = make_task(tmp_path / "v1", test_py="def test_a():\n    assert True\n", version=v1,
                    files={"tests/expected.json": '{"a": 1}\n'})
    new = tmp_path / "v2"
    shutil.copytree(old, new)
    toml = (new / "task.toml").read_text().replace(f'version = "{v1}"', f'version = "{v2}"')
    (new / "task.toml").write_text(toml)
    return old, new


def text(cmp: dict) -> str:
    return " | ".join(versioning.warnings(cmp))


def test_tests_only_change_is_minor(tmp_path):
    old, new = pair(tmp_path, "1.0.0", "1.1.0")
    (new / "tests" / "test_outputs.py").write_text("def test_a():\n    assert 1 == 1\n")
    cmp = versioning.compare(old, new)
    assert cmp["suggested_class"] == "minor" and cmp["declared_class"] == "minor"
    assert "Regrade" in cmp["redo"]
    assert versioning.warnings(cmp) == []


def test_tests_change_declared_as_patch_warns(tmp_path):
    old, new = pair(tmp_path, "1.0.0", "1.0.1")
    (new / "tests" / "test_outputs.py").write_text("def test_a():\n    assert 1 == 1\n")
    assert "declared patch, the files call for minor" in text(versioning.compare(old, new))


def test_instruction_only_change_is_major_and_shows_the_changed_lines(tmp_path):
    old, new = pair(tmp_path, "1.0.0", "2.0.0")
    (new / "instruction.md").write_text((new / "instruction.md").read_text() + "The program must be one file.\n")
    cmp = versioning.compare(old, new)
    assert cmp["suggested_class"] == "major" and "agent trials again" in cmp["redo"]
    assert any("must be one file" in line for line in cmp["instruction_diff"])
    warned = text(cmp)
    assert "declared" not in warned  # the bump is right
    assert "instruction.md changed" in warned and "instruction.md" in cmp["locked_changes"]


def test_instruction_change_declared_minor_warns(tmp_path):
    old, new = pair(tmp_path, "1.0.0", "1.1.0")
    (new / "instruction.md").write_text("Something else.\n")
    assert "declared minor, the files call for major" in text(versioning.compare(old, new))


def test_canary_change_is_a_locked_item(tmp_path):
    old, new = pair(tmp_path, "1.0.0", "1.0.1")
    (new / "tests" / "notes.txt").write_text(f"# harbor-canary GUID {PRIVATE}\n")
    cmp = versioning.compare(old, new)
    assert any(e.startswith("canary GUIDs") for e in cmp["locked_changes"])
    assert "locked item" in text(cmp)


def test_timeouts_artifacts_and_expected_values_are_locked(tmp_path):
    old, new = pair(tmp_path, "1.0.0", "1.1.0")
    toml = (new / "task.toml").read_text()
    toml = toml.replace('artifacts = ["/app/out.json"]', 'artifacts = ["/app/out.json", "/app/log.txt"]')
    toml = toml.replace("[agent]\ntimeout_sec = 600.0", "[agent]\ntimeout_sec = 900.0")
    (new / "task.toml").write_text(toml)
    (new / "tests" / "expected.json").write_text('{"a": 2}\n')
    locked = versioning.compare(old, new)["locked_changes"]
    assert any(e.startswith("artifacts allowlist") for e in locked)
    assert "[agent] timeout_sec: 600.0 -> 900.0" in locked
    assert "expected values: tests/expected.json" in locked


def test_a_change_without_a_version_bump_warns(tmp_path):
    old, new = pair(tmp_path, "1.0.0", "1.0.0")
    (new / "solution" / "solve.sh").write_text("#!/bin/bash\necho changed\n")
    assert "declared none, the files call for patch" in text(versioning.compare(old, new))


def test_a_missing_or_malformed_version_warns(tmp_path):
    old, new = pair(tmp_path)
    (new / "task.toml").write_text((new / "task.toml").read_text().replace('version = "1.0.1"\n', ""))
    (new / "README.md").write_text("changed\n")
    cmp = versioning.compare(old, new)
    assert cmp["new_version"] is None and cmp["declared_class"] is None
    assert "no [task] version in X.Y.Z form" in text(cmp)


def test_nothing_changed_means_nothing_to_redo_and_no_warning(tmp_path):
    old, new = pair(tmp_path, "1.0.0", "1.0.0")
    cmp = versioning.compare(old, new)
    assert cmp["changed"] == [] and cmp["suggested_class"] is None and cmp["redo"] == "Nothing changed."
    assert versioning.warnings(cmp) == []


def test_the_canary_pattern_is_the_harbor_one():
    assert versioning.CANARY_RE.findall(f"<!-- harbor-canary GUID {PRIVATE} -->") == [PRIVATE]


# ---- GET /changes and the evidence -----------------------------------------------------------

def test_the_changes_endpoint_and_the_evidence_carry_the_version_class(tmp_path):
    import json
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db import Base
    from app.models import Submission
    from app.routers.submissions import get_changes, get_evidence_bundle

    old, new = pair(tmp_path, "1.0.0", "1.0.1")
    (new / "instruction.md").write_text("Something else.\n")
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    db.add(Submission(id="old1", task_name="t", uploaded_at=now - timedelta(hours=1), zip_path="x", extracted_path=str(old)))
    db.add(Submission(id="new1", task_name="t", uploaded_at=now, zip_path="x", extracted_path=str(new)))
    db.commit()
    version = get_changes("new1", None, db)["version"]
    assert version["suggested_class"] == "major" and version["declared_class"] == "patch"
    assert any("declared patch, the files call for major" in w for w in version["warnings"])
    bundle = json.loads(get_evidence_bundle("new1", db).body)
    assert bundle["changes"]["against"] == "old1"
    assert "## Changes since the earlier upload" in bundle["summary_markdown"]
    assert "declared patch, the files call for major" in bundle["summary_markdown"]
    assert "changes" not in json.loads(get_evidence_bundle("old1", db).body)  # nothing earlier to compare
