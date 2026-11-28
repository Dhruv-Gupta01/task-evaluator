# Vendored: Terminal-Bench's richer analyze/check rubrics and cheat prompt

Source: https://github.com/harbor-framework/terminal-bench
Path: `docs/prompts/`
Commit: `bdf60347ddb22be127cf3af14215c0385ea08705` (2026-11-16) for
`task-implementation.toml` (last changed upstream in `6964e92`, 2026-10-26); the other
four files are from `1dcda8716784493721921c23e4bc7f7d988b4494` (2026-09-28).
`trial-analysis.toml` and `hack-trial-prompt.md` were refreshed to `bdf6034` on
2026-11-17: upstream `2c22157` (2026-10-28) added a `[[cheat_criteria]]`
`verifier_tampering_exposure` and one sentence on executable artifacts (which Tier 1 and
the Cheat Trial now send). Harbor's rubric model reads only `[[criteria]]`, so nothing on
this branch uses the `[[cheat_criteria]]` block yet; it is kept so the file stays verbatim.
License: Apache License 2.0 (see upstream repo's `LICENSE`)

Verified byte-identical (`diff`) against the same files as provided directly
by the team, before vendoring. The 2026-11-17 refresh brought in upstream #2316:
a 37th criterion, `score_provenance` (`severity = "warning"`, like
`effective_instruction_length`). Harbor's rubric model (0.23.0, and 0.26.0 checked 2026-11-28) ignores `severity`
and returns plain pass/fail, so the platform marks a failed warning-level
criterion as a non-blocking warning itself (`vendor_info.rubric_summary`). The 2026-10-15 refresh brought in upstream
#1518: a 36th criterion, `effective_instruction_length` (a nonblocking
warning), and `instruction_concision` no longer fails on length alone.
Upstream's newer `regrade-analysis.txt` and `task-proposal.md` are not vendored
(nothing here uses them).

- `trial-analysis.toml` -- 6-criterion rubric for `harbor analyze -r`
  (`task_specification`, `reward_hacking`, `difficulty_crux`, `near_miss`,
  `refusals`, `low_timeout`), richer than Harbor's own default 2-criterion
  rubric (`reward_hacking`, `task_specification` only).
- `trial-analysis.txt` -- the matching per-trial evaluator prompt for
  `harbor analyze -p`, meant to be used together with the rubric above.
- `trial-analysis-job.txt` -- a job-level *summary* prompt (has a
  `{trial_results}` placeholder). Not a `harbor analyze` CLI flag -- Harbor
  has no built-in job-summary step -- so `task_runner.run_failure_analysis`
  does one extra LLM call with this template after `harbor analyze` finishes,
  substituting in that run's own per-trial results.
- `task-implementation.toml` -- the 36-criterion rubric for `harbor check -r`,
  richer than Harbor's own default 11-criterion rubric.
- `hack-trial-prompt.md` -- a more aggressive, explicit red-team-style cheat
  directive than this platform's own hand-written `_CHEAT_INSTRUCTION`
  (harbor_runner.py) -- an alternative prompt spliced into `instruction.md`
  for the Cheat Trial stage, selectable per-run rather than replacing the
  platform's own default.

To update: re-clone the upstream repo, diff `docs/prompts/*` against this
directory, and update the commit hash above.
