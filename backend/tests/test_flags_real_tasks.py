"""Batch 2 on the three real tasks: the static Flags each one must raise, no more, no fewer.

The tasks are not in this repo. Point at local copies to run it:
    TB_TASK_HARROW=.../harrow-ledger-migration TB_TASK_PGWAL=.../pg-wal-recovery \
    TB_TASK_INT8=.../int8-runtime-port uv run --with pytest --with httpx pytest tests/test_flags_real_tasks.py

Expected sets are rule ids (rule:key where one rule can fire twice). Where this differs
from the plan sheet, the reason is noted next to the entry.
"""

import os
from pathlib import Path

import pytest

from app.services import flag_rules as fr

EXPECTED = {
    "TB_TASK_HARROW": {
        "15.14",  # S5a: public canary only
        "15.15:author",  # S3: author_github = "anonymous"
        "15.15:template",  # S2: no [environment] network_mode
        "15.21",  # not on the sheet (it lists I3 for int8 only); harrow reads TEST_DIR the same way
        "15.22",  # not on the sheet (I5 is int8 only); harrow runs migrate.py with no memory bound too
        "15.35",  # reads /app/export/*.jsonl whole
        "15.25",  # H8: no [task] version
    },
    # Only the 2026-09-28 pg-wal copy is on hand. It predates the handover's version: no
    # CREATE EXTENSION (P2, 15.5) and no [task] name, so 15.5 cannot fire here.
    "TB_TASK_PGWAL": {
        "15.2",  # P7: sums() calls count, md5, string_agg without pg_catalog.
        "15.4",  # P1: tar -xzf of the agent's tarball as root
        "15.14",  # S5a
        "15.35",  # reads /app/output/report.json whole
        "15.25",  # no [task] section at all in this copy
    },
    "TB_TASK_INT8": {
        "15.7",  # I2: python3 -I -S, the instruction never says one self-contained file
        "15.14",  # S5a
        "15.15:author",  # S3
        "15.15:template",  # S2
        "15.21",  # I3: TEST_DIR from the environment
        "15.22",  # I5: kref.py runs with no memory bound
        "15.25",  # no [task] version
        # I7 (15.24): 129 cases x 20 s = 2580 s of 3600 s fits, so no flag; the note shows it.
    },
}


@pytest.mark.parametrize("env", sorted(EXPECTED))
def test_expected_flags(env):
    path = os.environ.get(env)
    if not path:
        pytest.skip(f"{env} not set")
    results = fr.evaluate(fr.load_context(Path(path)))
    got = {
        f"{rule}:{f.key}" if f.key else rule
        for rule, res in results.items()
        for f in res.findings
    }
    errors = [rule for rule, res in results.items() if res.note.startswith("rule error")]
    assert errors == []
    assert got == EXPECTED[env]
