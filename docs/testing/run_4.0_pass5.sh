#!/bin/bash
# Re-run of legacy-utility-triage (A) before/after: pass4's Oracle runs hit Harbor's 1200 s environment
# build timeout because the build cache had been pruned and this task builds three services (one on a
# ~2 GB Playwright base). Warm the cache once, then run both versions without pruning in between.
cd "$(dirname "$0")/../../backend"
R=${R:?set R to the terminal-bench clone}
OUT=../docs/testing/4.0/A/legacy-utility-triage-1762-2042
W=$(mktemp -d)
git -C $R archive 5a3f5dadc tasks/legacy-utility-triage | tar -x -C $W
(cd $W/tasks/legacy-utility-triage/environment && docker compose -p warmup build) || echo "WARMUP FAILED"
S=static-checks,tb-static-checks,sufficiency?runs=3,oracle?n=1,variant-probe
for pair in "before 5a3f5dadc" "after dc84eafbc"; do
  set -- $pair
  uv run python scripts/run_tb_test.py --tasks-dir $R/tasks --repo-dir $R --ref $2 --task legacy-utility-triage \
    --issue 1762 --issue 2042 --build --timeout 3600 --stages "$S" --out $OUT/$1-rerun || echo "FAILED $1"
done
rm -rf $W
echo PASS5-DONE
