## Issue #1288: add CI check for CTRF

https://github.com/harbor-framework/terminal-bench/issues/1288 (open)

gives provenance to the final reward. shows up nicely in harbor logs and harbor hub.
CTRF works well for unit tests
If your task has a threshold, you can store a reward-details.json in the verifier directory for provenance

## Issue #1390: add CI check for standardized score provenance

https://github.com/harbor-framework/terminal-bench/issues/1390 (open)

Follow-up to #1382, which suggests reporting provenance (sub-scores, raw metrics) as additional fields in `reward.json`. 

**Current state**: ~10 tasks invented ad-hoc sidecar files for exactly this provenance:

- `reward_details.json` — freecad-impeller, freecad-platform-drawing, freecad-spring-clip
- `trace_results.json` / `trace_summary.json` / `breakdown.json` — freight-dispatch-shift, heat-pump-warranty, intrastat-meldung, legacy-utility-triage, vba-userform-port
- `score_breakdown.json` — erp-procurement-planning
- `metrics.json` — distributed-dedup, lake-temp-glm

Another ~15 binary tasks compute rich internal scores (pixel-match %, RMSE, geomean speedup, precision/recall/F1) that are thresholded and then discarded (stdout/ctrf only).
