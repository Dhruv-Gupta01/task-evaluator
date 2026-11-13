# Provenance

`docker_runner.py`, `probe.py`, `record.py`, `taskspec.py`, `mutants.py` and
`probes/generic.py` are adapted from the Tier 0 harness in `bta-benchmark-handover.zip`
(`adversarial/`, built 2026-10-31 against harrow-ledger-migration and pg-wal-recovery,
protocol in `CHEAT_TRIAL_PROTOCOL.md`). Not vendored verbatim: this is our code now.

Changes from the handover:

- Lives in the backend as a package; relative imports.
- `taskspec`: a compose file is allowed when `main` is still built from
  `environment/Dockerfile` (the other services are listed as `sidecars` and never started);
  the task is identified by `[task] name`, not by its folder name.
- `mutants`: `role` is `defence` or `fault`. The harrow and pgwal mutants are dropped
  (other tasks); int8 mutants added. A fault mutant that scores 1 is verdict `fail_open`.
- `probe`/`record`: `fault_mutants`, `fault_results`, verdict `fail_open` (not counted in
  the attack success rate).
- `runner.py`: the judging and mutant-check logic that was inside the handover's CLI.
- `replay.py` is the handover's loader (manifest-checked bundles) plus `recorded_reward` and
  `collect`, which finds the rollouts the platform stores; the regrade and comparison logic is
  `runner.run_replays` and `task_runner._tier0_compare`. The handover's `run_replay.py` and
  baseline file are replaced by those. The harrow/pgwal probes are not included.
- Added probes: G0, G13 (control), G14 to G16 (null-agent ladder). Added: observer controls,
  the unsanitised second pass and verdict `relies_on_harbor`, `selftest.py`, a Harbor-run G1,
  and the `legitimate` ruling endpoint (the handover had the verdict but no way to record it).
