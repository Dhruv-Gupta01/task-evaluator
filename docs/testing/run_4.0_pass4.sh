#!/bin/bash
# Before/after runs for every 4.0 task that only had one version: base and head of the fix PR.
# Needs the 5.0 backend on :8003 and a terminal-bench clone in $R with those commits fetched.
cd "$(dirname "$0")/../../backend"
R=${R:?set R to the terminal-bench clone}
OUT=../docs/testing/4.0
run() { # category task issues(comma) ref label stages build(0/1)
  cat=$1; task=$2; issues=$3; ref=$4; label=$5; stages=$6; build=$7
  args=(); for i in ${issues//,/ }; do args+=(--issue $i); done
  [ "$build" = 1 ] && args+=(--build)
  dest=$OUT/$cat/$task-${issues//,/-}/$label
  uv run python scripts/run_tb_test.py --tasks-dir $R/tasks --repo-dir $R --ref $ref --task $task "${args[@]}" --timeout 3600 --stages "$stages" --out $dest || echo "FAILED $dest"
  docker builder prune -f >/dev/null 2>&1  # keep the disk free between tasks
}
pair() { run "$1" "$2" "$3" "$4" before "$6" "$7"; run "$1" "$2" "$3" "$5" after "$6" "$7"; }
S=static-checks,tb-static-checks
pair D cumulative-layout-shift 2104,2105 4def1f367 6f52bb8fc $S 0
pair E coq-block-bound 1802,1907 83c7a6172 04048bb62 $S,oracle?n=1 1
pair E nextjs-performance 1379,2026 ded32f6fb ea27c4f61 $S,oracle?n=1 1
pair B ctr-optimization 1572,1585 14e2ef927 178fb05d5 $S,sufficiency?runs=3,edge-cases,oracle?n=1,weak-probe 1
pair A legacy-utility-triage 1762,2042 5a3f5dadc dc84eafbc $S,sufficiency?runs=3,oracle?n=1,variant-probe 1
pair B freecad-spring-clip 1759,1884 5a3f5dadc f6bd560f7 $S,sufficiency?runs=3,oracle?n=1 1
echo PASS4-DONE
