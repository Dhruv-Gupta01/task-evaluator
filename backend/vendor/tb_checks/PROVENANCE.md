# Vendored: Terminal-Bench official static checks

Source: https://github.com/harbor-framework/terminal-bench
Path: `scripts/checks/check-*.sh`, `scripts/checks/check_ai_detection.py`
Commit: `1dcda8716784493721921c23e4bc7f7d988b4494` (2026-09-28)
License: Apache License 2.0 (see upstream repo's `LICENSE`)

`check_ai_detection.py` (Layer 3 of Terminal-Bench's own three-layer AI-usage
detection -- see `docs/prompts/task-implementation.toml`'s `instruction_concision`
criterion for Layer 2, already run via the platform's Rubric Check "TB" option)
is genuinely NOT one of the 26 `check-*.sh` scripts and is NOT wired into any
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
