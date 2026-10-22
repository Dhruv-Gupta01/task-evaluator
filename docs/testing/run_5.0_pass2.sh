#!/bin/bash
# 5.0 real-task tests, pass 2: Docker stages (build, Oracle, agent trials). Run after pass 1; one Harbor job at a time.
cd "$(dirname "$0")/../../backend"
R=${R:?set R to the terminal-bench clone}
OUT=../docs/testing/5.0
run() { # cat task issues stages ref label
  local cat=$1 task=$2 issues=$3 stages=$4 ref=$5 label=$6 args=""
  for i in $issues; do args="$args --issue $i"; done
  df -h / | tail -1
  uv run python scripts/run_tb_test.py --tasks-dir $R/tasks --repo-dir $R --ref $ref --task $task $args --build \
    --stages "$stages" --timeout 3600 --out $OUT/$cat/$task-$(echo $issues | tr ' ' '-')/$label || echo "FAILED $task $label"
}
ST=static-checks,tb-static-checks
# E: the platform and image facts, Oracle on this machine (arm64)
run E gsea-proteomics "1805 1925" $ST,oracle?n=1 83c7a6172 before-docker
run E gsea-proteomics "1805 1925" $ST,oracle?n=1 08bf83c6a after-docker
run E hof-topology-interpenetration "1803 1916" $ST,oracle?n=1 83c7a6172 before-docker
run E hof-topology-interpenetration "1803 1916" $ST,oracle?n=1 4f6282299 after-docker
# H: a non-root Oracle that fails is the signal
run H mvcc-lsm-compaction "1765 1990" $ST,oracle?n=1 fb107622e before-docker
run H mvcc-lsm-compaction "1765 1990" $ST,oracle?n=1 dfd8aa49f after-docker
# J: one Codex trial (luna) capped at $1; refusals are detected from the trial
run J interleaved-vigenere "1837" $ST,agent-trials?n=1\&max_cost_usd=1 21ae84bf run
run J atrx-vep-crispr "1836 2079" $ST,agent-trials?n=1\&max_cost_usd=1 3ef4bf2b7 before
run J atrx-vep-crispr "1836 2079" $ST,agent-trials?n=1\&max_cost_usd=1 c524af963 after
echo PASS2-DONE
