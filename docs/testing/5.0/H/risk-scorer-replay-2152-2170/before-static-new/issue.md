## Issue #2152: Declare explicit agent and verifier users for risk-scorer-replay and rs-archive-clone

https://github.com/harbor-framework/terminal-bench/issues/2152 (open)

The TB4 tasks `risk-scorer-replay` and `rs-archive-clone` express their non-root agent identity only through Dockerfile `USER`. Please encode the intended execution users explicitly in `task.toml`.

This is a configuration-fix follow-up to #2024 and #2025, which already describe the behavioral consequences and Modal gVisor execute-only binary limitation.

At revision `1dcda8716784493721921c23e4bc7f7d988b4494`:

| Task | Dockerfile identity | agent.user | verifier.user |
| --- | --- | --- | --- |
| risk-scorer-replay | nobody | absent | absent |
| rs-archive-clone | agent (UID 10001) | absent | absent |

Sources: [risk-scorer-replay](https://github.com/harbor-framework/terminal-bench/tree/1dcda8716784493721921c23e4bc7f7d988b4494/tasks/risk-scorer-replay), [rs-archive-clone](https://github.com/harbor-framework/terminal-bench/tree/1dcda8716784493721921c23e4bc7f7d988b4494/tasks/rs-archive-clone).

Harbor documents [agent.user and verifier.user](https://docs.harborframework.com/core-concepts/tasks/configuration) as phase-specific execution identities. [Modal ignores Dockerfile USER and starts as UID 0](https://modal.com/docs/guide/existing-images#user). A successful trial with an unspecified agent user therefore does not establish that the intended black-box restrictions were enforced.

Requested changes:

- Set `[agent].user = "nobody"` for risk-scorer-replay and `[agent].user = "agent"` for rs-archive-clone.
- Remove reliance on Dockerfile USER for the initial container identity, allowing providers to perform root-required framework setup before switching to the configured task user.
- Audit and explicitly set `[verifier].user` to each task's intended verifier identity. Both tasks use separate verifier environments; the agent's identity should not be blindly copied if the verifier image does not define it or requires different permissions.
- Validate both execution of the protected reference binary and denial of read access from the agent identity, plus successful verification with the chosen verifier identity.

The user settings alone do not resolve the gVisor execute-only limitation reported in #2024/#2025; please retain a supported runtime or address that limitation separately.

## PR #2170: fix risk-scorer-replay (agent runs as root on Modal)

https://github.com/harbor-framework/terminal-bench/pull/2170 (open)

risk-scorer-replay sets its non-root agent user only with Dockerfile `USER`. Modal ignores Dockerfile `USER` and starts as root, so the agent could read `legacy-score`, which the task keeps execute-only so it can only be used as a black box.

Changes:
- Remove `USER` from the agent Dockerfile and declare the user in task.toml, as #1992 did for fp8-rmsnorm-gemm. The image starts as root and Harbor switches to the task user for each phase.
- `[agent].user = "agent"`. #2152 suggested `nobody`, but Harbor's user switch sets HOME to nobody's `/nonexistent` and agent installs fail there (Claude Code: `mkdir: cannot create directory '/nonexistent': Permission denied`). The task now creates a dedicated `agent` user with a home that owns `/app/parityctl` and `/app/output`, replacing the `HOME=/tmp` workaround.
- `[verifier].user = "root"`, since the verifier drops submitted code to `nobody` itself.

Validation (Harbor 0.24.0): on Modal, the agent ran as root and could read `legacy-score` before the change; after it, it runs as `agent` and the read fails. Oracle 1.0 and nop 0 on Docker and Modal. Daytona was not tested.

Known limit: on Modal's default gVisor runtime a non-root user can't execute an execute-only binary, so agent trials on Modal need `--ek modal_vm_runtime=true` (#2024).

Part of #2152; rs-archive-clone is fixed in #2171.
