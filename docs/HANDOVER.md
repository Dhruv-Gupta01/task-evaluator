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
- **Vendored upstream code** (`backend/vendor/`): Terminal-Bench's 28 `check-*.sh` scripts, its
  AI-detection script and five prompt/rubric files, copied verbatim at a pinned commit
  (`2e29ca9`, 2026-10-15). `PROVENANCE.md` in each folder records the commit.
  `backend/scripts/diff_vendored_checks.py` shows how upstream has moved on; updating is manual.

## 3. Features

### Gates (the task must pass these)

| Stage | What it does |
|---|---|
| Build | Validates the task files, then builds `environment/Dockerfile`. |
| Oracle | Runs the reference solution through Harbor, N times (default 3, every run kept). Passes only when every run scores 1. Shows time, memory and the tests passed for each run, flags a run whose test count differs from the others, and marks a mix of 1 and 0 as flaky. A "Run under CPU load" option adds stress runs outside the gate. |
| Nop | A do-nothing agent must score 0. |
| Sufficiency | An LLM judge reads the instruction, the files the agent can see and the hidden tests, and lists requirements the tests enforce that the instruction does not establish. Can run 1 to 3 times; the majority decides and disagreement is shown. |
| Agent Trials | Runs N real agent attempts (Codex, Claude Code or Terminus-2; you pick the agent, model, reasoning level and a cost cap). Shows pass rate, pass@k for every k, and each trial's tests, time, memory, tokens and cost. Failed trials with no reward and a known infrastructure error are retried once automatically. |

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
| Valid-Variant Probe | Runs other correct forms of the Oracle's answer; a rejection suggests the grader is too strict. | LLM, Docker |
| Weak-Answer Probe | Breaks one stated rule at a time in the Oracle's answer; a broken answer that still scores 1 means a rule is not enforced. | LLM, Docker |
| Cheat Trial | Runs 1 to 3 trials told to cheat. A reward of 1 triggers an automatic reward-hacking check. A trial where the agent made fewer than 3 tool calls is "inconclusive", not "the trap held". | agent key |
| Failure Analysis | `harbor analyze` over every trial: reward hacking, instruction gaps, a classification of each failure (real bug, instruction gap, infrastructure), and an audit of passing trials. | OpenAI key |
| Review Report | A draft report built from everything above, for human review. Needs every gate to be finished. | LLM |

### Around the stages

- **Changes banner**: compares an upload with the latest earlier upload of the same task name
  (amber when tests were removed). **Reviewer decision** records a person's ruling on a contested
  failure cluster; it never changes a gate (see known issue 1).
- **Task version check**: every Oracle, Nop and agent run records the task checksum Harbor
  reports. A red banner appears if runs on one submission used different task copies.
- **Verifier report**: per-test results, flags where reward and tests disagree, time and memory
  against limits, and refusal detection for model safety refusals.
- **Download Evidence**: one JSON file with the build log, Static Checks, Rubric Check, every
  stage's result, cost per stage, run limits and an auto-written summary (no LLM writes it).
  Home-directory usernames in paths are redacted. See known issue 4 about keys.
- **Budget guard**: caps priced LLM spend per UTC month and per agent run (section 5).

The README has a longer table of the same stages with the extra detail added in 4.0, 4.1 and 5.0.
### Post-fix additions (branch `harbor-5.0-post-fixes-harbor-more-checks`)

- **Tier cycle ("Run all tiers")**: Tier 0 (static probes), Tier 1 (hinted agent plus fixer) and
  Tier 2 (unhinted agent) repeat on the patched task until a round needs no patch (default 3
  rounds). Tier 1 is the only tier that edits the task; the upload is never changed and a person
  reviews the diff. Honest solves and refusals are labelled as such and never count as the grader
  holding. Costs real credits.
- **Regrade**: re-scores saved trials with the latest grader (`harbor job regrade`), without a new
  agent run. Flags 15.29 and 15.32 read its result.
- **Solution variants**: the reviewer lists correct alternative solutions (`$SOLUTION` values); each
  runs as an Oracle trial. A rejected one raises Flag 15.38, and Tier 1's patch check re-runs them.
- **`.harborignore`**: a Static Check, and the Tier 0 and Tier 1 copies honour the same rules Harbor
  does, so attackers only see what the real verifier gets.
- **Web use**: each trial's web tool calls, fetches, installs and hosts are counted from its
  trajectory and shown in the evidence. Agents run with an open network and web tools on.
- **Harbor 0.26.0** is the tested version everywhere; `GET /versions` warns about any other.
- **Tier logs and cost**: tier logs record the reasoning effort actually sent (none for Haiku 4.5),
  and Haiku 4.5 spend is priced and counted against the budget.
- **Failure Analysis per trial**: `harbor analyze` now runs once per trial folder (two at a time),
  each with its own limit (`ANALYZE_TRIAL_TIMEOUT_SEC`, default 900). One hung evaluator used to run
  the whole job to `ANALYZE_TIMEOUT_SEC` and throw away the trials that had finished (seen on
  2026-11-20: 2 of 3 done in 2.5 minutes, the third hung for an hour, no results kept).
