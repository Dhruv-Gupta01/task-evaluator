"""Banked rollout artifacts, and the verifier fingerprint that dates a baseline.

A Harbor rollout keeps what the agent produced under `<rollout>/artifacts/`, with a
`manifest.json` mapping each declared artifact's absolute container path to its file in
the bundle. That makes every past rollout a free regression fixture: Harbor's own
versioning says a verifier-only change is a MINOR bump *because* the agent output stays
valid, and this module is how that gets cashed in.

The bundles are agent output. Feeding one to a verifier that runs pytest as root is the
same trust boundary finding P1 is about, so the guards are here, at load time, and a
bundle that trips one is refused rather than quietly repaired:

  * no entry may land outside the bundle's own artifacts directory (manifest traversal)
  * no entry may restore under /logs, which is where the reward lives

Harbor itself adds a `/logs/artifacts` entry to every manifest; that one is dropped with
a note, since it is Harbor's own collection rather than a declared artifact.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from app.services.harborignore import TaskIgnore

MANIFEST = "manifest.json"
SKIP_PREFIXES = ("/logs",)


class BadBundle(ValueError):
    pass


@dataclass
class Bundle:
    key: str                      # stable id, relative to the search root
    root: Path                    # the artifacts/ directory
    origin: str                   # human label, e.g. "rollout, status=ok x6"
    entries: dict = field(default_factory=dict)   # absolute artifact path -> local Path
    skipped: list = field(default_factory=list)   # (source, why)

    def file_for(self, artifact: str) -> Path | None:
        return self.entries.get(artifact)

    def covers(self, artifacts: list[str]) -> list[str]:
        return [a for a in artifacts if a in self.entries]

    def matches(self, spec) -> bool:
        """True when this bundle was produced by the task `spec` describes.

        Decided by overlap with the declared artifact paths rather than by directory
        naming, so it keeps working whatever the team calls its run directories.
        """
        return bool(self.covers(spec.artifacts))


def load(artifacts_dir: str | Path, search_root: str | Path | None = None) -> Bundle:
    root = Path(artifacts_dir).resolve()
    manifest_path = root / MANIFEST
    if not manifest_path.is_file():
        raise BadBundle(
            f"{root}: no {MANIFEST}. Harbor writes one next to the collected artifacts; "
            f"without it there is no trustworthy mapping from container path to file."
        )
    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BadBundle(f"{manifest_path}: unparseable: {exc}") from exc
    if not isinstance(doc, list):
        raise BadBundle(f"{manifest_path}: expected a list of entries, got {type(doc).__name__}")

    base = search_root and Path(search_root).resolve()
    try:
        key = str(root.relative_to(base)) if base else root.name
    except ValueError:
        key = str(root)

    entries: dict[str, Path] = {}
    skipped: list[tuple[str, str]] = []
    statuses: dict[str, int] = {}

    for raw in doc:
        if not isinstance(raw, dict) or "source" not in raw:
            raise BadBundle(f"{manifest_path}: unsupported entry {raw!r}")
        source = raw["source"]
        status = raw.get("status", "?")
        statuses[status] = statuses.get(status, 0) + 1

        if source.startswith(SKIP_PREFIXES):
            skipped.append((source, "under /logs: Harbor's own collection, not a declared "
                                    "artifact, and /logs is where the reward lives"))
            continue
        if status != "ok":
            skipped.append((source, f"manifest status={status!r}"))
            continue

        dest = raw.get("destination")
        if not dest:
            skipped.append((source, "no destination in manifest"))
            continue
        # `destination` is written relative to the rollout directory, i.e. it normally
        # starts with "artifacts/". Resolve it there, then require it to have landed
        # inside this bundle: a manifest is a file like any other and may lie.
        resolved = (root.parent / dest).resolve()
        if resolved != root and root not in resolved.parents:
            raise BadBundle(
                f"{manifest_path}: entry {source!r} points to {dest!r}, which resolves "
                f"outside the bundle ({resolved}). Refusing to replay it."
            )
        if not resolved.exists():
            skipped.append((source, f"file missing at {dest}"))
            continue
        entries[source] = resolved

    if not entries:
        raise BadBundle(
            f"{root}: no usable artifacts. Skipped: "
            + "; ".join(f"{s} ({why})" for s, why in skipped[:6])
        )

    origin = ", ".join(f"{n} {st}" for st, n in sorted(statuses.items()))
    return Bundle(key=key, root=root, origin=origin, entries=entries, skipped=skipped)


def discover(search_root: str | Path, spec=None) -> list[Bundle]:
    """Find every bundle under `search_root`, optionally only this task's.

    Returns them sorted by key, so a baseline file and a later comparison iterate in the
    same order whatever the filesystem does.
    """
    base = Path(search_root).resolve()
    found: list[Bundle] = []
    for manifest_path in sorted(base.rglob(MANIFEST)):
        if manifest_path.parent.name != "artifacts":
            continue
        if any(p in {".git", "node_modules", "results"} for p in manifest_path.parts):
            continue
        try:
            bundle = load(manifest_path.parent, base)
        except BadBundle:
            continue
        if spec is None or bundle.matches(spec):
            found.append(bundle)
    return sorted(found, key=lambda b: b.key)


def verifier_fingerprint(spec) -> str:
    """A short digest of tests/, so a baseline records which verifier produced it.

    A fingerprint mismatch is the normal case after a fix, not an error: it is how the
    report can say "baseline was recorded against a different verifier" instead of
    letting someone compare against a stale file without noticing.
    """
    h = hashlib.sha256()
    tests = spec.root / "tests"
    ignore = TaskIgnore(spec.root)  # excluded files are not part of the verifier
    for path in sorted(p for p in tests.rglob("*") if p.is_file() and not ignore.is_excluded(p)):
        h.update(str(path.relative_to(tests)).encode())
        h.update(b"\0")
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def recorded_reward(bundle: "Bundle") -> float | None:
    """The reward Harbor itself recorded for this rollout (`<trial>/result.json`), which is
    what a replay is compared against. None when it is missing or unreadable."""
    try:
        doc = json.loads((bundle.root.parent / "result.json").read_text(encoding="utf-8"))
        value = ((doc.get("verifier_result") or {}).get("rewards") or {}).get("reward")
        return None if value is None else float(value)
    except Exception:
        return None


def collect(submission_dir: str | Path, spec, kinds=("oracle", "agent"), limit: int = 3) -> list[tuple["Bundle", str]]:
    """Rollouts stored by the platform for one submission: `runs/<kind>/harbor/<job>/<trial>/
    artifacts/`. Only Oracle and agent runs (not Nop, cheat trials or stress runs, whose
    outputs are not a normal attempt). Newest first, at most `limit`, and only bundles that
    cover this task's declared artifacts. Returns (bundle, kind)."""
    base = Path(submission_dir) / "runs"
    found: list[tuple[float, Bundle, str]] = []
    for kind in kinds:
        for manifest in (base / kind).glob("harbor/*/*/artifacts/" + MANIFEST):
            try:
                bundle = load(manifest.parent, base)
            except BadBundle:
                continue
            if bundle.matches(spec):
                found.append((manifest.stat().st_mtime, bundle, kind))
    found.sort(key=lambda t: t[0], reverse=True)
    return [(b, k) for _, b, k in found[:limit]]
