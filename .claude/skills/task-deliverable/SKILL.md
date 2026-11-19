---
name: task-deliverable
description: Build a client-facing evidence package for a Task Evaluator submission (Build/Oracle/Nop/Agent Trials/Cheat Trial/Rubric Check/etc., packaged as 01_task_package through 05_summary + README.md). Use this whenever the user asks to "create a deliverable," "build the package," "put together evidence for the client," or similar, for any submission on this platform — not just the ones already delivered. Also use it when the user asks you to audit an existing deliverable against known client-feedback lessons (see references/past-feedback-checklist.md) before sending. Trigger proactively whenever a user mentions sending a task submission's results to a client or external reviewer.
---

# Task Deliverable Builder

Packages one submission's full evidence trail (gates, advisory checks, agent
trials, cheat trial) into a reviewable zip, following the exact structure and
hard-won corrections from two real delivery cycles on this platform
(`pg-wal-recovery-deliverable-v2`, `harrow-ledger-migration-deliverable-v1`).
The single biggest risk in this task is **overclaiming** — stating something
as fact that was actually inferred, assumed, or copied from a summary without
reading the underlying evidence. Everything below exists to prevent that.

## Before touching any files: ask, don't assume

Use `AskUserQuestion` for these — guessing wrong here means redoing work:

1. **Do we have every raw data source, or just the aggregated JSON?** The
   evidence-bundle JSON and the full submission-schema JSON (from the UI) give
   you *text* logs for Build/Oracle/Nop/Rubric-Check/Static-Checks — these are
   complete, not truncated, and usually sufficient on their own. But they do
   **not** contain any agent's own reasoning transcript (`trajectory.json`,
   `claude-code.txt`/`codex.txt`) for Agent Trials or the Cheat Trial — that
   only exists in the raw per-run job folder on the machine that ran it
   (`backend/storage/submissions/<id>/runs/<stage>/harbor/<job-name>/`). If the
   deliverable needs to say anything about *why* a trial failed/passed beyond
   "reward was X," you need that raw folder. Ask the user to zip it from the
   machine that ran it (`zip -r <job-name>.zip <job-name>` inside the stage's
   `harbor/` dir) if it isn't already provided.
2. **Deliverable naming / version** — e.g. `<task-name>-deliverable-v1`.
3. **Is there a client-stated bar** (target pass rate) to compare against, or
   should results be reported as-is with no comparison?
4. **Verdict framing** — purely evidentiary (facts + caveats, reader decides —
   the default, matches both prior deliverables) or should it include an
   explicit recommendation?

## The structure (copy this exactly)

```
<task-name>-deliverable-vN/
├── 01_task_package/       <task-name>/ (the literal extracted/ folder) + CHECKSUM.sha256
├── 02_baseline_checks/    build.log, static_checks.log, static_checks_tb_official.log,
│                          rubric_review_tb.json, ai_detection.log, leakage_scan.log,
│                          oracle/oracle-run-1..N.log, nop/nop.log,
│                          cheat-trial/{cheat-trial.log, cheat-check-note.json,
│                            job/ (full raw run, if obtained — see below)}
├── 03_evaluations/        config.json + rollout-1..N/ (full raw Harbor trial output:
│                          result.json, trial.log, agent/{trajectory.json, <agent>.txt,
│                          sessions/projects/*/*.jsonl}, verifier/{test-stdout.txt,
│                          ctrf.json, reward.txt}, artifacts/, analysis.json)
├── 04_results/            results.json, results.csv
├── 05_summary/            summary.md, analyze/{failure_analysis_<rubric>.json, job_summary.txt}
└── README.md
```

Rollout numbering is **chronological start order** (read `started_at` from
each trial's own `result.json`), never Harbor's random trial-folder-name
order and never Failure Analysis's result order (which is alphabetical by
folder name — a trap, see gotcha below).

## Build order

1. **01_task_package**: extract the task zip, rename the folder to the task's
   real name (not `extracted`), write `CHECKSUM.sha256` (relative paths,
   sha256, computed *last* after everything else is final).
