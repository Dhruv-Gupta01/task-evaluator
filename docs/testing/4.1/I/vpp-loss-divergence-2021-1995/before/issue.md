## Issue #2021: add CI check for cpu threads

https://github.com/harbor-framework/terminal-bench/issues/2021 (open)

common issue that the host machine might have more threads than what is allocated to the container / task

see https://github.com/harbor-framework/terminal-bench/pull/1995

## PR #1995: fix vpp-loss-divergence (verifier timeout on Daytona)

https://github.com/harbor-framework/terminal-bench/pull/1995 (closed)

Set `OMP_NUM_THREADS=2` in the agent and verifier images, matching the task's `[environment] cpus = 2`.

Torch sizes its thread pool from the CPU count the sandbox reports. Some backends report the host's cores even though the sandbox is only allotted 2 vCPUs of CPU time, so torch spawns a thread per host core and they all contend for 2 cores' worth of time. This is the case on Daytona, where the oversubscription makes this CPU-bound training run about 48x slower and the verifier hits its 900s timeout on every trial. Pinning the pool to the configured budget restores normal runtime.

[batched-eval-parity](https://github.com/harbor-framework/terminal-bench/blob/main/tasks/batched-eval-parity/environment/Dockerfile#L5) already pins `OMP_NUM_THREADS` the same way in both of its images. Several other live tasks cap their numerical thread pools inside the oracle solution, for performance or reproducibility.

Validation: oracle runs on Daytona and Modal, with and without the `OMP_NUM_THREADS=2` pin, on Harbor 0.23.0. Times are medians in seconds.

| Backend | Threads | Trials | Reward | Agent phase (oracle) | Verifier phase |
|---|---|---|---|---|---|
| Daytona | unpinned | 1 | 0 (timeout) | 968 | >900, timed out |
| Daytona | pinned | 4 | 1.0 | 20 | 71 |
| Modal | unpinned | 6 | 1.0 | 35 | 83 |
| Modal | pinned | 6 | 1.0 | 37 | 85 |

Modal reports the allotted 2 CPUs, so torch already picks 2 threads there and the pin is a no-op. The difference between the two Modal rows is within run-to-run noise.
