# Task Evaluator

A local platform for grading Terminal-Bench-style task submissions. A candidate/task-author
uploads a task zip (environment + solution + tests), and the platform runs it through five
gates against real Docker containers, plus about twenty advisory checks (see
[Stages in detail](#stages-in-detail-40-and-41)):

1. **Build** — builds the task's `environment/Dockerfile`.
2. **Oracle** — runs every correct solution the task declares (at least two), each at least 3 times, with `harbor run -a oracle`; all must score `1`. The later gates stay closed until they do.
3. **Nop** — runs `harbor run -a nop`, confirms a do-nothing agent scores reward `0`.
4. **Sufficiency** — an LLM judge checks whether the hidden test requirements are inferable
   from the visible instructions and files.
5. **Agent Trials** — runs `harbor run` with Codex, Claude Code or Terminus-2 N times, reporting
   the pass rate and each trial's token usage and cost.

Everything runs locally: FastAPI backend, React/Vite frontend, Postgres, and Docker.

## What a task must provide

| A task must | Checked by | If it does not | Rules |
|---|---|---|---|
| Ship at least two different correct solutions, declared in `[metadata] oracle_solutions` | Static Checks, Run Oracle | the Oracle is refused or fails, and later stages stay closed | [`docs/oracle-solutions.md`](docs/oracle-solutions.md) |
| Follow the standard `task.toml` format (separate verifier, `network_mode = "public"`) | Static Check `task_toml_format` | Static Checks fail | [`docs/task-toml-format.md`](docs/task-toml-format.md) |
| Store its expected answers as SHA-256 digests the grader really uses | Static Check `hash_answers`, "Verify hashing" | the check fails | [`docs/hash-rules.md`](docs/hash-rules.md) |

There are no exemptions: a task that fails is revised and uploaded again. `examples/smoke-task` meets all three.

**Taking this over?** Read [`docs/HANDOVER.md`](docs/HANDOVER.md) for what the platform is, how it
works, every feature, the configuration, costs and the known issues. This README is the setup guide.

## Prerequisites

- **Docker Desktop** (or another local Docker daemon) — running, before you start the backend.
  Give it generous memory and disk: tasks declare their own CPU and memory, and images and trial
  containers add up.
- **Python 3.13** and [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
  (`pyproject.toml` allows 3.11+, but only 3.13 has been tested)
- **[Harbor](https://github.com/laude-institute/harbor)** CLI — runs the Oracle, Nop and Agent
  Trials stages with the same harness as the real grading pipeline: `uv tool install harbor==0.26.0` (the version this was tested with)
- **Node.js** and [`bun`](https://bun.sh) (this repo is bun-lockfile-managed; `npm` will also
  work off `package-lock.json` if you don't have bun, but bun is what's actually been tested)
- API keys: an OpenAI key covers the judge stages, Failure Analysis and the Codex agent, and an
  Anthropic key covers Rubric Check and the Claude Code agent (see
  [Which keys you need](#which-keys-you-need)). Gemini, Groq and Fireworks also work as the
  judge provider.
- **Machine**: tested on an Apple Silicon Mac. On an Intel Mac or Linux, set
  `DOCKER_PLATFORM=linux/amd64` in `backend/.env`. Windows support is written but has never been
  run.

## 1. Clone and start Postgres

```sh
git clone https://github.com/Dhruv-Gupta01/task-evaluator.git
cd task-evaluator
docker compose up -d
```

This starts Postgres on `localhost:5433` with a persistent named volume, matching the default
`DATABASE_URL` below. No migrations to run — the backend creates its tables automatically on
first startup.

## 2. Backend setup

```sh
cd backend
cp .env.example .env
```

Edit `.env` and fill in **one** provider's API key matching `LLM_PROVIDER`. `.env.example` sets
`LLM_PROVIDER=openai` and `LLM_MODEL=gpt-6-sol` for the LLM-judge stages (Sufficiency, Edge-Case
Coverage, Review Report), so it needs `OPENAI_API_KEY`; the code's own fallback, used only when
`LLM_PROVIDER` is not set at all, is `fireworks`. `gpt-6-luna` is a much cheaper judge model that also
works (set `LLM_MODEL=gpt-6-luna`); it missed one issue in our five-task comparison, so use it for
quick runs. Failure Analysis and Rubric Check default to `gpt-6-luna` through `ANALYZE_MODEL` and
`CHECK_MODEL`. Everything else in `.env.example` works as-is for a local setup.

```sh
uv sync
uv run uvicorn app.main:app --reload --reload-dir app --port 8001
```

Confirm it's up: `curl http://localhost:8001/docs` should return `200`.

### Which keys you need

Put keys only in `backend/.env` (it is gitignored); never commit it. A stage whose key is missing
fails and shows the error in its logs; the stages that need no key keep working.

| Key in `backend/.env` | Needed by |
|---|---|
| `OPENAI_API_KEY` | Sufficiency, Edge-Case Coverage, both probes, Review Report (with the default `LLM_PROVIDER=openai`); Failure Analysis; the Codex agent; Cheat Trial when it uses Codex |
| `ANTHROPIC_API_KEY` | Rubric Check; the Claude Code agent; Cheat Trial when it uses Claude Code |
| `GEMINI_API_KEY`, `GROQ_API_KEY`, `FIREWORKS_API_KEY` | Only if you set `LLM_PROVIDER` to that provider |
| `GPTZERO_API_KEY` | AI Detection (optional; without it the stage reports "skipped") |

Build, Oracle, Nop, Static Checks, TB Static Checks, Leakage Scan, Answer Exposure, Image
Contents and Fresh Build need no key at all, so you can try the platform with none.

**Cost safety**: `.env.example` caps one agent run at `AGENT_RUN_BUDGET_USD=5` and the month at
`LLM_BUDGET_USD=50`. Agent trials on real tasks have cost $3 to $42 each, so check these before
raising them. Setting either to `0` turns that cap off.

## 3. Frontend setup

From the repo root, in a separate terminal:

```sh
bun install
VITE_API_BASE_URL=http://localhost:8001 bun run dev
```

(Swap `bun install` / `bun run dev` for `npm install` / `npm run dev` if you're not using bun.)

The frontend serves on `http://localhost:8080` (or whatever port Vite reports) and expects the
backend at the URL passed via `VITE_API_BASE_URL`.

## 4. Using it

Open the frontend, upload a task zip, and run it through the gates in order (Validate & Build →
Oracle → Nop → Sufficiency → Agent Trials) from the UI. Submission files and per-run logs land
under `backend/storage/submissions/{id}/` (Harbor's full job output is in
`runs/{oracle,nop,agent}/harbor/`).

### First run: check your setup in about two minutes

`examples/smoke-task/` is a tiny task (fix two bugs in a 10-line Python function) that needs no
API key. It meets the author rules above: two correct solutions, the standard `task.toml` and hashed answers. Zip it so `task.toml` sits at the top of the zip, then upload it:

```sh
cd examples/smoke-task && zip -r ../smoke-task.zip . -x '.DS_Store' && cd ../..
```

In the UI, upload `examples/smoke-task.zip`, then click **Validate & Build**, **Run Oracle** and
**Run Nop**. A working setup shows all three as **Passed**: Build takes about 15 seconds on a
warm Docker, and the Oracle (six runs: two solutions, three times each) plus Nop a minute or two
more (the first run pulls base images, so allow a few minutes). **Run Static Checks** should then
report no failures, and **Verify hashing** (in the same card) should pass. If one fails, see [Troubleshooting](#troubleshooting). To test an LLM stage, run
**Sufficiency** next (needs the judge key).

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Backend exits with `llm_tpm ... unable to parse string as an integer` | An empty `LLM_TPM=` line in an old copy of `.env`. Delete that line (or comment it out). |
| Backend crashes at startup with `connection refused` on port 5433, or every page shows a 500 | Postgres is not running. `docker compose up -d` (or `docker start taskeval-postgres`), then restart the backend. |
| `failed to connect to the docker API` | Docker Desktop is not running. Start it, wait until `docker info` works, then restart the backend. |
| Docker and Postgres stop after the Mac sleeps | Known. Start Docker, start Postgres, restart the backend (`pkill -f "uvicorn app.main:app"` first, since `--reload` leaves a parent process). Stages that were running are marked failed on restart; run them again. |
| An LLM or agent stage fails at once | Check its logs: most likely the key for that stage is not in `backend/.env` (see [Which keys you need](#which-keys-you-need)). Restart the backend after editing `.env`. |
| Frontend loads but shows network errors | It is pointing at the wrong backend. Start it with `VITE_API_BASE_URL=http://localhost:8001 bun run dev` (the default is port 8000). |
| Builds are very slow or `exec format error` | `DOCKER_PLATFORM` does not match your machine. Use `linux/arm64` on Apple Silicon and `linux/amd64` elsewhere. |
| A Dockerfile that downloads from GitHub hangs at random | See "Builds that download from GitHub" under Notes (`DOCKER_ADD_HOSTS`). |
| Disk fills up | Each submission keeps its files and an image. See "Docker image buildup" and "Resetting local state" under Notes. |

## Stages in detail (4.0 and 4.1)

**Nothing can be contested, except AI Detection.** A check passes or fails, and a task that fails is revised and uploaded again. The one manual ruling is on AI Detection: a named person approves it (it passes) or disapproves it (it fails), with a reason (`PUT /submissions/{id}/ai-detection-decision`). The ruling is kept with its history and stops applying when `instruction.md` or `solution/solve.sh` changes.

The submission page runs top to bottom in three sections: **Before the agent trials**, **Agent
trials**, **After the agent trials**. A button that depends on an earlier stage is greyed out and
says what it is waiting for. Nop, Sufficiency, Agent Trials, the cheat trial, Tier 1, Tier 2, Run all tiers, the valid-variant, weak-answer and edge-case probes, and the hash proof
stay closed until the Oracle passes (Tier 0 and the Worker probe do not need it). Only Build, Oracle, Nop, Sufficiency and Agent Trials are gates (the Grader Isolation card
below is a sixth gate only when `GRADER_ISOLATION_ENFORCE=true`; by default it is advisory);
everything else is **advisory** and never changes a gate. All of it comes from the task files
and Harbor's own results, so a finding is a question for a reviewer, not a verdict.

Items marked **(4.1)** are new in the 4.1 branch; the rest came with 4.0.

### Before the agent trials

| Stage | What it checks | Needs |
|---|---|---|
| Validate & Build | Task contract, then builds `environment/Dockerfile`. | Docker |
| Oracle | Runs every declared correct solution (at least two), each at least 3 times; the gate needs every run to score 1. Each run shows its solution, time used against its limit, peak memory, platform, agent and verifier user, and tests passed. **(4.1)** Flags runs whose test count differs from the passing runs, marks a solution that mixes 1 and 0 as flaky, and has a "Run under CPU load" button that adds stress runs kept outside the gate. | Docker |
| Nop | A do-nothing agent must score 0. | Docker |
| Image Contents **(4.1)** | After every build, lists the files and package versions the agent's container really has. Sufficiency uses it to tell files that never reach the container from visible ones. | Docker |
| Fresh Build **(4.1)** | Rebuilds with no cache and freshly pulled base images, then runs one Oracle with every `RUN` step redone. Catches a dependency that no longer installs. | Docker, slow |
| Static Checks | Mechanical checks of `instruction.md`, `test.sh`, Dockerfile and zip layout, plus hygiene: named volumes, unpinned installs, `curl \| sh`, key-like strings, x86-only downloads, dangling paths, files that differ between `environment/` and `tests/`, tools the tests need, instruction length, whether the solution reads `tests/`. **(4.1)** Also grader isolation (the verifier importing or running agent code, `eval`/pickle in the grader, results in `/tmp`, reward-file reset), reproducibility (wall-clock assertions, unpinned images and dependencies, run-time downloads) and scoring-file checks. | none |
| TB Static Checks | Terminal-Bench's own `check-*.sh` scripts, vendored verbatim. **(4.1)** Refreshed to upstream `2e29ca9` (28 scripts). A check that cannot run is not a pass: run `uv run python scripts/install_dockerfile_pin.py` (from `backend/`) once per machine for `check-image-digests`. | none |
| Leakage Scan | Unedited LLM chat artifacts. | none |
| Answer Exposure **(4.1)** | Can the agent find the answer in its own files: answer-like names, a `.git` folder, copies of `solution/` or `tests/` files, values the tests compare against. | none |
| AI Detection | Terminal-Bench's three AI-usage layers side by side: written policy, Rubric Check opinion, and a GPTZero measurement. | GPTZero key |
| Sufficiency | LLM judge: is `instruction.md` plus `environment/` enough to pass the hidden tests? Runs 1 to 3 times (majority decides, disagreement shown); tagged checks for hidden rules, stale numbers, coverage, loose tests and more. **(4.1)** Knows which files are in the image. | LLM |
| Edge-Case Coverage | Lists edge cases from the instruction alone, then checks which the tests cover. **(4.1)** "Check + run through the grader" breaks the Oracle on each uncovered case and confirms with the real verifier. | LLM |
| Rubric Check | `harbor check`: an agent scores the task against a rubric (Harbor's own, or the vendored 35-criterion TB rubric, now 36 criteria **(4.1)**; the TB one is pre-selected). | `ANTHROPIC_API_KEY` |
| Valid-Variant Probe | Runs other correct forms of the Oracle's answer through the verifier; a rejection means the grader may be too strict. | LLM, Docker |
| Weak-Answer Probe | Breaks one stated rule at a time in the Oracle's answer; a broken answer that still scores 1 means a rule is not enforced. | LLM, Docker |
| Cheat Trial | One trial told to cheat. Reward 1 triggers an automatic reward-hacking check. **(4.1)** Runs 1 to 3 trials with a combined summary. An agent that never really tried is "inconclusive". | Agent key |

### Agent trials

Run N independent trials with the chosen agent (Codex, Claude Code or Terminus-2), reasoning
level and a cost cap (for the whole run or per trial, with a warning under $3 per trial). Shows
pass rate and pass@k, each trial's tests passed, raw reward, time, memory, platform, user,
tokens and cost, and flags where the reward and the tests disagree.

### After the agent trials

| Stage | What it checks |
|---|---|
| Failure Analysis | `harbor analyze` over every trial: reward hacking, task specification, which tests the failing trials failed, a classification of each failure (real bug / instruction gap / infrastructure), and an audit of passing trials. **(4.1)** Reward-file and harness-error flags are added per trial. |
| Review Report | A draft report built from everything above, for human review. |

### Other things on the page

- **Changes banner**: compares an upload with the latest earlier upload of the same task name
  (amber when tests were removed).
- **Download Evidence**: one JSON with the build log, Static Checks, Rubric Check, per-stage
  results, cost per stage and a summary. **(4.1)** Adds run limits, Oracle notes, Cheat Trial,
  Answer Exposure, Fresh Build, Image Contents and the verifier reports.

### 5.0 additions

All advisory, like the rest: a finding is a question for a reviewer and never changes a gate. None
of these adds a button; each shows up inside an existing stage.

| Area (TB 5.0 category) | What it does | Where it shows |
|---|---|---|
| Agent user (H) | Terminal-Bench 5.0 moves agents to a non-root user (`[agent] user` in `task.toml`). A root agent is a warning, not a failure. With a user set, it warns when the Dockerfile never creates it or never `chown`s files to it, when it has `NOPASSWD`/sudo, when toolchains stay under `/root` (including Maven's `/root/.m2`), and when `solve.sh` runs `apt-get install` at run time. It warns when the non-root user comes only from a Dockerfile `USER` (some runners, such as Modal, ignore that and start the agent as root). Each running trial container is also asked `id -u`, so the trial log and facts line show the real uid (amber when 0). | Static Checks, trial logs |
| Refusals (J) | A model's safety refusal scores 0 like a real failure and lowers the pass rate. Non-passing trials are read for provider content-policy errors, or refusal phrases from an agent that made fewer than 3 tool calls. The page shows an amber refusal line plus `pass_rate_excluding_refusals`; `pass_rate` and pass@k are unchanged. Phrase matching can be wrong, so it is evidence only. Failure Analysis now defaults to the TB rubric, which includes a `refusals` criterion. | Agent Trials, Failure Analysis, evidence |
| Environment causes (E) | Failures that come from the machine or image, not the agent: architecture mismatches (`exec format error`, `no matching manifest`, aarch64 package misses, `GLIBC_x not found`), GitHub rate limits, "port not ready", and the verifier sandbox failure "Worker could not install required protections". A non-passing trial gets an "environment, not the agent" flag. Image Contents also records the image's architecture, OS and glibc, and base images are inspected for missing platforms or a single-platform digest. Run limits warn on glibc below 2.36, image architecture against `DOCKER_PLATFORM`, and `AGENT_TIMEOUT_SEC` overriding the task's own timeout. New Static Checks: `bundled_binaries_single_arch`, `gpu_not_available`, `agent_timeout_below_estimate`. | Trials, Image Contents, Static Checks, evidence |
| Grader hardening (B, A1) | Static Checks also flag a grader that enforces a restriction by reading the submission's source (`ast` walks, banned-import lists, `grep import`), one that leaks file descriptors to agent code (`close_fds=False`, `pass_fds`), and `compile()`/`__import__()` of a non-literal. The Weak-Answer Probe also tries corrupting k of n outputs, blanking a field, embedding a forbidden library, and X/Z/NaN values. The Cheat Trial has a **Targeted** prompt built from the task's own restriction sentences (and axiom/macro holes for Lean tasks); it is never run by default. | Static Checks, Weak-Answer Probe, Cheat Trial |
| Sufficiency recall (A2) | The judge also tabulates every output field a test checks (`[fields]`), treats comments and docstrings in agent-visible files as part of the instruction (`[docstring]`), lists every tolerance, threshold and count the tests enforce (`[numbers]`), checks external identifiers and versions (`[identifier]`), and reads lines where a test's author admits something is "not stated" (`[admitted]`). Prompts are longer, so each call costs a little more. | Sufficiency |
| Grader too strict, Oracle proves nothing (A1) | Edge-Case Coverage also reads up to 3 agent-visible format or spec files and turns every option they allow into a case, and reports whether the Oracle itself handles each. Sufficiency gets `[ground-truth]`: are the expected values independent, or the reference's own output? Static Checks warn when a `solution/` file equals or embeds a `tests/` data file, or imports a module that exists only under `tests/`. | Edge-Case Coverage, Sufficiency, Static Checks |
| Lock and licence hygiene (F) | `toolchain_pinning` (opam without a lock, elan/lake without `lean-toolchain` and a manifest, sbt, cpanm, unversioned conda packages, unpinned rustup/uv/nvm/pnpm bootstraps), `huggingface_revision` (`from_pretrained` and friends without `revision=`), and info-only notes for bundled data licences and task size. The TB Static Checks card shows which upstream commit the vendored scripts come from. | Static Checks, TB Static Checks |
| Answer exposure extras | Symlinks in `environment/` (one pointing at an answer is a warning), future or very old file timestamps, and version banners (a commit hash or a GitHub tree/blob/commit URL). | Answer Exposure |

**Not built for 5.0:** a cross-architecture Oracle run, scanning archives for binaries, an
independent-tool comparison for ground truth (rustc, a parser), and an author-supplied folder of
equivalent or false solutions. The Lean 4.28 "unsound toolchain" note rests on a milestone item title
and is unverified.

### Worker required and the Grader Isolation gate

Does the task's grader run agent-written code without a Reward Kit `Worker` (or equivalent isolation)?
The platform only reports this; it adds no isolation, the task author does.

| Area | What it does | Where it shows |
|---|---|---|
| Worker required (Static Check `verifier_worker_required`) | Reads the task's files as text and gives one result: `not_required` (nothing agent-written runs in grading, or it runs in a VM), `present` (a `Worker` runs it), `equivalent` (a separate user, no-new-privs, `/tests` locked and children killed, all found in the task's files) or `missing` (agent code runs with none of that). Agent code counts when the grader runs it, imports it, or starts a service from an agent path (`supervisorctl`, `systemctl`, `docker compose up`). `missing` is a warning with file:line evidence, what is absent and the fix (use `Worker(...).run(...)`, pin `harbor-rewardkit==0.2.4`, add `tests/reward.toml`, bump the minor version). | Static Checks, evidence summary, Review Report (`worker_required`) |
| Grader Isolation gate | Turns that result into `ok`, `missing` or `unclear` (a Worker with agent code also running outside it, or only the shared-mode `python -m pytest` note). Nobody can overrule it: a task that does not pass is revised. Advisory unless `GRADER_ISOLATION_ENFORCE=true`, which makes agent trials, the cheat trial, Tier 1, Tier 2 and the tier cycle return 409 and the Review Report wait until it passes. | Grader Isolation card |

It is a text-pattern guess, so a finding is a question for a person. Existing accepted tasks are not
re-checked automatically; run the gate on each.

More Worker checks:

| Area | What it does | Where it shows |
|---|---|---|
| Scratch copies | A grader that copies the agent's files into a temp folder and runs them from there (`shutil.copytree(APP, root / "app")`, then `python3 app/server/server.py`) now counts as running agent code. It used to be classed `not_required` (found on collab-editor-desync). | Static Checks, Grader Isolation |
| Unused Worker (`verifier_worker_unused`, info) | A Worker that is imported and never called, or called where no agent-writable path appears in `Worker(...)` or its chained `.run(SCRIPT, files=[...])`. A Worker around the grader's own commands protects nothing and can fail a correct solution. | Static Checks |
| Shared verifier (`verifier_shared_mode_routes`, warn) | A task graded in the agent's own container leaves open a program left running, a swapped program on PATH, a planted `conftest.py` and visible files; Tier 0 cannot probe it. | Static Checks |
| Agent service graded (`verifier_queries_agent_service`, warn) | In a shared verifier, the grader queries `localhost:PORT` and nothing in the task starts that port, so only a server the agent left running can answer, outside any Worker. Tried on fixtures only. | Static Checks |
| Worker probe | Builds the task's own verifier image, runs a Worker in it as root and has it try to read `/tests`, write the reward file, keep a background process and read the environment. Ten checks: non-root uid and gid, no extra groups, no-new-privs, no capabilities, `/tests` and the reward file unreachable, reward intact, environment not inherited, leftovers killed. Reward Kit 0.2.4 already checks most of this itself; the probe shows what it looks like from inside. Needs Docker, no LLM; skipped when the grader has no Worker. Uses the default `private_paths`. | Worker Probe card, evidence summary and download (`worker_probe`) |

All of this is decided from the grader's own files (and, for the probe, the verifier image). Nothing needs an
agent trial first: run Static Checks and Grader Isolation after Validate, then the Worker probe, then the Oracle
(3 runs) and Nop to show correct work still scores 1 and nothing scores for free.

### Cheat-trial tiers (Tier 0, 1, 2)

Do the task's tests hold when an agent tries to fake a pass? All advisory: a result is a question for a reviewer
and never changes a gate. Tiers 1 and 2 spend real credits; set a cap. Details: [`docs/cheat-trial-tiers.md`](docs/cheat-trial-tiers.md).

| Stage | What it does | Where it shows |
|---|---|---|
| Tier 0: Static Probes | Scripted fake agents (planted `conftest.py`, shadowed programs, `LD_PRELOAD`, a symlink or FIFO in place of the output, pre-written reward files, empty or placeholder output) run against a replica of the task's verifier. A probe that scores 1 is an exploit. The task's own solution must score 1 through the same replica (the control). No LLM; needs Docker. | Tier 0: Static Probes card |
| Tier 1: Hinted Agent | A Claude Code attacker with the red-team prompt and a read-only copy of `tests/` tries to get reward 1. A judge labels each attempt (exploited, tried a shortcut and failed, honest solve, refused ...). After a hit a fixer proposes edits to `tests/` and `environment/`, kept only if the Oracle still scores 1 and a replay of the winning output no longer wins. The upload is never edited; a person reviews the diff. | Tier 1: Hinted Agent card |
| Tier 2: Unhinted Agent | The plain instruction plus one pressure line, no test copy and no memory between runs: what happens in ordinary use. Never patches anything. | Tier 2: Unhinted Agent card |
| Run all tiers | Tier 0, 1 and 2 as one job. If Tier 1 patched the task, all three run again on the patched copy (up to 3 rounds). Honest solves and refusals are never counted as the grader holding. | Tier cards, "Run all tiers" button |

Each tier ends `robust` after 3 consecutive real shortcut attempts that failed. `max_attempts` caps attempts of
any kind, so a run of honest solves ends `inconclusive`, not `robust`. A cheap model such as Haiku mostly refuses
or solves honestly: that shows the plumbing works, not that the task is robust.

### Reward Kit

Reward Kit runs inside the task's own grader. The platform only reads what it leaves behind. All advisory.

| Area | What it does | Where it shows |
|---|---|---|
| Per-check results | Reads `reward-details.json` into named checks (failed first). | Oracle, Nop and Agent Trials cards |
| Grader errors | A trial with no reward because the verifier itself broke (a missing reward file, a Worker setup error) is a grader error: left out of the pass rate, never shown as a plain 0. | Oracle, Nop and Agent Trials cards |
| Suggested verdict | Pre-fills fake, likely genuine, pass or unclear per trial. | Trial cards |
| Reward Kit checklist | Separate verifier with a `tests/Dockerfile`, exact `harbor-rewardkit==0.2.4` pin, `uv.lock`, a `reward.toml` Reward Kit 0.2.4 would accept (only `[[reward]]` and `[scoring.<file>]` with `aggregation`/`threshold`), setup-error handling, check names, parser errors the grader does not score as 0, and `harbor lint` findings. Nothing here for a task that does not use Reward Kit. | Static Checks |
| Re-grade | `harbor job regrade` re-scores saved trials with the latest grader, without a new agent run; the originals are kept. | Re-grade Saved Trials card |

### Hashed expected answers

Rules for authors: [`docs/hash-rules.md`](docs/hash-rules.md). Why it works this way: [`docs/hash-discussion.md`](docs/hash-discussion.md).

| Check | What it does | Where it shows |
|---|---|---|
| `hash_answers` | **Fails** a task with no `_sha256` / `_not_sha256` entry under `tests/` | Static Checks |
| `tests_plain_answers` | Warns about values that look like plain answers (a person decides; "none seen" is not proof) | Static Checks |
| Hash proof ("Verify hashing") | Damages one digest at a time and runs the Oracle; the score must fall. Needs a passing Oracle; Docker, no model | Static Checks card, evidence |

Nothing blocks agent runs on these yet.

### At least two correct solutions

Rules for authors: [`docs/oracle-solutions.md`](docs/oracle-solutions.md).

| Check | What it does | Where it shows |
|---|---|---|
| `oracle_solutions` | **Fails** a task that does not declare two correct solutions in `[metadata] oracle_solutions` or whose `solution/` never reads `$SOLUTION` | Static Checks |
| `identical_solutions` | **Fails** two solutions that are the same code | Static Checks |
| Run Oracle | Runs every declared solution (at least 3 times each). The gate opens only when all pass; Nop, Sufficiency, Agent Trials and the later stages stay closed until then | Run Oracle card, evidence |

A task with one solution is refused. Tier 1 accepts a patch only if every declared solution still scores 1.

### task.toml format

Rules for authors: [`docs/task-toml-format.md`](docs/task-toml-format.md). Static Check `task_toml_format` **fails** a missing, unknown or wrong-type field, a `network_mode` other than `"public"`, a verifier that is not `separate`, and `allow_internet`; `task_toml_extra_fields` only flags extra `[metadata]` fields and a top-level `version`.

## Notes

- **Docker platform**: every build and container run (Build, and Oracle/Nop/Agent Trials via
  Harbor) uses one architecture, set by `DOCKER_PLATFORM` in `backend/.env` (default
  `linux/arm64`, native on Apple Silicon). `linux/amd64` also works on a Mac but runs under
  QEMU emulation and is much slower.
- **LLM spending cap**: agent trials and OpenAI judge calls stop once this calendar month's
  recorded spend reaches `LLM_BUDGET_USD` (default $50; `0` disables it). `GET /budget` shows
  spend so far. Only models with a known price (currently `gpt-6-astra`) are tracked.
- **Builds that download from GitHub**: on some networks one of GitHub's release/raw CDN addresses
  (`185.199.109.133`) accepts the connection and then hangs, so a Dockerfile that `curl`s GitHub
  fails at random. Set `DOCKER_ADD_HOSTS=raw.githubusercontent.com:185.199.108.133,release-assets.githubusercontent.com:185.199.108.133`
  in `backend/.env` to pin a working address for every build (the platform's and Harbor's). Leave it
  empty on a normal network.
- **Adding trials without replacing the old ones**: `POST /submissions/{id}/agent-trials?n=3&append=true`
  (API only; the UI button replaces).
- **Oracle runs every declared solution, and keeps every run**: the Run Oracle card has a "Runs per
  solution" field (default and minimum 3). Every click appends runs and shows each one's solution, status,
  reward and logs. "passed" means every declared solution scored 1 at least 3 times and no run so far
  failed.
- **Task version consistency check**: every Oracle, Nop and Agent Trials run records the
  `task_checksum` Harbor's own `result.json` reports for the exact task directory it ran against.
  The first one seen on a submission becomes its canonical checksum; a red banner appears on the
  submission page if any later run's checksum differs (a submission stitched together from runs
  against different task zips/extractions, rather than one frozen version, e.g. an oracle run
  before a re-upload and rollouts after). `GET /submissions/{id}` exposes it as `checksum:
  {canonical, consistent, mismatched}` and each run's own `task_checksum`. The Cheat Trial is
  excluded from this check — its task copy always has an intentionally edited `instruction.md`
  (see below), so its checksum never matches by design.
- **Agent timeout and run cost cap**: `AGENT_TIMEOUT_SEC` forces one agent timeout on every task
  (Terminal-Bench 4.0 uses 8 hours, `28800`); empty keeps each task's own. `AGENT_RUN_BUDGET_USD`
  (default $5) stops an agent-trials run once its own cost reaches it, and a run is also stopped
  when it would exceed what is left of this month's `LLM_BUDGET_USD`. The cost is checked every 10
  seconds, so a run can overshoot the cap by a few cents.
- **Codex agent**: set `HARBOR_AGENT=codex` and `AGENT_MODEL=openai/gpt-6-astra` in `backend/.env`
  to run trials with OpenAI's Codex instead of Terminus-2. Codex is baked into each task's image at build time (`CODEX_BAKE_INTO_IMAGE`, default on;
  the first build of a task takes about a minute longer, later trials start in seconds), needs the network (`HARBOR_NETWORK_MODE=public`,
  the default), has the OpenAI key inside the container, and has no turn cap: only the task's
  agent timeout bounds its cost. Pin its version with `CODEX_VERSION`. Leave it empty and the
  platform now also cache-busts the install layer once a day, so "latest" actually stays latest
  instead of silently freezing at whatever was newest the first time a task's image was built --
  a real risk of Docker's own build caching, separate from (and in addition to) the other real
  incident this project hit: `CODEX_VERSION` getting explicitly pinned by a stray shell
  environment variable, invisible in `.env`, that stayed set for days without anyone noticing.
- **Agent per run**: the Agent Trials card has an Agent dropdown: "Codex · GPT-6 Astra" (model
  `AGENT_MODEL`, needs `OPENAI_API_KEY`) or "Claude Code · Fable 5.1" (model `CLAUDE_AGENT_MODEL`,
  default `anthropic/claude-fable-5-1`, needs `ANTHROPIC_API_KEY`). API: `agent-trials?agent=claude-code`.
  Both install themselves inside the task container, so their key enters it. Claude Code's cost can't
  be read while it runs, so each trial is also capped by Claude Code itself at its share of the run budget.
  A Model dropdown next to it overrides that agent's `.env` model for the run (Codex: `gpt-6-luna`, `gpt-6-sol`,
  `gpt-6-astra`; Claude Code: Fable 5.1, Opus 5.5, Sonnet 5.5, Haiku 5.5; "Default" keeps the `.env` model).
  API: `agent-trials?agent=codex&model=openai/gpt-6-sol`. The provider prefix is checked (Codex `openai/*`,
  Claude Code `anthropic/*`).
- **Reasoning level per run**: the Agent Trials card has a Reasoning dropdown (Default, low, medium,
  high, xhigh, max). Default uses `AGENT_REASONING_EFFORT` from `backend/.env`; anything else applies
  to that run only (API: `agent-trials?reasoning_effort=high`). The trial logs start with the agent,
  model and options used, so you can see which level a trial ran on.
- **Every trial's logs start with a full audit header**: the Harbor CLI version, the exact `harbor run`
  command (every `--agent-kwarg` it actually passed, including reasoning effort), confirmation the
  agent execution timeout was written into task.toml as-is with no multiplier applied (the separate
  install-time `--agent-setup-timeout-multiplier`, if any, is logged too), and the agent's own resolved
  version — read per trial from Harbor's own `agent/trajectory.json` (every installed agent writes this
  itself), not from a requested/pinned setting like `CODEX_VERSION` (which resolves to npm's "latest"
  when empty, and has no equivalent for Claude Code at all).
- **Infra failures get one automatic retry**: a trial that crashed or timed out with no reward, and
  whose logs match a known infra-noise pattern (connect/read timeout, rate limit, DNS failure, Docker
  orchestration error — see `infra_marker_scan.py`), is retried once automatically at the end of the
  Agent Trials run, rather than counted as the agent genuinely failing the task. The original failure
  is kept, not discarded, in the trial's logs; a retry that shows infra noise again is left as-is for a
  human rather than retried forever. A real reward=0 attempt (the agent tried and failed) is never
  touched by this.
- **Failure analysis**: the Failure Analysis card's "Analyze Trials" button runs `harbor analyze`
  over every finished agent trial (passes too) and shows a summary plus `reward_hacking` and
  `task_specification` checks for each. It uses `ANALYZE_MODEL` (default `openai/gpt-6-luna`, a few
  cents per run) through `ANALYZE_AGENT` (Terminus-2), needs `OPENAI_API_KEY`, and is advisory. The
  evaluator sees the task's tests and the trajectories, so use keys with no-training / zero-retention
  terms. Its labels are Harbor's trial folder names, not the "Trial #N" numbers. Its Rubric dropdown
  (Default / TB) swaps in a vendored, richer 6-criterion Terminal-Bench rubric instead of Harbor's own
  2-criterion default, and additionally synthesizes a job-level summary across all trials (a step
  Harbor itself doesn't have).
- **Rubric dropdowns (Failure Analysis, Rubric Check) and Cheat Trial's Prompt dropdown**: all three
  can swap in a richer, vendored Terminal-Bench alternative (`backend/vendor/tb_prompts/`, copied
  verbatim from `harbor-framework/terminal-bench` at a pinned commit -- see `PROVENANCE.md` there)
  instead of Harbor's own default. Rubric Check's TB option is a 35-criterion review (vs. Harbor's
  own 11); Cheat Trial's TB prompt is a more aggressive, explicit red-team-style directive than the
  platform's own hand-written one. None of these are the default -- pick them per-run when the
  richer review is worth the extra cost.
- **Rubric check**: the Rubric Check card's "Run Rubric Check" button runs `harbor check` — a
  different command from `harbor analyze` above: it scores the *task itself* (instruction, tests,
  environment) against Harbor's quality rubric, not agent trials. Uses `CHECK_MODEL` (default
  `anthropic/claude-fable-5-1`) through `CHECK_AGENT` (Claude Code), needs `ANTHROPIC_API_KEY`, and
  is advisory. A task with a very large `environment/` (e.g. vendored dependencies) can make
  Harbor's own launcher fail with an OS argument-length error; that shows up as an error in the
  stage's logs, not a real quality verdict — strip large vendor directories before checking if you
  hit this.
- **Static checks**: the Static Checks card's "Run Static Checks" button runs the same mechanical,
  LLM-free checks (word counts, formatting, Dockerfile/test.sh/zip hygiene) that Review Report already
  used internally, but persists the result as its own stage so it's independently visible and part of
  the evidence bundle below. No Docker, no LLM cost. Advisory.
- **TB Static Checks**: a second, separate stage running Terminal-Bench's own official static checks
  (`backend/vendor/tb_checks/`, 28 `check-*.sh` scripts vendored verbatim from
  `harbor-framework/terminal-bench` at a pinned commit -- see `PROVENANCE.md` there). This is not a
  replacement for the platform's own Static Checks above -- the two rule sets barely overlap (TB's
  checks framework compliance: canary, dockerfile platform/sanity, separate verifier, task fields,
  absolute paths; ours checks platform-specific hygiene: instruction word count, Dockerfile digest
  pinning) -- so both run. The task folder name is checked by the platform's own Static Checks
  (`task_folder_name`), because here the scripts run against a link named after `[task] name`. No Docker,
  no LLM cost. Advisory.
- **AI Detection**: Terminal-Bench's own Layer 3 AI-usage detection (`backend/vendor/tb_checks/`,
  `check_ai_detection.py`, same pinned commit) -- calls the real GPTZero API against
  `instruction.md` and `solution/solve.sh`, flagging anything at or above 70% AI-generated
  probability. Needs `GPTZERO_API_KEY` (a paid third-party subscription, not an Anthropic/OpenAI
  key); without it, the check runs a graceful no-op (prints a note, passes) exactly like it does
  in Terminal-Bench's own workflow, where this layer is genuinely optional too -- it's not wired
  into any of their automatic CI, only enabled on request with that secret configured. No Docker.
- **Evidence bundle & auto-filled summary**: `GET /submissions/{id}/evidence` downloads one JSON file
  with the Docker build log, the Static Checks report, and the Rubric Check report, plus a
  `summary_markdown` field (C8) rendered straight from that same run data — task name, checksum
  consistency, every gate's status, agent trial pass rate and **pass@k for every k from 1 to n** (C6;
  the standard unbiased estimator `1 - C(n-c,k)/C(n,k)`, unlike Harbor's own job-level reporting, which
  only ever fills in powers-of-2/multiples-of-5 k values and never computes pass@1 at all), and total
  recorded LLM spend. No LLM call generates it, so every field is a real value or an explicit "not run
  yet"/"not recorded" — never a `[fill]` placeholder. Every absolute `/Users/<user>/...` or
  `/home/<user>/...` path in the bundle has its username redacted (C10) before this leaves the platform.
  The "Download Evidence" button in the header triggers this from the UI.
- **Cheat trial**: the Cheat Trial card's "Run Cheat Trial" button runs one agent trial with an
  explicit directive to cheat spliced into the instruction (edit the verifier, hardcode outputs,
  read the reference solution). Its Model dropdown (API: `cheat-trial?agent=...&model=...`) and
  Reasoning dropdown both default to **Same as Agent Trials**, meaning the agent, model and reasoning
  currently selected on the Agent Trials card; change either to run the cheat trial on another model
  (Codex takes `openai/*` models, Claude Code `anthropic/*`). Mind the reasoning default: a cheat
  trial run at whatever `AGENT_REASONING_EFFORT` happens to be (e.g. low) is a weaker test of whether
  the agent will cheat than one run at max effort, and it's easy to leave that setting stale without
  noticing (confirmed live: a real cheat trial once ran at low purely because of that), so pick max
  for a strict test. A pass
  (reward stays 0) means the anti-cheat design held even when told to cheat. reward=1 alone doesn't
  say the agent found a real cheat — confirmed live on two different harnesses, both got reward 1
  after refusing the directive and solving the task honestly instead — so a reward=1 trial
  automatically runs a `harbor analyze` `reward_hacking` check on itself, and only passes if that
  check positively clears it; a real finding, or a check that can't resolve it, stays failed for a
  human to read.
- **Tasks with a docker-compose.yaml**: the Codex bake is skipped for them, so Harbor installs Codex
  inside each trial (10-16 minutes per trial instead of about 4). Results are unaffected.
- **Judge file limits**: Sufficiency reads up to `JUDGE_MAX_FILE_CHARS` (150000) per
  file and `JUDGE_MAX_TOTAL_CHARS` (500000) in all; a judge that sees only part of a long dossier
  reports the rest as missing.
- **Task network policy**: Harbor 0.23+ only enforces `no-network` when Docker's kernel supports
  its egress-control sidecar (Docker Desktop for Mac doesn't) and otherwise rejects the task, so
  older tasks with `allow_internet = false` would fail every stage. `HARBOR_NETWORK_MODE` (default
  `public`) runs a temporary copy of each task with `network_mode = "public"`, and the stage logs
  say so. Set it empty to respect each task's own setting.
- **Resetting local state**: submission data lives in `backend/storage/submissions/` (safe to
  delete) and in the `taskeval-pg-data` Docker volume (`docker compose down -v` wipes it).
- **Docker image buildup**: each submission builds a tagged image (`taskeval/{id}:build`). These accumulate over time — `docker image prune -a` reclaims space from ones no
  longer referenced by a submission you care about.
