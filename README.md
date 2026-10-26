# Task Evaluator

A local platform for grading Terminal-Bench-style task submissions. A candidate/task-author
uploads a task zip (environment + solution + tests), and the platform runs it through five
gates against real Docker containers, plus a set of advisory checks (see
[Stages in detail](#stages-in-detail-40-and-41)):

1. **Build** — builds the task's `environment/Dockerfile`.
2. **Oracle** — runs `harbor run -a oracle`, confirms the reference solution scores reward `1`.
3. **Nop** — runs `harbor run -a nop`, confirms a do-nothing agent scores reward `0`.
4. **Sufficiency** — an LLM judge checks whether the hidden test requirements are inferable
   from the visible instructions and files.
5. **Agent Trials** — runs `harbor run` with the Terminus-2 agent N times, reporting the pass
   rate and each trial's token usage and cost.

Everything runs locally: FastAPI backend, React/Vite frontend, Postgres, and Docker.

## Prerequisites

- **Docker Desktop** (or another local Docker daemon) — running, before you start the backend.
- **Python 3.13+** and [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
- **[Harbor](https://github.com/laude-institute/harbor)** CLI — runs the Oracle, Nop and Agent
  Trials stages with the same harness as the real grading pipeline: `uv tool install harbor==0.23.0` (the version this was tested with)
- **Node.js** and [`bun`](https://bun.sh) (this repo is bun-lockfile-managed; `npm` will also
  work off `package-lock.json` if you don't have bun, but bun is what's actually been tested)
- An API key for at least one LLM provider (Anthropic, OpenAI, Gemini, Groq, or
  [Fireworks](https://fireworks.ai) — Fireworks is the default and is what this project has
  been run against)

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

## Stages in detail (4.0 and 4.1)

The submission page runs top to bottom in three sections: **Before the agent trials**, **Agent
trials**, **After the agent trials**. A button that depends on an earlier stage is greyed out and
says what it is waiting for. Only Build, Oracle, Nop, Sufficiency and Agent Trials are gates;
everything else is **advisory** and never changes a gate. All of it comes from the task files
and Harbor's own results, so a finding is a question for a reviewer, not a verdict.

Items marked **(4.1)** are new in the 4.1 branch; the rest came with 4.0.

### Before the agent trials

| Stage | What it checks | Needs |
|---|---|---|
| Validate & Build | Task contract, then builds `environment/Dockerfile`. | Docker |
| Oracle | Runs the reference solution N times (default 3, every run kept); gate needs all to score 1. Each run shows time used against its limit, peak memory, platform, agent and verifier user, and tests passed. **(4.1)** Flags runs whose test count differs from the passing runs, marks a mix of 1 and 0 as flaky, and has a "Run under CPU load" button that adds stress runs kept outside the gate. | Docker |
| Nop | A do-nothing agent must score 0. | Docker |
| Image Contents **(4.1)** | After every build, lists the files and package versions the agent's container really has. Sufficiency uses it to tell files that never reach the container from visible ones. | Docker |
| Fresh Build **(4.1)** | Rebuilds with no cache and freshly pulled base images, then runs one Oracle with every `RUN` step redone. Catches a dependency that no longer installs. | Docker, slow |
| Static Checks | Mechanical checks of `instruction.md`, `test.sh`, Dockerfile and zip layout, plus hygiene: named volumes, unpinned installs, `curl \| sh`, key-like strings, x86-only downloads, dangling paths, files that differ between `environment/` and `tests/`, tools the tests need, instruction length, whether the solution reads `tests/`. **(4.1)** Also grader isolation (the verifier importing or running agent code, `eval`/pickle in the grader, results in `/tmp`, reward-file reset), reproducibility (wall-clock assertions, unpinned images and dependencies, run-time downloads) and scoring-file checks. | none |
| TB Static Checks | Terminal-Bench's own `check-*.sh` scripts, vendored verbatim. **(4.1)** Refreshed to upstream `2e29ca9` (28 scripts). | none |
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
  (amber when tests were removed). **Reviewer decision** records a ruling on a contested cluster.
- **Download Evidence**: one JSON with the build log, Static Checks, Rubric Check, per-stage
  results, cost per stage and a summary. **(4.1)** Adds run limits, Oracle notes, Cheat Trial,
  Answer Exposure, Fresh Build, Image Contents and the verifier reports.

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
- **Oracle runs N times, always kept**: the Run Oracle card has an N field (default 3). Every click
  appends N more runs to whatever's already there and shows each one's own status/reward/logs — the
  old behavior silently overwrote the single previous run, which meant you could never actually see
  whether the 3 consecutive runs Gate 1 wants had all passed, only the most recent one. "passed" on
  the card means every run so far has reward=1, not just the latest.
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
  read the reference solution). It uses the same Agent choice as Agent Trials, but its own,
  independent Reasoning dropdown (API: `cheat-trial?reasoning_effort=max`), defaulting to **max**
  rather than the server default -- a cheat trial run at whatever `AGENT_REASONING_EFFORT` happens
  to be (e.g. low) is a weaker test of whether the agent will cheat than one run at max effort, and
  it's easy to leave that setting stale without noticing (confirmed live: a real cheat trial once
  ran at low purely because of that, not a deliberate choice). A pass
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
