import json
import os
import stat
import zipfile
from datetime import datetime
from pathlib import Path

# Written next to the extraction folder (never inside it, so it is not part of the task):
# what the zip said about each entry that extraction loses. A symlink entry is written as
# a plain file holding the link target, and no modification time is kept.
ZIP_ENTRIES_FILE = "zip_entries.json"
_MAX_ENTRIES_RECORDED = 5000


class ZipSlipError(Exception):
    pass


def safe_extract(zip_path: Path, dest_dir: Path) -> None:
    """Extract a zip file, refusing any entry that would escape dest_dir
    (the classic "zip slip" path-traversal attack via ../ or absolute paths).

    Also normalizes backslash path separators to forward slashes before
    extracting. The zip format mandates '/' as the separator regardless of
    the creating OS, but some Windows zip tools store raw '\\'-joined paths
    anyway — extracted naively, "environment\\src\\x.ts" lands as one oddly
    named flat file instead of a real nested environment/src/x.ts, which
    silently breaks the required-entry contract for every folder in the zip.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    resolved_dest = dest_dir.resolve()

    with zipfile.ZipFile(zip_path) as zf:
        normalized_targets: list[tuple[zipfile.ZipInfo, Path]] = []
        for info in zf.infolist():
            normalized_name = info.filename.replace("\\", "/")
            member_path = (dest_dir / normalized_name).resolve()
            if not str(member_path).startswith(str(resolved_dest)):
                raise ZipSlipError(f"unsafe path in zip: {info.filename}")
            normalized_targets.append((info, member_path))

        entries: list[dict] = []
        for info, member_path in normalized_targets:
            if info.filename.endswith("/") or info.filename.endswith("\\"):
                member_path.mkdir(parents=True, exist_ok=True)
                continue
            member_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(member_path, "wb") as dst:
                data = src.read()
                dst.write(data)
            if len(entries) < _MAX_ENTRIES_RECORDED:
                entry: dict = {"name": info.filename.replace("\\", "/")}
                if stat.S_ISLNK(info.external_attr >> 16):
                    entry["symlink"] = data.decode("utf-8", errors="replace")[:500]
                try:
                    entry["mtime"] = datetime(*info.date_time).timestamp()
                except (ValueError, OverflowError):
                    pass
                entries.append(entry)
            unix_mode = (info.external_attr >> 16) & 0o777
            if unix_mode:
                os.chmod(member_path, unix_mode)

    try:
        (dest_dir.parent / ZIP_ENTRIES_FILE).write_text(json.dumps(entries))
    except OSError:
        pass  # advisory metadata only


# Junk zip entries that macOS's Finder "Compress" (and plain `zip -r` without
# excludes) adds alongside the real content — must be ignored when deciding
# whether a zip has a single real top-level folder, or every Mac-zipped
# submission falsely looks like it has multiple top-level directories.
_IGNORED_TOP_LEVEL_NAMES = {"__MACOSX", ".DS_Store"}


def load_zip_entries(task_root: Path) -> list[dict] | None:
    """The sidecar written by safe_extract, found beside the extraction folder (the task
    root may be that folder or one level inside it). None for uploads from before it existed."""
    for folder in (task_root.parent, task_root.parent.parent):
        path = folder / ZIP_ENTRIES_FILE
        if path.is_file():
            try:
                return json.loads(path.read_text())
            except (OSError, ValueError):
                return None
    return None


def find_task_root(extracted_dir: Path) -> Path:
    """The zip may contain the task files directly, or nested one level under
    a single top-level directory (common when zipping a folder). Return the
    directory that actually contains task.toml."""
    if (extracted_dir / "task.toml").is_file():
        return extracted_dir

    entries = [
        p
        for p in extracted_dir.iterdir()
        if p.is_dir() and p.name not in _IGNORED_TOP_LEVEL_NAMES
    ]
    if len(entries) == 1 and (entries[0] / "task.toml").is_file():
        return entries[0]

    return extracted_dir
