# Task Evaluator

A local platform for grading Terminal-Bench-style task submissions. A candidate/task-author
uploads a task zip (environment + solution + tests), and the platform runs it through five
gates against real Docker containers:

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

Edit `.env` and fill in **one** provider's API key matching `LLM_PROVIDER` (defaults to
`fireworks`). Everything else in `.env.example` works as-is for a local setup.

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
  (`backend/vendor/tb_checks/`, 26 `check-*.sh` scripts vendored verbatim from
  `harbor-framework/terminal-bench` at a pinned commit -- see `PROVENANCE.md` there). This is not a
  replacement for the platform's own Static Checks above -- the two rule sets barely overlap (TB's
  checks framework compliance: canary, dockerfile platform/sanity, separate verifier, task fields,
  absolute paths; ours checks platform-specific hygiene: instruction word count, Dockerfile digest
  pinning) -- so both run. No Docker, no LLM cost. Advisory.
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
- **Judge file limits**: Sufficiency and Code Smell read up to `JUDGE_MAX_FILE_CHARS` (150000) per
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
