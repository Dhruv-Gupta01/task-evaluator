## Issue #1568: wal-recovery-ordering: flaky verifier

https://github.com/harbor-framework/terminal-bench/issues/1568 (closed)

wal-recovery-ordering run pytest as nobody via setpriv without setting a working directory. pytest 8+ chdirs back to its start path at session end, so when the verifier shell starts in /root the teardown raises PermissionError and reward is 0 even though all tests pass. docker CI is unaffected because exec starts in the image WORKDIR, which is why this only shows up on remote environments.

reported with the correct one-line fix (`cd /tmp` before the privilege drop) in #1515

issue originally reported on daytona, needs reproduction
