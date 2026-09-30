# Vendored: Terminal-Bench official static checks

Source: https://github.com/harbor-framework/terminal-bench
Path: `scripts/checks/check-*.sh`
Commit: `1dcda8716784493721921c23e4bc7f7d988b4494` (2026-09-28)
License: Apache License 2.0 (see upstream repo's `LICENSE`)

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
