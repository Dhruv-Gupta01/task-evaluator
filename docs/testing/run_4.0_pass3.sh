#!/bin/bash
# Re-runs after the shared-network fix (ctr-optimization, nextjs-performance) and a repeat Oracle for
# legacy-utility-triage. Docker evidence runs (Oracle, probes, agent trial). Needs the 5.0 backend on :8003
# and a terminal-bench clone in $R.
cd "$(dirname "$0")/../../backend"
R=${R:?set R to the terminal-bench clone}
OUT=../docs/testing/4.0
run() { # category task issues(comma) stages label
  cat=$1; task=$2; issues=$3; stages=$4; label=$5
  args=(); for i in ${issues//,/ }; do args+=(--issue $i); done
  dest=$OUT/$cat/$task-${issues//,/-}/$label
  uv run python scripts/run_tb_test.py --tasks-dir $R/tasks --task $task "${args[@]}" --build --timeout 3600 --stages "$stages" --out $dest || echo "FAILED $dest"
  docker builder prune -f >/dev/null 2>&1  # keep the disk free between tasks
}
O=oracle?n=1
run B ctr-optimization 1572 $O,weak-probe probe-rerun
run E nextjs-performance 1379 $O,agent-trials?n=1\&max_cost_usd=1 run-rerun
run A legacy-utility-triage 1573,1762 $O,variant-probe probe-rerun
echo PASS3-DONE