- **Task name from `[task] name`**: after Validate & Build, a submission is named by the `[task] name`
  in `task.toml` (without `terminal-bench/`), not by the zip's file name. Versioning, the
  earlier-upload comparison, regrade and Tier 0 comparison match uploads by that name, so renaming a
  zip no longer hides the earlier upload.
- **Where the grader runs agent code**: Static Checks now see compilers and provers (coqc, lean,
  g++), commands built from named lists and constants, helper functions around `subprocess`, and a
  compiled artifact that is run later, and they recognise a Reward Kit `Worker(...)` as the safe
  pattern (info `verifier_runs_agent_code_in_worker`). Back-tested on the pre-Worker versions of 33
  upstream tasks: agent code seen in 31 (was 25), with no new warnings on the 33 tasks without a
  Worker. It is a flag for a person, not a verdict on "a Worker is needed": it still misses a grader
  that drives the agent's web app with a browser, and code run through a file copied into a scratch
  project. A Worker is never a gate here; the ruling goes through Flag 15.23.

- **Breakwater follow-up (2026-11-23)**: ideas from Singularity-Bench's Breakwater (a paper) and
  GitHub Breakwater (a tool); neither is installed or called. All advisory.
  - *Prompt guards*: every prompt that reads what an agent produced (Tier 1 and Tier 2 judges, the
    failure classification and trial audit, the Tier 1 fixer) now says the text is data and that
    instructions inside it are ignored.
  - *Broken output files* (Tier 0 probes G17 to G21, plus the empty and half-output probes): the grader
    must score a damaged file 0. A grader that starts and writes no reward is the new verdict
    `crashed_on_bad_input`. A probe that found nothing of its type to damage says so (`inconclusive`),
    never "held". A new Static Check warns when a Reward Kit grader's list of "score 0" errors misses one
    its parser raises. **Found on pg-wal**: a deeply nested JSON report makes its grader raise
    `RecursionError` and write no reward.
  - *Session-log integrity*: Claude Code and Codex write their log where the agent can edit it. The
    platform notes each log while the run is live and checks it was not changed; a mismatch flags the
    trial and warns the judges. It cannot see an edit made and finished inside one 10-second poll.
  - *Tier 0 additions*: G22 (every digit shifted), G23 (one declared output removed at a time,
    `artifact_not_checked`) and G24 (the reference output graded under other time zone, locale and
    hash-seed settings: `env_sensitive`, or `flaky` if two identical runs differ).
  - *Shared-verifier tasks*: Tier 0 no longer skips them. A Static Check lists the routes a shared
    verifier leaves open, and three probes (G25 a program left running, G26 swapped `pytest`/`python`,
    G27 a planted `conftest.py`) run through real Harbor trials.
  - *Agent output scan*: the collected output is scanned for key-shaped strings, AI-provider addresses
    and key variable names (kind and file only, never the key).
  - *Timed repeats*: a Static Check notes a timed call repeated on one input (answers can be cached), and
    the Targeted cheat prompt tries it.
  - **Checked live**: pg-wal (separate mode) for G13, G17 to G24; the smoke task (shared mode) for G25 to
    G27, where all three exploited a vulnerable grader and a hardened copy held. **Not checked live**: log
    integrity on a real Claude Code or Codex trial, the output scan and timed-repeat check on real tasks,
    G26 as a non-root agent.

