#!/bin/bash
set -euo pipefail

cat > /app/stats/summary.py <<'PY'
def summarize(values):
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return {"count": 0, "mean": None, "median": None, "min": None, "max": None}
    mid = n // 2
    median = ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return {
        "count": n,
        "mean": sum(ordered) / n,
        "median": median,
        "min": ordered[0],
        "max": ordered[-1],
    }
PY
