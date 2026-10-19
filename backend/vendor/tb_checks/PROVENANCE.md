# Vendored: Terminal-Bench official static checks

Source: https://github.com/harbor-framework/terminal-bench
Path: `scripts/checks/check-*.sh`, `scripts/checks/check-cpu-threads.py`, `scripts/checks/check_ai_detection.py`
Commit: `2e29ca91d5f1cb6fb60924bf867acc755565c044` (2026-10-15)
License: Apache License 2.0 (see upstream repo's `LICENSE`)

Refreshed on 2026-10-19 from the 2026-09-28 pin (`1dcda87`), which brought in:

- `check-compose-volumes.sh` (upstream #1520): rejects every compose volume,
  named or bind. It replaces `check-compose-host-binds.sh`, which upstream
  removed and which is removed here too.
- `check-task-changelog.sh` (upstream #2193): a new Change Log entry must add,
  not replace, a line and must carry the task version as `X.Y.Z` after the PR
  link; with `PR_NUMBER` and `REPO_URL` set it must also link that PR. Neither
  variable is set here, so the PR-link part does not run on this platform.
- `check-cpu-threads.sh` + `check-cpu-threads.py`: an advisory scan of thread
  pool settings (OMP/MKL/OpenBLAS) against `[environment].cpus`. It prints
  `WARN` lines and exits 0, so its output is shown even when it passes.
- `check-image-digests.sh`: every `FROM` and compose `image:` must be digest
  pinned. It needs the `dockerfile-pin` CLI on PATH (upstream CI installs
  v1.5.0 from github.com/azu/dockerfile-pin); without it the result is
  reported as "not run", not as a task failure.

Upstream's `test-*.sh`/`test-*.py` files test the checks themselves and
`rubric_review.py` drives their CI; neither is vendored. The canary change
discussed upstream (#2200/#2210) is not merged as of this commit, so
`check-canary.sh` is unchanged.

`check_ai_detection.py` (Layer 3 of Terminal-Bench's own three-layer AI-usage
detection -- see `docs/prompts/task-implementation.toml`'s `instruction_concision`
criterion for Layer 2, already run via the platform's Rubric Check "TB" option)
is genuinely NOT one of the `check-*.sh` scripts (29 since the refresh) and is NOT wired into any
of TB's own automatic CI workflows -- confirmed directly against their
`.github/workflows/`: it's optional, off by default, enabled only by adding a
step with a `GPTZERO_API_KEY` secret. It calls a real third-party service
(GPTZero, api.gptzero.me) to check `instruction.md` and `solution/solve.sh`
for AI-generated text (threshold 0.70 on `completely_generated_prob`), and
gracefully no-ops (prints a warning, exit 0) when `GPTZERO_API_KEY` isn't set
-- same behavior this platform's own wrapper (`app/services/ai_detection.py`)
relies on rather than reimplementing.

These are copied verbatim, unmodified, from the upstream repo at the commit
above. They're vendored (not cloned at runtime) so results are reproducible
and don't silently shift if upstream changes its check requirements — that
already happened once: an earlier manual run of these checks reported 25
checks all passing, but the actual current script set has 26 checks
(`check-no-cheat-dir.sh` wasn't in that report), and running all 26 here
found one genuine failure (`check-task-fields.sh`: the task's README is
missing an `Author:` line matching `task.toml`).

To update: re-clone the upstream repo, diff `scripts/checks/check-*.sh`
against this directory, and update the commit hash above.

This is separate from and does not replace `app/services/static_checks.py`,
which checks a different, platform-specific set of rules (instruction
formatting, Dockerfile hygiene) with almost no overlap with these.
