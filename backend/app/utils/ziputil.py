import zipfile
from pathlib import Path


class ZipSlipError(Exception):
    pass


def safe_extract(zip_path: Path, dest_dir: Path) -> None:
    """Extract a zip file, refusing any entry that would escape dest_dir
    (the classic "zip slip" path-traversal attack via ../ or absolute paths).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    resolved_dest = dest_dir.resolve()

    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            member_path = (dest_dir / member).resolve()
            if not str(member_path).startswith(str(resolved_dest)):
                raise ZipSlipError(f"unsafe path in zip: {member}")
        zf.extractall(dest_dir)


def find_task_root(extracted_dir: Path) -> Path:
    """The zip may contain the task files directly, or nested one level under
    a single top-level directory (common when zipping a folder). Return the
    directory that actually contains task.toml."""
    if (extracted_dir / "task.toml").is_file():
        return extracted_dir

    entries = [p for p in extracted_dir.iterdir() if p.is_dir()]
    if len(entries) == 1 and (entries[0] / "task.toml").is_file():
        return entries[0]

    return extracted_dir
