"""Builds the file dump the LLM judges read for a task directory.

The judges are told they see "every file" in a directory, so anything left out
must still be named: a judge that only sees a Dockerfile will otherwise report
the files it references as missing. Text is detected by content rather than by
extension, and a listing of every file (with size and whether its contents are
shown) comes first."""

import codecs
from pathlib import Path

from app.config import get_settings

MAX_LISTING_ENTRIES = 300

_SNIFF_BYTES = 4096


def _is_text(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(_SNIFF_BYTES)
    except OSError:
        return False
    if b"\0" in head:
        return False
    try:
        # Incremental decoder tolerates a multi-byte character cut at the end.
        codecs.getincrementaldecoder("utf-8")().decode(head)
    except UnicodeDecodeError:
        return False
    return True


def read_text_files(root: Path, max_total: int | None = None) -> str:
    settings = get_settings()
    max_file = settings.judge_max_file_chars
    max_total = max_total or settings.judge_max_total_chars
    listing: list[str] = []
    chunks: list[str] = []
    total = 0
    budget_reached = False
    files = [p for p in sorted(root.rglob("*")) if p.is_file()]

    for path in files:
        rel = path.relative_to(root)
        try:
            size = path.stat().st_size
        except OSError:
            continue

        if not _is_text(path):
            listing.append(f"- {rel} ({size} bytes, binary: contents not shown)")
            continue
        try:
            content = path.read_text(errors="replace")
        except OSError:
            listing.append(f"- {rel} ({size} bytes, unreadable)")
            continue

        note = ""
        if len(content) > max_file:
            content = content[:max_file] + "\n... [truncated]"
            note = f", truncated to the first {max_file} characters"
        chunk = f"--- {rel} ---\n{content}\n"
        if budget_reached or total + len(chunk) > max_total:
            budget_reached = True
            listing.append(f"- {rel} ({size} bytes, text: contents omitted, size budget reached)")
            continue
        chunks.append(chunk)
        total += len(chunk)
        listing.append(f"- {rel} ({size} bytes, text{note})")

    shown = listing[:MAX_LISTING_ENTRIES]
    if len(listing) > MAX_LISTING_ENTRIES:
        shown.append(f"- ... and {len(listing) - MAX_LISTING_ENTRIES} more files")
    header = f"FILE LISTING ({len(files)} files; every file exists even if its contents are not shown):\n"
    body = "\n".join(chunks)
    return header + "\n".join(shown) + ("\n\nFILE CONTENTS:\n" + body if body else "")
