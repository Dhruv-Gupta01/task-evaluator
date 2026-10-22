#!/bin/bash
# After the disk-full crash: re-run freecad-spring-clip's after-version Oracle on its existing submission,
# export the before/after evidence, then run pass5 (legacy-utility-triage before/after).
cd "$(dirname "$0")/../../backend"
R=${R:?set R to the terminal-bench clone}
SID=933e350d-a04c-44fa-85c7-c41b8409c986
BASE=http://localhost:8003
curl -s -X POST "$BASE/submissions/$SID/oracle?n=1" >/dev/null
sleep 20
until [ "$(curl -s $BASE/submissions/$SID | python3 -c 'import json,sys;print((json.load(sys.stdin).get("oracle") or {}).get("status"))')" != "running" ] && \
      [ "$(curl -s $BASE/submissions/$SID | python3 -c 'import json,sys;print((json.load(sys.stdin).get("oracle") or {}).get("status"))')" != "pending" ]; do sleep 15; done
echo "freecad after oracle: $(curl -s $BASE/submissions/$SID | python3 -c 'import json,sys;print((json.load(sys.stdin).get("oracle") or {}).get("status"))')"
uv run python scripts/export_evidence.py --id $SID --task freecad-spring-clip --issue 1759 --issue 1884 \
  --out ../docs/testing/4.0/B/freecad-spring-clip-1759-1884/after
../docs/testing/run_4.0_pass5.sh
echo PASS6-DONE