2. **02_baseline_checks**: pull `build`, every `oracle` run, `nop`, and
   `cheat_trial`'s text straight from the full schema JSON (`logs` field per
   run) — these are complete, just redact host paths. Render
   `static_checks`/`tb_static_checks`/`rubric_check` JSON reports to
   files. For the Cheat Trial, if you obtained the raw job folder, copy the
   trimmed version into `cheat-trial/job/` (see gotcha on trimming below).
3. **03_evaluations**: unpack the raw agent-trials job zip. For each trial,
   keep `trial.log`, `result.json`, `config.json`, `lock.json`,
   `analysis.json` (if present), `agent/trajectory.json`, `agent/<agent>.txt`,
   the real session transcript (`agent/sessions/projects/*/*.jsonl` for
   Claude Code; `agent/sessions/<date>/*.jsonl` for Codex), `verifier/*`,
   `artifacts/*`. **Drop** install-state noise: `policy-limits.json`,
   `.claude.json*`, `remote-settings.json`, `backups/`, empty
   `debug/`/`shell-snapshots/`/`memory/`/`setup/` dirs — these aren't
   evidence, just Claude Code's own install bookkeeping.
4. **04_results**: build `results.json`/`results.csv` from the raw per-trial
   `result.json` + `verifier/ctrf.json` (for `tests_passed`/`tests_total`) —
   never from a summary. Compute `pass@k` for **every** `k` from 1 to `n`
   with `1 - C(n-c, k) / C(n, k)` (`math.comb`) — a past deliverable shipped
   with only pass@2 and pass@4 and that was a flagged defect.
5. **05_summary**: write `summary.md` and `README.md` by hand (not
   templated) — see "Writing the prose" below.
