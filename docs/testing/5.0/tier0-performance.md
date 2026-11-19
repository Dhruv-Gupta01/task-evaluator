# Tier 0 (static probes): how it performed

Run on 2026-11-12, `linux/arm64` (Docker Desktop on macOS), Harbor 0.23.0. Tier 0 is a replica
of Harbor's separate-verifier contract, not Harbor itself. Where a result depends on Harbor
(G1) a real Harbor Oracle trial is used.

## Tasks

| Task | Where it ran | Probes | Held | Exploited | Fail open | Valid solution graded 0 | Rely on Harbor | Unproven |
|---|---|---|---|---|---|---|---|---|
| collab-editor-desync | CLI, 7 probes (G13, G2, G7, G8, G14-G16) + replay | 7 | 6 | 0 | 0 | 0 | 0 | 0 |
| telecom-entity-resolution | CLI, full suite (G1 inconclusive: CLI has no Harbor) | 17 | 13 | 0 | 0 | 0 | 1 (G8) | 0 (G5 hit a decoding bug, fixed, not re-run) |
| protein-autointerp-disulfide | CLI, full suite | 17 | 14 | 0 | 0 | 0 | 1 (G8) | G5 (observer control, fixed afterwards) |
| sound-change-cascade | CLI, full suite | 17 | 14 | 0 | 0 | 0 | 1 (G8) | G5 (observer control, fixed afterwards) |
| int8-runtime-port v1 | platform stage, with Harbor G1 | 20 | 15 | 0 | 2 | 1 | 0 | 0 |
| int8-runtime-port v2 (fixture-count assert) | platform stage, no Harbor G1 on the rerun | 20 | 17 | 0 | 0 | 1 | 0 | 0 |

No probe scored 1 on a real verifier (0 exploited) on any task. The control (the task's own
solution through the same pipeline) scored 1 on every task it ran on, so a 0 from an attack is
meaningful there.

## What it found

1. **int8 v1 fails open (md finding I1).** With `tests/fixtures` removed from the verifier
   image, the I-a probe (an empty `kref.py`) scored 1. Generic probe G0 found the same thing
   with no per-task authoring (`generic:drop-fixtures` reward 1; `generic:drop-generator`
   failed closed). Adding `assert len(NAMES) == 68` turned both into `held`, and the stage's
   comparison reported both as FIXED.
2. **int8 grades a valid multi-file solution 0 (md finding I2).** Probe I-c scored 0 on v1 and
   v2. The fixture-count fix does not touch it. A human decides whether the instruction allows
   a multi-file solution. A `legitimate` ruling can be recorded; it does not change the gate.
3. **FIFO artifacts (G8) rely on Harbor on three tasks.** On telecom-entity-resolution,
   protein-autointerp-disulfide and sound-change-cascade the verifier hangs until its timeout
   (about 25 min on telecom) if a FIFO at the artifact path reaches it. A hang is an invalid
   reward in Harbor, not a 0. Harbor drops FIFOs, so today the task is safe, but only because
   of Harbor. int8 and collab-editor-desync handle it themselves.
4. **Harbor's own reward handling holds (G1).** A real Harbor Oracle trial whose `solve.sh`
   only pre-wrote `reward.txt` and `reward.json` scored 0 on int8 v1.

## How the checks behaved

- Observer controls (G2, G3, G4, G6, G9): the probe's check fired when the planted file was
  made to arrive, on every task where they ran. G5's control was broken on tasks where
  `/usr/local/bin/python3` is a symlink; it now only proves arrival, and says so.
- Fault checks (G0): removing each data folder or file from `tests/` failed closed on every
  task except int8 v1 (above). 1 to 4 items per task.
- Null-agent ladder (G14 to G16): held on every task. G16 only means something when the
  reference solution produces an artifact (it did on all tasks run).
- Replay: two stored Harbor Oracle trials of collab-editor-desync, regraded through the
  replica, both reproduced Harbor's recorded reward 1.0. The earlier-upload replay
  (LOOSENED / REGRESSION) and `harness_disagrees` were not exercised, because no test had
  stored rollouts for two uploads.
- Comparison between uploads: works (`fixed` x2 on int8). A probe that is `inconclusive` one
  time (G1 without Harbor) is reported `not_comparable`. The `regression` case was not
  exercised.

## Bugs found by running it (all fixed)

- G7 and G8 did nothing on tasks whose artifact is a directory (`rm -f` cannot remove one).
- A symlink pointing at a path that does not exist on the host was counted as "not copied".
- G5's observer control used a symlink and so proved nothing.
- Non-UTF-8 output from a task image crashed a probe (telecom G5, `infra_failed`); output is
  now decoded with replacement. Fixed but not re-run on telecom.

## Added afterwards: I-d, the tamper probe (int8 v2)

kref.py, which the grader runs inside its own container, tries to append to `test_outputs.py`,
write a file in `/tests/fixtures`, delete a fixture and rewrite every expected value. On the
real grader all attempts were blocked (PermissionError, or the folder cannot be listed). With
the lock removed (`int8:tests-writable`) the same probe's attempts succeeded, so the block is
the protection and not a broken probe. The first run said `EXPLOITED` because an empty file
list counted as a successful rewrite; that was a probe bug and is fixed. Not run on v1.

## Limits

- 5 of the 6 task rows are a single run, so flakiness (`repeats`) was not exercised.
- Only int8 has task-specific probes; the other tasks got the 17 generic ones.
- G0 now also empties large constants in `tests/*.py` (27 of the 61 loadable upstream tasks have one). Run on foodstuff-beta-activity, interleaved-vigenere and hof-topology-interpenetration: every emptied constant that could be graded failed closed (reward 0); one (interleaved-vigenere `UNIGRAM_PCT`) stopped the verifier image from building, so it is `no_result`. A 0 can also come from the code breaking for an unrelated reason. Constants built inside functions are not covered.
- The harness is a replica, not Harbor. A task Harbor treats differently could differ.
- The int8 package is the 68-fixture version, not the 129-fixture one the md reviewed.
- `relies_on_harbor` and unproven results are listed for a human; none is an exploit.
- Tier 1, Tier 2 and the loop are not built.
