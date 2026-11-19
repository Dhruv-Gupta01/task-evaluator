"""Checks on the probe suite itself, no Docker needed.

A mutant whose anchor no longer matches turns its positive control into a false negative,
which is the one failure this mechanism exists to prevent, so anchors are checked here
before any probe runs. Also: probe ids are unique, every control names a mutant that exists
and applies, every script parses under `bash -n`, and every injected source exists.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from app.services import harborignore

from . import mutants, probes
from .taskspec import TaskSpec

_FLAWS = {"-", "V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8"}
_KINDS = {"attack", "control", "valid_variant"}


def run(spec: TaskSpec | None = None) -> list[str]:
    """Returns a list of problems (empty = fine). With a task, also checks its mutants'
    anchors against the real tests/ and that injected sources exist."""
    errors: list[str] = []
    allp = probes.all_probes()

    seen: set[str] = set()
    for p in allp:
        if p.pid in seen:
            errors.append(f"duplicate probe id {p.pid}")
        seen.add(p.pid)
        if p.flaw not in _FLAWS:
            errors.append(f"{p.pid}: unknown flaw class {p.flaw!r}")
        if p.kind not in _KINDS:
            errors.append(f"{p.pid}: unknown kind {p.kind!r}")
        if p.kind == "valid_variant" and p.expect_reward != 1.0:
            errors.append(f"{p.pid}: a valid_variant probe must expect reward 1")
        for mid in (p.positive_control or ()):
            m = mutants.REGISTRY.get(mid)
            if m is None:
                errors.append(f"{p.pid}: positive_control names unknown mutant {mid}")
            elif m.role != "defence":
                errors.append(f"{p.pid}: positive_control {mid} is a {m.role} mutant, not a defence")
            elif "*" not in p.applies_to and m.task not in p.applies_to:
                errors.append(f"{p.pid}: mutant {mid} targets {m.task}, outside the probe's tasks")
        for mid in (p.control_by_check or ()):
            m = mutants.REGISTRY.get(mid)
            if m is None:
                errors.append(f"{p.pid}: control_by_check names unknown mutant {mid}")
            elif m.role != "defence":
                errors.append(f"{p.pid}: control_by_check {mid} is a {m.role} mutant, not a defence")
        for mid in (p.fault_mutants or ()):
            if mid in (mutants.GENERIC_DROP_DIRS, mutants.GENERIC_EMPTY_LITERALS):
                continue
            m = mutants.REGISTRY.get(mid)
            if m is None:
                errors.append(f"{p.pid}: fault_mutants names unknown mutant {mid}")
            elif m.role != "fault":
                errors.append(f"{p.pid}: fault_mutants {mid} is a {m.role} mutant, not a fault")
        r = subprocess.run(["bash", "-n"], input=p.agent_script, text=True, capture_output=True)
        if r.returncode:
            errors.append(f"{p.pid}: probe script does not parse: {r.stderr.strip()[:200]}")

    if spec is not None:
        for p in probes.for_task(spec.slug):
            for src in {**p.inject, **p.control_inject}.values():
                if not (spec.root / src).exists():
                    errors.append(f"{p.pid}: injected source {src} is missing from the task")
        for mid in mutants.expand(spec.root / "tests", (mutants.GENERIC_EMPTY_LITERALS,)):
            staging = Path(tempfile.mkdtemp(prefix="tier0-selftest-"))
            try:
                copy = staging / "tests"
                harborignore.copy_tests(spec.root, copy)
                mutants.get(mid).apply(copy)
            except (AssertionError, SyntaxError, OSError) as exc:
                errors.append(f"{mid}: {exc}")
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        for mid, m in mutants.for_task(spec.slug).items():
            staging = Path(tempfile.mkdtemp(prefix="tier0-selftest-"))
            try:
                copy = staging / "tests"
                harborignore.copy_tests(spec.root, copy)
                m.apply(copy)
            except AssertionError as exc:
                errors.append(str(exc))
            finally:
                shutil.rmtree(staging, ignore_errors=True)
    return errors