6. **Validate, every time, including after small late edits**:
   - Every `*.json` parses (`python3 -c "import json; json.load(open(f))"`)
   - `CHECKSUM.sha256` self-verifies 100% (`shasum -a 256 -c`)
   - Full redaction sweep: `grep -rl "<actual-username>" <deliverable-dir>`
     returns nothing, and separately check for your *own* working-directory
     paths leaking in (`/tmp/<your-scratchpad>`, etc.) — not just the
     machine's username.
   - `dirhash` (pip-installable; Harbor's own `Task.checksum` uses it)
     reproduces every reported checksum independently — see gotcha below.
7. Rebuild the zip, copy to a place the user can grab it, re-run step 6 after
   *any* subsequent edit — a one-line wording fix is not exempt.

## Writing the prose: hard rules

- **Never characterize an agent-run stage's outcome from `reward` alone.**
  Read the actual raw output before writing a sentence about it. A reward=0
  Cheat Trial could mean: refused-and-solved-honestly, refused-and-did-
  nothing, attempted-and-got-caught, or (confirmed, live, in this platform's
  own history) ran a full exploit search and explicitly declined to attempt
  the task at all. These are different findings and read very differently to
  a client. The only source of truth is the agent's own transcript
  (`trajectory.json`), not the summary field, not your assumption from the
  reward number.
- **Pull exact failure evidence from `verifier/ctrf.json`'s own assertion
  traces**, not from a judge model's summary of them. A platform's own
  Failure Analysis / Review Report may say "the exact diffs are truncated" —
  check anyway; the raw CTRF trace usually has them, and they're often more
  precise and more useful than anything synthesized.
- **Never write "the anti-cheat design held," "trap held outright," or
  similar success language** unless you've read the transcript and it
  genuinely supports that. Prefer a specific, falsifiable claim ("`migrate.py`
  was never written") over a comforting summary phrase.
- **Disclose real inconsistencies, don't paper over them.** If two runs used
  different agent/model versions, say so plainly with the actual version
  strings — don't write "no inconsistency to flag" without checking.
- **Treat a cheap/weak judge model's criterion failures with suspicion**,
  specifically: read whether its own stated explanation actually engages with
  the criterion's real question, or just restates the reward/pass-fail
  outcome. If it's the latter, say so, cross-check against independent
  stronger signals (Sufficiency, Rubric Check, the raw diffs), and state your
  own assessment plainly — but don't attribute the problem to "it's the cheap
  model" in the client-facing text; describe the mechanism (what the judge's
  reasoning actually did or didn't do), not the cost tier.
- **No internal scaffolding ships**: no platform code references
  (`harbor_runner.py`, gate-check-ID names like "B7"/"C11"), no invented
  taxonomy not grounded in real data ("Medium difficulty band" unless that's
  a real published spec), no draft language ("I'll add this later," `[fill]`,
  TODO), no reference to "the Task Evaluator platform" in instructions
  meant for the client to reproduce results (say "run Harbor directly").
- Independently reproduce every checksum rather than quoting the platform's
  report — see the gotcha below for exactly how.

## Known gotchas (each one cost real time once)

- **Rollout ordering**: Failure Analysis's per-trial results list is sorted
  alphabetically by trial folder name, *not* chronologically. If you map
  "rollout-1, 2, 3, 4" to that order you will mislabel every trial. Always
  derive rollout number from `started_at` in each trial's own `result.json`.
- **The UI/evidence-bundle's `logs` text field is not the raw job folder.**
  For Oracle/Nop/Build/Rubric-Check/Static-Checks it genuinely is complete
  (verified: full pytest output, no truncation). For any agent-run stage
  (Agent Trials, Cheat Trial), it is Harbor's command trace + verifier output
  only — the agent's own reasoning transcript is a separate file that must be
  fetched from the host machine's `runs/<stage>/harbor/<job-name>/` folder.
  Read the *entire* `logs` string before concluding something isn't there —
  it can be tens of thousands of characters with a section (`=== VERIFIER
  OUTPUT ===`) you haven't scrolled to yet.
- **Checksum reproduction recipe** (matches Harbor's own `Task.checksum`:
  `dirhash(task_dir, "sha256")`, installable via `pip install dirhash` in a
  throwaway venv if the system Python is externally managed):
  1. `dirhash` the raw extracted folder as-is → this is the "raw package"
     hash, independent of anything the platform does.
  2. Insert `network_mode = "public"` (or whatever `HARBOR_NETWORK_MODE` is
     set to) right after the `[environment]` header line in a *copy* of
     `task.toml`, re-hash → should exactly equal the canonical
     Build/Oracle/Nop/Agent-Trials checksum (assuming the agent doesn't bake
     anything into `environment/Dockerfile` — Codex does via
     `CODEX_BAKE_INTO_IMAGE`, Claude Code does not, so Claude-Code-agent
     submissions have only this one checksum across Build/Oracle/Nop/Agent).
  3. For the Cheat Trial specifically, additionally splice the TB cheat
     prompt (`vendor/tb_prompts/hack-trial-prompt.md`, **with its leading
     `---` stripped** — this platform deviates from upstream deliberately
     here) into `instruction.md` before the closing `"\n\nYou have "` timeout
     sentence (or appended if that marker isn't found), re-hash → should
     exactly equal the Cheat Trial's own reported checksum.
  Getting an exact match at every step is the actual proof the task package
  you're shipping is what Harbor really ran — not something to skip.
- **Stray `__pycache__`/`.pyc` files inside the submitted zip are real
  content, not your mistake to clean up.** Ship the package byte-for-byte as
  submitted; flag the hygiene issue in the write-up (likely caught by Rubric
  Check's `no_extraneous_files` or similar), don't silently delete it.
- **A cheap evaluator model's `task_specification`-style criterion will often
  FAIL by just restating the reward**, not by actually comparing
  `instruction.md` against the test files the way its own guidance asks.
  Read the stated explanation text, not just the outcome, before deciding
  whether to trust it.

## Reference material

- `references/past-feedback-checklist.md` — the full, real client-feedback
  checklist from this platform's delivery history (what was wrong, what got
  fixed, what recurred across rounds). Run through this explicitly as an
  audit pass on any deliverable before calling it done — most items are
  directly checkable with a few `grep`s against the built package.
