"""Version class between two uploads of one task (S9, CONTRIBUTING): which files changed,
the bump they call for, the bump the author declared, and what has to be redone.

Major: instruction.md or environment/ changed (agents must run again). Minor: tests/
changed, the verifier (saved trials can be regraded). Patch: anything else, such as the
README, task.toml metadata or solution/ (saved results stay valid).

On this branch it feeds the Changes banner (GET /submissions/{id}/changes, `version`) and the evidence summary as
plain warnings (`warnings`): a declared bump smaller than the files call for, no X.Y.Z version, instruction.md
changed, and locked items that changed. There is no ruling or named approval: only AI Detection has a manual
ruling here. On the post-fixes branch the same checks were Flags 15.18, 15.25, 15.30 and 15.34."""

import difflib
import hashlib
import re
import tomllib
from pathlib import Path

# The canary line Harbor tasks carry (`<!-- harbor-canary GUID ... -->`); the same pattern as the post-fixes
# branch's flag_rules.CANARY_RE.
CANARY_RE = re.compile(r"harbor-canary GUID ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)

CLASSES = ("patch", "minor", "major")
REDO = {
    "major": "Run the agent trials again: saved rollouts no longer match the task.",
    "minor": "Regrade the saved trials with the new verifier; agents need not run again.",
    "patch": "Saved results stay valid.",
}
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _files(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def changed_files(old: Path, new: Path) -> list[str]:
    a, b = _files(old), _files(new)
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def file_class(rel: str) -> str:
    if rel == "instruction.md" or rel.startswith("environment/"):
        return "major"
    if rel.startswith("tests/"):
        return "minor"
    return "patch"


def suggested_class(changed: list[str]) -> str | None:
    if not changed:
        return None
    return max((file_class(f) for f in changed), key=CLASSES.index)


def task_version(root: Path) -> str | None:
    try:
        data = tomllib.loads((root / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    version = (data.get("task") or {}).get("version")
    return str(version) if version is not None else None


def declared_class(old_version: str | None, new_version: str | None) -> str | None:
    """The bump between two X.Y.Z versions; None when either is missing or malformed, 'none' when equal."""
    a = _VERSION_RE.match(old_version or "")
    b = _VERSION_RE.match(new_version or "")
    if not a or not b:
        return None
    x, y = tuple(map(int, a.groups())), tuple(map(int, b.groups()))
    if y <= x:
        return "none"
    return "major" if y[0] > x[0] else "minor" if y[1] > x[1] else "patch"


def _toml(root: Path) -> dict:
    try:
        return tomllib.loads((root / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _canaries(root: Path) -> set[str]:
    found = set()
    for p in root.rglob("*"):
        if p.is_file():
            try:
                with p.open("r", encoding="utf-8", errors="ignore") as fh:
                    found |= {g.lower() for g in CANARY_RE.findall("".join(fh.readline() for _ in range(5)))}
            except OSError:
                continue
    return found


_EXPECTED_RE = re.compile(r"(?:^|/)(?:expected|golden|gold|answers?|ground[_-]?truth)[^/]*", re.I)


def locked_changes(old: Path, new: Path, changed: list[str]) -> list[str]:
    """Locked items (handover step 0) that differ: canary, artifacts allowlist, timeouts,
    expected values, instruction.md. Each needs a named approver."""
    out = []
    if _canaries(old) != _canaries(new):
        out.append(f"canary GUIDs: {sorted(_canaries(old))} -> {sorted(_canaries(new))}")
    a, b = _toml(old), _toml(new)
    if a.get("artifacts") != b.get("artifacts"):
        out.append(f"artifacts allowlist: {a.get('artifacts')} -> {b.get('artifacts')}")
    for section in ("agent", "verifier"):
        x, y = (a.get(section) or {}).get("timeout_sec"), (b.get(section) or {}).get("timeout_sec")
        if x != y:
            out.append(f"[{section}] timeout_sec: {x} -> {y}")
    out += [f"expected values: {f}" for f in changed if f.startswith("tests/") and _EXPECTED_RE.search(f)]
    if "instruction.md" in changed:
        out.append("instruction.md")
    return out


def instruction_diff(old: Path, new: Path, limit: int = 12) -> list[str]:
    """The lines of instruction.md that were removed (-) or added (+), at most `limit`."""
    def lines(root: Path) -> list[str]:
        try:
            return (root / "instruction.md").read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
    diff = [l for l in difflib.unified_diff(lines(old), lines(new), lineterm="", n=0)
            if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    return [l if len(l) <= 160 else l[:157] + "..." for l in diff[:limit]]


def compare(old: Path, new: Path) -> dict:
    changed = changed_files(old, new)
    suggested = suggested_class(changed)
    old_v, new_v = task_version(old), task_version(new)
    declared = declared_class(old_v, new_v)
    return {
        "changed": changed,
        "by_class": {c: [f for f in changed if file_class(f) == c] for c in CLASSES},
        "suggested_class": suggested,
        "old_version": old_v,
        "new_version": new_v,
        "declared_class": declared,
        "redo": REDO.get(suggested or "", "Nothing changed."),
        "locked_changes": locked_changes(old, new, changed),
        "instruction_diff": instruction_diff(old, new) if "instruction.md" in changed else [],
    }


def warnings(cmp: dict) -> list[str]:
    """Plain-words warnings for the Changes banner and the evidence. Advisory: nothing waits on them."""
    out: list[str] = []
    suggested, declared = cmp.get("suggested_class"), cmp.get("declared_class")
    if suggested is None:
        return out
    if cmp.get("new_version") is None or declared is None:
        out.append(
            f"no [task] version in X.Y.Z form to compare ({cmp.get('old_version')} -> {cmp.get('new_version')}); "
            f"the changed files call for a {suggested} bump"
        )
    elif declared == "none" or CLASSES.index(declared) < CLASSES.index(suggested):
        out.append(
            f"version {cmp['old_version']} -> {cmp['new_version']}: declared {declared}, the files call for {suggested}. "
            f"Bump at least the {suggested} version. To redo: {cmp['redo']}"
        )
    if "instruction.md" in cmp.get("changed", []):
        out.append("instruction.md changed: saved rollouts no longer match the task; a person should read the diff")
    others = [c for c in cmp.get("locked_changes", []) if c != "instruction.md"]
    if others:
        out.append(f"{len(others)} locked item(s) changed: " + "; ".join(others[:6]) + (" ..." if len(others) > 6 else ""))
    return out
