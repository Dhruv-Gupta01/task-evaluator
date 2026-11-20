"""Correct alternative solutions of one task (the Harbor convention: solve.sh reads $SOLUTION).

An oracle is a correct solution by definition, so this is only about several correct ones: each, run as its
own Oracle trial with `--ae SOLUTION=<value>`, should score 1, which shows the grader accepts the real range
of correct answers and not just the author's first way of writing it. Wrong answers are not oracles; Nop and
the Weak-answer probe cover those.

A reviewer lists the values (there is no upstream manifest). Rows are Run kind "solution_variant:<value>",
run_index = the repeat, never Gate 1 runs and never part of the task-version comparison."""
from __future__ import annotations

PREFIX = "solution_variant:"
ENV = "SOLUTION"  # the variable solve.sh reads


def value_of(kind: str) -> str | None:
    return kind[len(PREFIX):] if kind.startswith(PREFIX) else None


def group_status(runs) -> str:
    """running / pending while anything is unfinished, passed when every run scored 1, else failed."""
    if any(r.status == "running" for r in runs):
        return "running"
    if any(r.status == "pending" for r in runs):
        return "pending"
    return "passed" if runs and all(r.reward == 1 for r in runs) else "failed"


def grouped(runs) -> dict[str, list]:
    """{value: its runs in order} from any run rows; other kinds are ignored."""
    out: dict[str, list] = {}
    for r in runs:
        value = value_of(r.kind)
        if value is not None:
            out.setdefault(value, []).append(r)
    return {v: sorted(rs, key=lambda r: r.run_index) for v, rs in sorted(out.items())}


def rewards_text(runs) -> str:
    return ", ".join("-" if r.reward is None else str(r.reward) for r in runs if r.status not in ("pending", "running"))
