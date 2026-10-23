#!/bin/bash
# Re-runs every 4.1 real-task test (docs/testing/4.1/README.md). No agent trials. Needs the 5.0 backend on $BASE
# (started with DOCKER_PLATFORM=linux/amd64 and the judges on gpt-6-luna) and a terminal-bench clone in $R with
# core.autocrlf=false (Git for Windows' default CRLF breaks the task scripts). Usage: run_4.1.sh [free|docker|all]
# Everything is inside main(), so bash has read the whole script before it starts and an edit made while it
# runs cannot shift what it executes next.
main() {
  cd "$(dirname "$0")/../../backend"
  R=${R:?set R to the terminal-bench clone}
  BASE=${BASE:-http://localhost:8011}
  OUT=../docs/testing/4.1
  PHASE=${1:-all}
  export PYTHONUTF8=1
  for n in 1603 1783 1797 1824 1964 1972 1978 1995 2010 2034 2042 2060 2073 2076; do
    git -C "$R" rev-parse -q --verify pr-$n >/dev/null || git -C "$R" fetch -q origin pull/$n/head:pr-$n
  done
  MAIN=edb931320a0b
  S=static-checks
  T=tb-static-checks

  if [ $PHASE != docker ]; then
    run B ontology-kg-querying 1763,2060 before 5a3f5dadcfa7 $S
    run B ontology-kg-querying 1763,2060 after 913323a66cd9 $S
    run B takens-embedding-lean 1857,1768 current-fixed $MAIN $S  # after the lean_submission_checks fix
    run C batched-eval-parity 1632,1978 before 5a3f5dadcfa7 answer-exposure,$S
    run C batched-eval-parity 1632,1978 after 2e9cef670a0c answer-exposure,$S
    run E session-window-debug 1767,1783 before 3ef4bf2b7926 sufficiency?runs=3,edge-cases
    run E session-window-debug 1767,1783 after a3967812bedc sufficiency?runs=3,edge-cases
    run F foodstuff-beta-activity 1642,2076 before 3ef4bf2b7926 sufficiency?runs=3
    run F foodstuff-beta-activity 1642,2076 after 8c46a654ad49 sufficiency?runs=3
    run G wal-recovery-ordering 1568 current $MAIN $S
    run H intrastat-meldung 1824 before 3e9bf0ee42f7 $S,$T
    run H intrastat-meldung 1824 after 112b6ca30ec3 $S,$T
    run H interleaved-vigenere 1760,2034 before 5a3f5dadcfa7 $S
    run H interleaved-vigenere 1760,2034 after adc0ddc3a653 $S
    run I vpp-loss-divergence 2021,1995 before 7a337a834bff $T,$S
    run I vpp-loss-divergence 2021,1995 after 6b1aac064f05 $T,$S
  fi

  if [ $PHASE != free ]; then
    B=--build
    run G risk-scorer-replay 1963,1964 before e2995b93b0a4 $S,oracle?n=3,oracle?stress=true $B
    evidence $OUT/G/risk-scorer-replay-1963-1964/before
    run G risk-scorer-replay 1963,1964 after b47a72fa48e9 $S,oracle?n=3,oracle?stress=true $B
    evidence $OUT/G/risk-scorer-replay-1963-1964/after
    run H cad-model 1949,2010 before 039ba1c13bd8 $S,fresh-build $B
    run H cad-model 1949,2010 after b9c373737307 $S,fresh-build $B
    run C interleaved-vigenere 1637,2034 before 5a3f5dadcfa7 image-inventory,answer-exposure,$S $B
    run C interleaved-vigenere 1637,2034 after adc0ddc3a653 image-inventory,answer-exposure,$S $B
    run F data-anonymization 1972 before 8d0eb80ae639 image-inventory,sufficiency?runs=3 $B
    run F data-anonymization 1972 after 0c532ef20520 image-inventory,sufficiency?runs=3 $B
    run A legacy-utility-triage 1288,1390 current $MAIN oracle?n=3,nop $B
    run A intrastat-meldung 1390 current $MAIN oracle?n=3,nop $B
    run B ontology-kg-querying 1763,2060 before-oracle 5a3f5dadcfa7 oracle?n=1,nop $B
    # Rubric Check needs a built image
    run I legacy-utility-triage 1262,1520,1603 before d435a67e30ec $T,$S,rubric-check?rubric=tb $B
    run I legacy-utility-triage 1262,1520,1603 after 799c7c698e25 $T,$S,rubric-check?rubric=tb $B
    for label in before:5a3f5dadcfa7 after:dc84eafbcc8d; do
      run D legacy-utility-triage 1762,2042 ${label%%:*} ${label##*:} oracle?n=1,variant-probe,edge-cases?probe=true,sufficiency?runs=3 $B
    done
    for label in before:5a3f5dadcfa7 after:45fe1705a56a; do
      run D music-harmony 1643,1332,1797 ${label%%:*} ${label##*:} oracle?n=1,variant-probe,edge-cases?probe=true,sufficiency?runs=3 $B
    done
    for label in before:5a3f5dadcfa7 after:602a4c8e4fbe; do
      run E nextjs-performance 1537,2073 ${label%%:*} ${label##*:} oracle?n=1,weak-probe,edge-cases?probe=true,sufficiency?runs=3 $B
    done
  fi
  echo RUN-4.1-DONE
}

run() { # category task issues(comma) label commit stages [--build]
  local cat=$1 task=$2 issues=$3 label=$4 ref=$5 stages=$6 build=$7
  local args=() dest=$OUT/$1/$2-${3//,/-}/$4
  for i in ${issues//,/ }; do args+=(--issue $i); done
  uv run python ../docs/testing/tb_local.py scripts/run_tb_test.py --base $BASE --tasks-dir "$R/tasks" --task $task \
    --ref $ref --repo-dir "$R" "${args[@]}" $build --timeout 7200 --stages "$stages" --out $dest || echo "FAILED $dest"
  [ -n "$build" ] && docker builder prune -f >/dev/null 2>&1  # keep the disk free between builds
}

evidence() { # folder: saves GET /submissions/{id}/evidence next to its result.json
  local sid
  sid=$(python -c "import json,sys; print(json.load(open(sys.argv[1]))['_test']['submission_id'])" $1/result.json)
  curl -s $BASE/submissions/$sid/evidence > $1/evidence.json
}

main "$@"
exit
