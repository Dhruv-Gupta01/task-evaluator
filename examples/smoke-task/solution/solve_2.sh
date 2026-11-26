#!/bin/bash
set -euo pipefail

# The second correct solution: the standard library does the work.
cat > /app/stats/summary.py <<'PY'
import statistics


def summarize(values):
    if not values:
        return {"count": 0, "mean": None, "median": None, "min": None, "max": None}
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }
PY
