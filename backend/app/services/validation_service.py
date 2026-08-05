import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from app.services.task_config import TaskConfig
from app.utils.ziputil import ZipSlipError, find_task_root, safe_extract

REQUIRED_ENTRIES = ["task.toml", "instruction.md", "environment", "solution", "tests"]


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    task_root: Path | None = None
    config: TaskConfig | None = None
    task_name: str | None = None


def validate_zip(zip_path: Path, extract_dir: Path) -> ValidationResult:
    """Extract the zip and check the task contract. Never raises — any failure
    (bad zip, missing files, malformed toml) is captured in the returned
    ValidationResult so callers can persist a clean 'failed' status."""
    try:
        safe_extract(zip_path, extract_dir)
    except ZipSlipError as e:
        return ValidationResult(ok=False, errors=[str(e)])
    except Exception as e:
        return ValidationResult(ok=False, errors=[f"failed to extract zip: {e}"])

    task_root = find_task_root(extract_dir)

    errors = []
    for entry in REQUIRED_ENTRIES:
        if not (task_root / entry).exists():
            errors.append(f"missing required entry: {entry}")

    if errors:
        return ValidationResult(ok=False, errors=errors, task_root=task_root)

    environment_dir = task_root / "environment"
    if not (environment_dir / "Dockerfile").is_file():
        errors.append("environment/Dockerfile not found")

    solve_sh = task_root / "solution" / "solve.sh"
    if not solve_sh.is_file():
        errors.append("solution/solve.sh not found")

    test_sh = task_root / "tests" / "test.sh"
    if not test_sh.is_file():
        errors.append("tests/test.sh not found")

    if errors:
        return ValidationResult(ok=False, errors=errors, task_root=task_root)

    toml_path = task_root / "task.toml"
    try:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        return ValidationResult(ok=False, errors=[f"invalid task.toml: {e}"], task_root=task_root)

    try:
        config = TaskConfig.from_toml_dict(data)
    except Exception as e:
        return ValidationResult(
            ok=False, errors=[f"invalid task.toml config values: {e}"], task_root=task_root
        )

    task_name = data.get("metadata", {}).get("name") or task_root.name

    return ValidationResult(
        ok=True, errors=[], task_root=task_root, config=config, task_name=task_name
    )
