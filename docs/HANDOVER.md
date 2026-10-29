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
- **Harbor** (`uv tool install harbor==0.23.0`, the tested version) is Terminal-Bench's own test
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
