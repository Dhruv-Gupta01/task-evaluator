"""Probe discovery."""
from __future__ import annotations

import importlib

FAMILIES = ("generic", "int8")


def _family(name: str) -> list:
    return list(getattr(importlib.import_module(f"{__name__}.{name}"), "PROBES", []))


def all_probes() -> list:
    return [p for name in FAMILIES for p in _family(name)]


def for_task(slug: str, family: str | None = None, only: set[str] | None = None) -> list:
    out = []
    for name in FAMILIES:
        if family and name != family:
            continue
        for p in _family(name):
            if p.matches(slug) and (not only or p.pid in only):
                out.append(p)
    return out
