# Task Evaluator: handover

This document is for the person taking over the project. It says what the platform does, how it is
built, what every feature does, how to configure and run it, what it costs, and what is known to be
broken or missing. For installation steps, start with the [README](../README.md); this document does
not repeat them. `CLAUDE.md` in the repo root holds much deeper implementation notes, written for
an AI coding assistant, and is the place to look when you need the reason behind a design choice.

Facts here come from the code and from runs on the author's Mac (Apple Silicon, Docker Desktop).
Where something was measured, the text says so; where something was never tried, it says that too.

## 1. What it is

A local platform that grades **Terminal-Bench-style task submissions**. A task is a zip with:

```
task.toml            settings: timeouts, CPUs, memory, metadata
instruction.md       what the agent is told to do
environment/         Dockerfile and files the agent can see
solution/solve.sh    the reference solution (the "oracle")
tests/test.sh        the hidden grader that writes the reward (0 or 1)
```

A reviewer uploads the zip, then runs it through stages against real Docker containers. Five
stages are **gates** (a task must pass them); the rest are **advisory** (they raise questions for a
human and never change a gate). The platform does not decide whether a task is accepted. It
collects evidence, and a person decides.

Who uses it: a task reviewer or task author checking a submission before it goes to a client or
into the benchmark.

## 2. Architecture

```
Browser (React, port 8080)
   |  HTTP/JSON
FastAPI backend (port 8001)  --  Postgres (port 5433, Docker volume)
   |   in-process asyncio queue
   |-- docker (build, exec, run)         --> local Docker daemon
   `-- harbor CLI (oracle/nop/agents)    --> containers built from the task
