#!/bin/bash
# 5.0 real-task tests, pass 1: Docker-free stages (static checks, TB static checks, Sufficiency, edge cases)
# on the pre-fix and post-fix versions of each task. R is the terminal-bench clone with the SHAs fetched.
cd "$(dirname "$0")/../../backend"
R=${R:?set R to the terminal-bench clone}
OUT=../docs/testing/5.0
run() { # cat task issues(space sep) stages before after
  local cat=$1 task=$2 issues=$3 stages=$4 b=$5 a=$6 args=""
  for i in $issues; do args="$args --issue $i"; done
  local dir=$OUT/$cat/$task-$(echo $issues | tr ' ' '-')
  for v in "before $b" "after $a"; do
    set -- $v
    [ "$2" = "-" ] && continue
    uv run python scripts/run_tb_test.py --tasks-dir $R/tasks --repo-dir $R --ref $2 --task $task $args \
      --stages "$stages" --timeout 1800 --out $dir/$1 || echo "FAILED $task $1"
  done
}
ST=static-checks,tb-static-checks
run H risk-scorer-replay "2152 2170" $ST 771e556f4 13428c837
run H mvcc-lsm-compaction "1765 1990" $ST fb107622e dfd8aa49f
run A2 embedding-drift-monitor "1574 1782" $ST,sufficiency?runs=3 1dcda8716 f98dc977f
run A2 cargo-flight-dispatch "1641 2045" $ST,sufficiency?runs=3 6317ba5aa 31177ffa0
run B takens-embedding-lean "1857 1861" $ST,sufficiency?runs=3 6317ba5aa 8089faed7
run B bun-sourcemap-leak "1533 1602" $ST,sufficiency?runs=3 3b5caaa48 6cda35e8c
run A1 biped-contact-dynamics "1934 1965" $ST,sufficiency?runs=3,edge-cases e2995b93b 3ac989362
run A1 freight-dispatch-shift "2248 2293" $ST,sufficiency?runs=3,edge-cases cac759380 238e470e5
run F freecad-spring-clip "1904" $ST 2e5fd44f8 17bcb5a7f
run F formal-crypto "1901" $ST 2e5fd44f8 1987c4bca
echo PASS1-DONE
