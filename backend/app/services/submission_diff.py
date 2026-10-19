"""What changed between two uploads of the same task (4.0 category A): files
added, removed and changed, and how many tests were removed or added. Lets a
reviewer see what a revision touched, and notice a fix that quietly deleted
the test that was failing."""

import hashlib
import re
from pathlib import Path

_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)", re.M)
_SKIP = {".git", "__pycache__", "node_modules", "__MACOSX"}
_MAX_LISTED = 40


def _files(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not _SKIP.intersection(path.relative_to(root).parts):
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _test_names(root: Path) -> set[str]:
    names: set[str] = set()
    tests = root / "tests"
    if tests.is_dir():
        for path in tests.rglob("*.py"):
            try:
                names |= set(_TEST_DEF.findall(path.read_text(errors="replace")))
            except OSError:
                continue
    return names


def _words(root: Path) -> int | None:
    path = root / "instruction.md"
    return len(path.read_text(errors="replace").split()) if path.is_file() else None


def diff_tasks(old_root: Path, new_root: Path) -> dict:
    old, new = _files(old_root), _files(new_root)
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(f for f in set(old) & set(new) if old[f] != new[f])
    old_tests, new_tests = _test_names(old_root), _test_names(new_root)
    return {
        "added": added[:_MAX_LISTED],
        "removed": removed[:_MAX_LISTED],
        "changed": changed[:_MAX_LISTED],
        "counts": {"added": len(added), "removed": len(removed), "changed": len(changed)},
        "tests_removed": sorted(old_tests - new_tests)[:_MAX_LISTED],
        "tests_added": sorted(new_tests - old_tests)[:_MAX_LISTED],
        "tests_removed_count": len(old_tests - new_tests),
        "tests_added_count": len(new_tests - old_tests),
        "instruction_words": {"before": _words(old_root), "after": _words(new_root)},
    }