LLM providers (OpenAI, Anthropic, ...)  <-- judge calls and agent trials
Files: backend/storage/submissions/{id}/ (zip, extracted task, logs, Harbor output)
```

- **Frontend** (repo root): TanStack Start, React 19, Vite, Tailwind v4, shadcn/ui. The main screens
  are `src/routes/index.tsx` (submission list and upload) and `src/routes/submissions.$id.tsx`
  (the stage dashboard, which polls while stages run). API types are in `src/lib/api.ts`.
- **Backend** (`backend/app/`): FastAPI. `routers/submissions.py` has every endpoint.
  `services/task_runner.py` holds the code for each stage. `services/harbor_runner.py` runs the
  Harbor CLI. Other `services/*.py` files are the individual checks and judges.
- **How a stage runs**: an endpoint such as `POST /submissions/{id}/oracle` marks the stage
  `pending`, then hands a coroutine to `workers/task_queue.py`, an in-memory asyncio queue. The
  frontend polls `GET /submissions/{id}` until the stage is terminal. Posting the same stage again
  cancels the running one and restarts it.
- **Data**: one `Submission` row per upload, and one `Run` row per stage result, keyed by
  `(submission, kind, run_index)`. Agent trials and cheat trials get one row per trial.
  `schemas.py` and the types in `src/lib/api.ts` are kept in sync by hand.
- **Harbor** (`uv tool install harbor==0.26.0`, the tested version) is Terminal-Bench's own test
  harness. The platform shells out to `harbor run` for Oracle, Nop and Agent Trials, and to
  `harbor analyze` and `harbor check` for Failure Analysis and Rubric Check.
- **Vendored upstream code** (`backend/vendor/`): Terminal-Bench's 29 `check-*.sh` scripts, its
  AI-detection script and five prompt/rubric files, copied verbatim at a pinned commit
  (`bdf6034`, 2026-11-16). `PROVENANCE.md` in each folder records the commit.
  `backend/scripts/diff_vendored_checks.py` shows how upstream has moved on; updating is manual.
  Once the pin is older than `VENDOR_STALE_DAYS` (30) the TB Static Checks and Rubric Check
  cards show an amber reminder. Each check may run for `TB_CHECK_TIMEOUT_SEC` (180 s); one that
  runs out of time is "not run", which leaves the stage incomplete, never passed.

## 3. Features

**Nothing can be contested, except AI Detection.** A check passes or fails, and a task that fails is revised and uploaded again. The one manual ruling is on AI Detection: a named person approves it (it passes) or disapproves it (it fails), with a reason (`PUT /submissions/{id}/ai-detection-decision`). The ruling is kept with its history and stops applying when `instruction.md` or `solution/solve.sh` changes.

### What a task author must provide

| A task must | Checked by | If it does not | Rules |
|---|---|---|---|
| Ship at least two different correct solutions, declared in `[metadata] oracle_solutions` | Static Checks, Run Oracle | the Oracle is refused or fails, and later stages stay closed | [`docs/oracle-solutions.md`](oracle-solutions.md) |
| Follow the standard `task.toml` format (separate verifier, `network_mode = "public"`) | Static Check `task_toml_format` | Static Checks fail | [`docs/task-toml-format.md`](task-toml-format.md) |
| Store its expected answers as SHA-256 digests the grader really uses | Static Check `hash_answers`, "Verify hashing" | the check fails | [`docs/hash-rules.md`](hash-rules.md) |

There are no exemptions: a task that fails is revised and uploaded again. `examples/smoke-task` meets all three.

### Gates (the task must pass these)

| Stage | What it does |
|---|---|
| Build | Validates the task files, then builds `environment/Dockerfile`. |
| Oracle | Runs every correct solution the task declares (at least two, `[metadata] oracle_solutions`) through Harbor, each at least 3 times, every run kept. Passes only when every solution has 3 passing runs and no run fails; Nop, Sufficiency, Agent Trials, the cheat trial, Tier 1, Tier 2, Run all tiers, the valid-variant, weak-answer and edge-case probes, and the hash proof refuse until then (Tier 0 and the Worker probe do not need it). A task with one solution, or two identical ones, is refused. Shows time, memory and the tests passed for each run, flags a run whose test count differs from the others, and marks a solution that mixes 1 and 0 as flaky. A "Run under CPU load" option adds stress runs outside the gate. |
| Nop | A do-nothing agent must score 0. |
| Sufficiency | An LLM judge reads the instruction, the files the agent can see and the hidden tests, and lists requirements the tests enforce that the instruction does not establish. Can run 1 to 3 times; the majority decides and disagreement is shown. |
| Agent Trials | Runs N real agent attempts (Codex, Claude Code or Terminus-2; you pick the agent, model, reasoning level and a cost cap). Shows pass rate, pass@k for every k, and each trial's tests, time, memory, tokens and cost. Trials Harbor says have no valid reward (cancelled, a verifier timeout, a NaN or infinite reward) are left out of the pass rate and pass@k. Failed trials are retried once automatically when Harbor's error policy says the failure is retryable or the log shows a known infrastructure error; a cancelled trial is never retried. |

### Advisory checks (questions for a human)

| Stage | What it checks | Needs |
|---|---|---|
| Static Checks | Mechanical checks of instruction, `test.sh`, Dockerfile and zip layout, plus hygiene, grader isolation (does the grader run agent code), reproducibility, pinning, hardware flags, agent-user and resource checks. | nothing |
| TB Static Checks | Terminal-Bench's own official `check-*.sh` scripts. | nothing |
| Leakage Scan | Unedited LLM chat artifacts in the task files. | nothing |
| Answer Exposure | Can the agent find the answer in its own files (answer-like names, `.git`, copies of solution or test files, values the tests compare against)? | nothing |
| Image Contents | Lists the files and package versions the agent's container really has. Sufficiency uses it. | Docker |
| Fresh Build | Rebuilds with no cache, then runs one Oracle with every `RUN` step redone. Catches a dependency that no longer installs. | Docker |
| AI Detection | Terminal-Bench's GPTZero check on the instruction and solution. | `GPTZERO_API_KEY` (optional) |
| Edge-Case Coverage | Lists edge cases from the instruction, checks which the tests cover; optionally breaks the Oracle's answer on each uncovered case and runs it through the real grader. | LLM, Docker |
| Rubric Check | `harbor check`: an agent scores the task against a quality rubric (the vendored 36-criterion TB rubric by default). | Anthropic key |
| Valid-Variant Probe | Runs up to 4 other correct forms of each Oracle solution's answer; a rejection suggests the grader is too strict. | LLM, Docker |
| Weak-Answer Probe | Breaks one stated rule at a time, up to 4 times per Oracle solution; a broken answer that still scores 1 means a rule is not enforced. | LLM, Docker |
| Cheat Trial | Runs 1 to 3 trials told to cheat. A reward of 1 triggers an automatic reward-hacking check. A trial where the agent made fewer than 3 tool calls is "inconclusive", not "the trap held". | agent key |
| Failure Analysis | `harbor analyze` over every trial, one trial at a time per call (two calls at once, each limited to `ANALYZE_TRIAL_TIMEOUT_SEC`, 900 s, so one hung evaluator loses only its own trial): reward hacking, instruction gaps, a classification of each failure (real bug, instruction gap, infrastructure), and an audit of passing trials. Its prompts, like the Tier judges' and the fixer's, treat agent output as data and ignore instructions planted in it. | OpenAI key |
| Review Report | A draft report built from everything above, for human review. Needs every gate to be finished. | LLM |

### Around the stages

- **Changes banner**: compares an upload with the latest earlier upload of the same task name
  (amber when tests were removed or a version warning applies). It also says what kind of change it is
  (major: instruction.md or environment/, run the agents again; minor: tests/, regrade; patch: anything
  else), whether `[task] version` was bumped enough, and which locked items changed (canary, artifacts,
  timeouts, expected values, instruction.md), as warnings with no ruling. The name is `task.toml`'s `[task] name` without the
  `terminal-bench/` prefix, not the zip's file name, so a renamed zip still finds its earlier upload.
- **Task version check**: every Oracle, Nop and agent run records the task checksum Harbor
  reports. A red banner appears if runs on one submission used different task copies.
- **Verifier report**: per-test results, flags where reward and tests disagree, time and memory
  against limits, and refusal detection for model safety refusals.
- **Download Evidence**: one JSON file with the build log, Static Checks, Rubric Check, every
  stage's result, cost per stage, run limits and an auto-written summary (no LLM writes it).
  Home-directory usernames in paths and secrets (keys this machine holds, key-shaped strings,
  `NAME_API_KEY=value`) are redacted; `secret_redactions` counts what was removed.
- **Budget guard**: caps priced LLM spend per UTC month and per agent run (section 5).

The README has a longer table of the same stages with the extra detail added in 4.0, 4.1 and 5.0.
### Additions on this branch (`harbor-5.0-reward-kit-tiers`)

Built from `harbor-5.0`: Reward Kit, the three cheat-trial tiers, Harbor 0.26.0 and the Worker work below.
**Not on this branch yet** (they live on `merge-reward-kit-into-post-fixes` and are being brought over one at a
time since 2026-11-28): the Flags framework (table, panel, rulings; not planned: it is built on named rulings),
the Flag rules that read stored results (item 14b),
session-log integrity (deferred for review),
evidence-set runs and generators. Solution variants are replaced
here by `oracle_solutions`. A section here that mentions one of them would be wrong for this branch. Already
brought over: the vendored checks at `bdf6034`, the task name from `[task] name`, Harbor's error policy, four
Static Checks (printed success words, Lean output, one-sample timing, timed repeats), the prompt guards, the
Tier 0 probes G17 to G27, the regrade classifier, secret redaction on export, the output scan, Failure
Analysis per trial, `.harborignore` handling, web-use counting, the network policy report, versioning (as warnings on the
Changes banner) and 11 of the Flag rules as Static Checks (all warn).

- **Tier cycle ("Run all tiers")**: Tier 0 (static probes), Tier 1 (hinted agent plus fixer) and
  Tier 2 (unhinted agent) repeat on the patched task until a round needs no patch (no round
  limit unless one is set; each new round's Tier 1 gets notes on the earlier rounds). Tier 1 is the only tier that edits the task; the upload is never changed and a person
  reviews the diff. Every attempt is a hit or a miss: honest solves and refusals are labelled but count
  as misses. Each tier stops after 3 misses in a row; there is no attempt cap. Costs real credits.
- **Reward Kit**: runs inside the task's own grader; the platform only reads what it leaves behind. Per-check
  results from `reward-details.json`, grader errors (a missing reward file or a Worker setup error is shown as
  a grader error, left out of the pass rate, never a plain 0), a suggested verdict (shown only; it cannot be changed), and a
  Static Check list for Reward Kit tasks: separate verifier, exact `harbor-rewardkit==0.2.4` pin, `uv.lock`,
  a `reward.toml` the 0.2.4 model would accept, setup-error handling, check names, submission errors the grader
  does not score as 0, and `harbor lint` findings.
- **Regrade**: re-scores saved trials with the latest grader (`harbor job regrade`), without a new agent run.
  It lists tests a saved run reported that the regraded run no longer does, and with "Ask a model" (off by
  default, up to 12 judge calls, counted in the budget) labels each changed reward expected, unexpected or unclear.
- **Harbor 0.26.0** is the tested version everywhere; `GET /versions` warns about any other.
- **Tier logs and cost**: tier logs record the reasoning effort actually sent (none for Haiku 4.5),
  and Haiku 4.5 spend is priced and counted against the budget.
- **Where the grader runs agent code**: Static Checks see compilers and provers (coqc, lean, g++), commands built
  from named lists and constants, helper functions around `subprocess`, a compiled artifact that is run later, and
  agent files copied into a scratch folder and run from there (found on collab-editor-desync, which used to be
  classed `not_required`). A Reward Kit `Worker(...)` is recognised as the safe pattern. It is a flag for a
  person, not a verdict; it still misses a grader that drives the agent's web app with a browser.
- **Worker required and the Grader Isolation gate**: two steps.
  - *Way 1, advice only*: the Static Check `verifier_worker_required` (`verifier_isolation.analyze_worker_requirement`)
    reads the task's files as text and returns `not_required`, `present`, `equivalent` or `missing`. Agent code counts
    when the grader runs it (subprocess, test.sh, compilers, provers), imports it (`sys.path`, `importlib`, `runpy`,
    `PYTHONPATH`) or starts a service from an agent path (`supervisorctl`, `systemctl`, `service x start`,
    `docker compose up`; the task's own services do not count). `missing` is a warning with file:line evidence, the
    protections that are absent and the fix text. It shows in the Static Checks card, the evidence summary and as
    `worker_required` in the Review Report result.
  - *Way 2, the gate*: `services/grader_isolation.py`, `POST /grader-isolation`, the
    Grader Isolation card. States `ok`/`missing`/`unclear`; nobody can overrule it, so a task that does not pass is revised.
    `GRADER_ISOLATION_ENFORCE` (default false) makes it block agent trials, the cheat trial, Tier 1, Tier 2 and the
    tier cycle (409) and the Review Report; off, it only records. Neither step adds isolation to a task: the author
    does that, with a Reward Kit `Worker`.
  - *Not built*: the 37th rubric criterion `untrusted_code_isolation` (it would make our copy of the TB rubric differ
    from upstream) and a bulk run over stored tasks. `missing` stays a warning, not a fail, because the detection is
    heuristic.
- **Worker checks added after the gate**:
  - `verifier_worker_unused` (info, Static Checks): a Worker that is imported and never called, or called with no
    agent-writable path in `Worker(...)` or its chained `.run(SCRIPT, files=[...])`.
  - `verifier_shared_mode_routes` (warn, Static Checks): a task graded inside the agent's own container leaves a
    program left running, a swapped program on PATH, a planted `conftest.py` and visible files open. Tier 0
    probes the program, PATH and conftest routes through real Harbor trials (G25 to G27).
  - `verifier_queries_agent_service` (warn, shared mode only): the grader queries `localhost:PORT` and nothing in the
    task starts that port, so only a server the agent left running can answer, outside any Worker. Tried on fixtures
    only; none of the stored real tasks queries localhost.
  - **Worker probe** (`services/worker_probe.py`, `POST`/`GET /submissions/{id}/worker-probe`, the Worker Probe card,
    a "Worker probe" line in the evidence summary and `worker_probe` in the evidence download): builds the task's own
    verifier image (from `tests/` minus what `.harborignore` keeps out, like Harbor), runs a Worker in it as root, and has it try to read `/tests`, write the reward file, keep a
    background process and read the environment. Ten checks (non-root uid and gid, no extra groups, no-new-privs, no
    capabilities, `/tests` and the reward file unreachable, reward intact, environment not inherited, leftovers
    killed). Reward Kit 0.2.4 already verifies most of this itself and fails closed; the probe shows what it looks like
    from inside. A control run as plain root fails 9 of 10. Passed on pg-wal-recovery and the Reward Kit test task.
    Advisory; skipped when the grader has no Worker. It uses the default `private_paths` only.


### Author rules added on this branch

- **Two correct solutions** (`services/solution_variants.py`): see the Oracle row above. Static Checks
  `oracle_solutions` and `identical_solutions`; identical means the same code, so renamed or reordered code is
  not caught. Tier 1 accepts a patch only if every solution still scores 1.
- **`task.toml` format** (`services/toml_format.py`): `task_toml_format` fails missing, unknown, wrong-type or
  wrong-value fields; `task_toml_extra_fields` only flags extra `[metadata]` fields.
- **Hashed answers** (`services/hash_checks.py`): `hash_answers` fails a task with no digest;
  `tests_plain_answers` warns; "Verify hashing" damages each digest and expects the score to fall. It does not
  show that every answer is hashed or that a short answer cannot be guessed. Why it works this way:
  [`hash-discussion.md`](hash-discussion.md).
- **AI Detection ruling** (`services/ai_audit.py`): the only manual ruling left; a named person approves or
  disapproves with a reason.
- **What still blocks nothing:** a failed Static Check (hashing, `task.toml`) does not stop agent runs. Only the
  Oracle, and Grader Isolation when `GRADER_ISOLATION_ENFORCE=true`, close later stages.
- **Older uploads:** Oracle runs recorded before the two-solution rule have no solution and no longer count, so
  an old submission shows its Oracle as failed until the task is revised.

### Terminal-Bench 5.0 additions (branch `harbor-5.0`)

All advisory, no new buttons; each extends an existing stage.

- **Agent user**: 5.0 moves agents to a non-root user (`[agent] user`). Static Checks warn about a
  root agent, a user the Dockerfile never creates, sudo rights, toolchains left under `/root`, and a
  user set only by a Dockerfile `USER` (some runners ignore it). Running trial containers are asked
  `id -u`, so the log shows the real uid. A non-root Oracle that fails is the strongest signal.
- **Refusals**: a model's safety refusal scores 0 like a real failure. Trials are scanned for it and
  the page shows `pass_rate_excluding_refusals` next to the unchanged pass rate (phrase matching,
  so evidence only). Failure Analysis defaults to the TB rubric, which has a `refusals` criterion.
- **Environment causes**: architecture, glibc, GitHub rate-limit and sandbox failures are flagged on
  a failing trial as "environment, not the agent". Image Contents records architecture, OS and
  glibc; run limits warn on glibc below 2.36 and an image/platform mismatch.
- **Grader hardening**: extra Static Checks for a grader that reads the submission's source to
  enforce a rule or leaks file descriptors to agent code, trusts a success word the agent's program
  prints, or decides from text in Lean's output; notes for a timing assertion on one sample and for
  a timed call repeated on one input (agent code can cache the answer); a Targeted Cheat Trial
  prompt built from the task's own restrictions (never run by default); more cases in the
  Weak-Answer Probe.
- **Sufficiency and edge-case recall**: the judge adds field, docstring, number, identifier and
  ground-truth checks; Edge-Case Coverage reads format and spec files and checks the Oracle handles
  each option.
- **Lock and licence hygiene**: toolchain and Hugging Face revision pinning, plus info notes on
  bundled data licences and task size.

Not built for 5.0: a cross-architecture Oracle, scanning archives for binaries, an
independent-tool ground-truth comparison, and author-supplied equivalent or false solutions (all
four are also in section 7, item 11).

The README has a longer table of the same stages, with the 5.0 detail in its "5.0 additions" section.

## 4. Configuration

Everything is in `backend/.env`, copied from `backend/.env.example`, which comments every setting.
Restart the backend after changing it. The groups that matter:

| Group | Settings | Notes |
|---|---|---|
| Database | `DATABASE_URL`, `CORS_ORIGINS` | Defaults match `docker-compose.yml` and the Vite dev server. |
| Judge model | `LLM_PROVIDER`, `LLM_MODEL`, `LLM_REASONING_EFFORT`, the provider key | Example file uses OpenAI `gpt-6-sol`. `gpt-6-luna` is far cheaper but missed one issue in a five-task comparison; a Gemini flash-lite model matched `sol` on Sufficiency (use a paid Google key, since free-tier prompts may be used for training). |
| Agents | `HARBOR_AGENT`, `AGENT_MODEL`, `CLAUDE_AGENT_MODEL`, `AGENT_REASONING_EFFORT`, `AGENT_TIMEOUT_SEC` | The UI picks agent, model and reasoning per run; these are the defaults. `AGENT_TIMEOUT_SEC=28800` forces Terminal-Bench's 8-hour timeout on every task. |
| Harbor and Docker | `HARBOR_BIN`, `HARBOR_NETWORK_MODE`, `DOCKER_PLATFORM`, `DOCKER_ADD_HOSTS` | Tasks run on the public network only: keep `HARBOR_NETWORK_MODE=public`. The platform also removes a task's own `[agent]`/`[verifier]` network setting from its run copy, and the evidence says what the task declared. `AGENT_DISABLE_WEB_SEARCH` (default false) switches off the agents' built-in web tools. |
| Codex | `CODEX_BAKE_INTO_IMAGE`, `CODEX_VERSION`, `CODEX_WEB_SEARCH` | Codex is installed into the task image at build time so trials start fast. |
| Other tools | `ANALYZE_*`, `CHECK_*`, `CHEAT_TRIAL_TIMEOUT_SEC`, `GPTZERO_API_KEY` | Failure Analysis, Rubric Check, Cheat Trial and AI Detection. |
| Cost | `AGENT_RUN_BUDGET_USD`, `LLM_BUDGET_USD`, `MIN_TRIAL_COST_USD` | See section 5. |
| Results review | `MIN_TRIALS_FOR_RATE` (5) | Below 5 scored agent trials the page and the evidence give counts and a 95% interval instead of a pass rate. Advisory lines only (`services/result_rules.py`). |
| Evidence sets | `SECOND_AGENT`, `SECOND_AGENT_MODEL`, `CHEAP_AGENT`, `CHEAP_AGENT_MODEL` | Agent Trials card buttons: standard = 3 trials of the agent and model picked on the Agent Trials card, then Failure Analysis, with 3 trials of the second model family (its own agent and model pickers) only when its box is ticked; cheap = 2 trials of the small model (a pass warns 15.36). Trials are appended; they cost credits. |

## 5. Costs and limits

**LLM spend** is the large cost. Measured on the author's runs: an agent trial row cost about $2.50
on average, with the most expensive about $54 (117 recorded rows, $290 in total; the per-agent
breakdown was not recorded). Claude Code and Terminus-2 trials ran about three times longer than
Codex trials, so expect them to cost more. October 2026 had $230 of recorded spend at the time of
writing.

- `AGENT_RUN_BUDGET_USD` stops one agent run (all its trials together) once its cost reaches the
  number. The Cost cap box on the Agent Trials card overrides it per run, "for the run" or "per
  trial" (the value times N, since the watchdog stops the whole job, not one trial). The code
  default is $5. The author's own `.env` sets it to 0 (off), so check yours.
- `LLM_BUDGET_USD` caps priced LLM use per UTC month (code default $50, `0` is off). The UI can
  lower a run's cap but not raise it past what is left of the month.
- Only models listed in `backend/app/services/llm/pricing.py` (currently `gpt-6-astra` and the
  Gemini flash-lite model) count toward these caps. Spend on any other model is not tracked.
- Cost is checked every 10 seconds, so a run can overshoot a cap by a few cents.

**Time** (measured, one user on one Mac): Build about 15 seconds on a warm Docker; an Oracle run
about 1.5 minutes; a full set of pre-agent checks about 30 minutes in total; an agent trial 28
minutes on average (median 15). A whole submission averaged 2.3 hours of task time, and the
heaviest about 10 hours.

**Disk**: about 80 MB per submission on average (the largest 329 MB, measured on 53), plus a Docker
image per submission. Clean up with `docker image prune -a` and by deleting old folders under
`backend/storage/submissions/`.

## 6. Day-to-day operation

- **Start**: Docker Desktop, then `docker compose up -d`, then the backend and frontend commands
  in the README. Open `http://localhost:8080`.
- **After the Mac sleeps**, Docker and Postgres often stop and the backend dies with a connection
  error. Start Docker, `docker start taskeval-postgres`, then restart the backend (kill the old
  one with `pkill -f "uvicorn app.main:app"` first; `--reload` leaves a parent process).
- **A restart fails running work**: on startup the backend marks every `pending` or `running` row
  as failed and removes labelled task containers. Do not restart during a long agent run; stages
  can be re-run afterwards.
- **Reset**: delete `backend/storage/` for the files, and `docker compose down -v` to wipe the
  database. There are no migrations: a schema change to an existing table means wiping the
  database. New columns on `runs` are added at startup by `main._add_missing_run_columns`.
- **Useful scripts** (`backend/scripts/`): `run_tb_test.py` runs stages on a real Terminal-Bench
  task through a running backend and records the evidence under `docs/testing/`;
  `export_evidence.py` rebuilds that evidence from the database; `diff_vendored_checks.py`
  compares the vendored checks with upstream. `log_issue.py` appends reviewed issues to the
  author's Google Sheet and you can ignore it.

## 7. Known issues and limits

Read these before relying on the platform.

2. **Single machine only.** The platform needs a local Docker daemon, keeps its job queue in
   memory and stores absolute paths in the database. It cannot run on AWS Lambda, Fargate, Cloud
   Run or Runpod pods. A cloud plan exists (`docs/cloud-deployment-plan.md`: one Docker VM, a login
   in front, a resource scheduler), but nothing of it is built, so treat it as a plan only.
3. **No login and no users.** Anyone who can reach the backend can upload tasks and start
   spending money. Keep it on localhost, or put authentication in front of it before sharing it.
4. **API keys can leak into stored logs.** Codex and Claude Code run inside the task container and
   receive the provider key in their environment. An agent that dumps its environment (for example
   a cheat trial told to look around) writes the key into the trial log, which the evidence bundle
   then includes. The evidence download and `export_evidence.py` now redact such keys (any value this
   machine holds, plus key-shaped strings), and the output scan flags keys the agent left in its output
   files. The raw text is still in the database and the UI, and a key split into pieces is not caught,
   so still check before you share, and rotate any key that appears.
5. **Windows has never been run.** The code has Windows handling and `.gitattributes` keeps shell
   scripts on LF, but no one has tried it.
6. **Every task runs on the public network** (`HARBOR_NETWORK_MODE=public`, by rule): a task that relies on
   being offline is not tested that way, and an agent can look things up online. What each agent did with the
   web is counted from its tool calls and shown in the evidence; the Tier judges treat finding the answer online
   as a shortcut.
7. **Two Harbor jobs at once** caused start-up timeouts on the author's Mac, so the platform runs
   one Harbor job at a time. Untested on a larger machine.
8. **Budget guard covers priced models only** (section 5).
9. **Heuristic checks produce false positives and negatives.** Answer Exposure is noisy by design,
   refusal detection is phrase matching, and the Cheat Trial "inconclusive" rule is a tool-call
   count. Each is evidence for a human, not a verdict.
10. **Lint baseline**: `bun run lint` reports 58 prettier errors in `src/routes/submissions.$id.tsx`
    that predate this handover. New code should not add to them.
11. **Not built**: a cross-architecture Oracle, scanning archives for binaries, an independent-tool
    comparison for ground truth, an author-supplied set of equivalent or false solutions, and a
    rubric-generation stage for scoring agent trajectories (planned, not started).

## 8. Working on the code

- **Branches**: `main` is connected to Lovable; the 5.0 work lives on `harbor-5.0`, and Reward Kit, the three tiers and the Worker work on `harbor-5.0-reward-kit-tiers`. The repo is
  linked to a Lovable project, and commits pushed to the connected branch sync into it. **Never
  force-push, and never rebase, amend or squash commits that are already pushed.** Push with
  `git -c http.postBuffer=524288000 push origin <branch>`.
- **Tests**: `backend/tests/` (no Docker or database needed). From `backend/`:
  `UV_PYTHON=3.13 uv run --with pytest --with httpx pytest -q` (`pyproject.toml` points pytest at `tests/`, so files
  under `storage/` that runs leave behind are not collected; Python 3.15 has no `psycopg-binary` wheel). The Worker
  probe's Docker part is checked by running it on a real task, not in the suite. `docs/testing/` holds recorded runs
  on real Terminal-Bench tasks with the platform's results, for 4.0, 4.1 and 5.0 checks. Use `examples/smoke-task`
  for a quick check that the setup works.
- **Where to add a stage**: a function in `services/task_runner.py` (copy `run_edge_case_check`),
  an endpoint in `routers/submissions.py`, a result type in `schemas.py` and `src/lib/api.ts`, a
  mapping in `services/submission_service.py`, and a card in `src/routes/submissions.$id.tsx`.
  Keep new advisory stages out of the gates, and add the evidence bundle and `CLAUDE.md` notes.
- **Generated files**: do not edit `src/routeTree.gen.ts` or `src/components/ui/`.
- **Secrets**: `backend/.env` and any service-account JSON at the repo root are gitignored. Never
  commit them. Hand over `.env.example`, not your `.env`.

## 9. Other documents

| File | Contents |
|---|---|
| `README.md` | Setup, first-run check, troubleshooting, detailed per-stage notes |
| `CLAUDE.md` | Deep implementation notes and the reasons behind design choices |
| `backend/.env.example` | Every setting, commented |
| `docs/testing/` | Recorded runs on real Terminal-Bench tasks |
| `docs/cost-comparison-fireworks-vs-runpod.md` | An older cost comparison: Fireworks per-token pricing against self-hosting `gpt-oss-120b` on RunPod (Fireworks measured about 40-100x cheaper for the usage pattern then). The project's judge model has since moved to OpenAI, so read it as background. |
| `docs/cloud-deployment-plan.md` | Plan for running this on a cloud VM (not built) |
| `backend/vendor/*/PROVENANCE.md` | Which upstream commit each vendored file came from |