- **Worker required and the Grader Isolation gate (2026-11-23)**: two steps, test-first.
  - *Way 1, advice only*: the Static Check `verifier_worker_required` (`verifier_isolation.analyze_worker_requirement`)
    reads the task's files as text and returns `not_required`, `present`, `equivalent` or `missing`. Agent code counts
    when the grader runs it (subprocess, test.sh, compilers, provers), imports it (`sys.path`, `importlib`, `runpy`,
    `PYTHONPATH`) or starts a service from an agent path (`supervisorctl`, `systemctl`, `service x start`,
    `docker compose up`; the task's own services do not count). `missing` is a warning with file:line evidence, the
    protections that are absent and the fix text. It shows in the evidence summary and as `worker_required` in the
    Review Report result. On the stored real tasks: pg-wal `present`, a web task `not_required`, the smoke task
    `missing` (its `python -m pytest` from `/app`, the old shared-mode finding).
  - *Way 2, the gate*: `services/grader_isolation.py`, `POST /grader-isolation`, `PUT /grader-isolation-decision`, the
    Grader Isolation card. States `ok`/`missing`/`unclear`; a reviewer ruling needs a reason and a name and is kept
    against a fingerprint of `tests/`, `task.toml` and the environment Dockerfile (stale when the grader changes).
    `GRADER_ISOLATION_ENFORCE` (default false) makes it block agent stages (409) and the Review Report; off, it only
    records. Neither step adds isolation to a task: the author does that, with a Reward Kit `Worker`.
  - *Not built*: the 37th rubric criterion `untrusted_code_isolation` (it would make our copy of the TB rubric differ
    from upstream), a bulk run over stored tasks, and a live run of the card in a browser. `missing` stays a warning,
    not a fail, because the detection is heuristic.

- **Gap-closing batch (2026-11-23)**: eight items from the "Platform checks tracker" sheet. All advisory.
  - *Harbor's own failure classification*: a copy of Harbor 0.26.0's table of failure types. A trial Harbor
    says has no valid reward (cancelled, a missing reward file, a verifier timeout) or whose reward is NaN or
    infinite is marked and left out of the pass rate and pass@k (a banner and an evidence line say how many).
    The one automatic retry now also covers an agent that exited non-zero while the log shows infrastructure
    noise (Harbor grades those as a real 0), and never restarts a cancelled trial. A test compares the copy with
    the installed Harbor, so refresh it when Harbor is upgraded.
  - *Four new Static Checks*: a `reward.toml` Reward Kit 0.2.4 would reject (read from Reward Kit's own source),
    a grader that decides from a success word the agent's program prints, a Lean grader that decides from text in
    Lean's output, and a test that asserts on one timing sample.
  - *Regrade explained by a model* (opt-in, costs credits): tick the box next to Re-grade and a model labels each
    changed trial expected, unexpected or unclear, from the rewards, the missing tests and what changed in the
    task. At most 12 calls; a failure never loses the regrade.
  - *Checked on real tasks*: the new checks stay quiet on pg-wal, the smoke task and the int8-style task. Running
    them found a false alarm in the earlier timed-repeats check (a loop whose input varies inside an f-string),
    now fixed.
  - *Decisions*: a missing or invalid `ctrf.json` stays a warning, not a gate; Harbor's Fortify is not
    integrated (the Tier 1 fixer and the tier cycle do that job); the platform stays on the harbor command and
    does not move to `harbor serve`.

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
  enforce a rule or leaks file descriptors to agent code; a Targeted Cheat Trial prompt built from
  the task's own restrictions (never run by default); more cases in the Weak-Answer Probe.
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
| Harbor and Docker | `HARBOR_BIN`, `HARBOR_NETWORK_MODE`, `DOCKER_PLATFORM`, `DOCKER_ADD_HOSTS` | `HARBOR_NETWORK_MODE=public` is needed because Docker Desktop for Mac cannot enforce `no-network`. |
| Codex | `CODEX_BAKE_INTO_IMAGE`, `CODEX_VERSION`, `CODEX_WEB_SEARCH` | Codex is installed into the task image at build time so trials start fast. |
| Other tools | `ANALYZE_*`, `CHECK_*`, `CHEAT_TRIAL_TIMEOUT_SEC`, `GPTZERO_API_KEY` | Failure Analysis, Rubric Check, Cheat Trial and AI Detection. |
| Cost | `AGENT_RUN_BUDGET_USD`, `LLM_BUDGET_USD`, `MIN_TRIAL_COST_USD` | See section 5. |

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

1. **Reviewer decision cannot save from the browser.** The backend allows only GET and POST
   across origins, and the UI calls PUT. Confirmed: the preflight request returns 400. The fix is
   adding `"PUT"` to `allow_methods` in `backend/app/main.py`.
2. **Single machine only.** The platform needs a local Docker daemon, keeps its job queue in
   memory and stores absolute paths in the database. It cannot run on AWS Lambda, Fargate, Cloud
   Run or Runpod pods. A cloud plan exists (`docs/cloud-deployment-plan.md`: one Docker VM, a login
   in front, a resource scheduler), but nothing of it is built, so treat it as a plan only.
3. **No login and no users.** Anyone who can reach the backend can upload tasks and start
   spending money. Keep it on localhost, or put authentication in front of it before sharing it.
4. **API keys can leak into stored logs.** Codex and Claude Code run inside the task container and
   receive the provider key in their environment. An agent that dumps its environment (for example
   a cheat trial told to look around) writes the key into the trial log, which the evidence bundle
   then includes. Path redaction does not remove it. Redacting key-like strings is not built yet.
   Check a log before you share an evidence file, and rotate any key that appears in one.
5. **Windows has never been run.** The code has Windows handling and `.gitattributes` keeps shell
   scripts on LF, but no one has tried it.
6. **Docker Desktop for Mac cannot enforce `no-network`**, so every task is run with network access
   on (`HARBOR_NETWORK_MODE=public`). A task that relies on being offline is not truly tested.
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

- **Branches**: `main` is connected to Lovable; the 5.0 work lives on `harbor-5.0`. The repo is
  linked to a Lovable project, and commits pushed to the connected branch sync into it. **Never
  force-push, and never rebase, amend or squash commits that are already pushed.** Push with
  `git -c http.postBuffer=524288000 push origin <branch>`.
- **No test suite.** Verification is done by running stages on real tasks. `docs/testing/` holds
  recorded runs on real Terminal-Bench tasks with the platform's results, for 4.0, 4.1 and 5.0
  checks. Use `examples/smoke-task` for a quick check that the setup works.
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
