import io
import zipfile
from pathlib import Path

from app.services.validation_service import _task_slug, validate_zip

TOML = '[task]\nname = "{name}"\n\n[agent]\ntimeout_sec = 100.0\n'


def _zip(tmp_path: Path, toml: str) -> Path:
    path = tmp_path / "renamed-upload.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("task.toml", toml)
        z.writestr("instruction.md", "x")
        z.writestr("environment/Dockerfile", "FROM scratch\n")
        z.writestr("solution/solve.sh", "x")
        z.writestr("tests/test.sh", "x")
    return path


def test_slug_drops_the_org_prefix():
    assert _task_slug({"task": {"name": "terminal-bench/collab-editor-desync"}}) == "collab-editor-desync"
    assert _task_slug({"task": {"name": "plain-name"}}) == "plain-name"


def test_slug_is_none_without_a_task_name():
    assert _task_slug({}) is None
    assert _task_slug({"task": {}}) is None
    assert _task_slug({"task": {"name": 5}}) is None
    assert _task_slug({"task": {"name": "org/"}}) is None
    assert _task_slug({"metadata": {"name": "ignored"}}) is None


def test_validation_names_the_task_from_toml_not_the_zip(tmp_path):
    result = validate_zip(_zip(tmp_path, TOML.format(name="terminal-bench/my-task")), tmp_path / "x")
    assert result.ok, result.errors
    assert result.task_name == "my-task"
