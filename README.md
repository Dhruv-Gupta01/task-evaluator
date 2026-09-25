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
- **Agent timeout and run cost cap**: `AGENT_TIMEOUT_SEC` forces one agent timeout on every task
  (Terminal-Bench 4.0 uses 8 hours, `28800`); empty keeps each task's own. `AGENT_RUN_BUDGET_USD`
  (default $5) stops an agent-trials run once its own cost reaches it, and a run is also stopped
  when it would exceed what is left of this month's `LLM_BUDGET_USD`. The cost is checked every 10
  seconds, so a run can overshoot the cap by a few cents.
- **Codex agent**: set `HARBOR_AGENT=codex` and `AGENT_MODEL=openai/gpt-6-astra` in `backend/.env`
  to run trials with OpenAI's Codex instead of Terminus-2. Codex is baked into each task's image at build time (`CODEX_BAKE_INTO_IMAGE`, default on;
  the first build of a task takes about a minute longer, later trials start in seconds), needs the network (`HARBOR_NETWORK_MODE=public`,
  the default), has the OpenAI key inside the container, and has no turn cap: only the task's
  agent timeout bounds its cost. Pin its version with `CODEX_VERSION`.
- **Task network policy**: Harbor 0.23+ only enforces `no-network` when Docker's kernel supports
  its egress-control sidecar (Docker Desktop for Mac doesn't) and otherwise rejects the task, so
  older tasks with `allow_internet = false` would fail every stage. `HARBOR_NETWORK_MODE` (default
  `public`) runs a temporary copy of each task with `network_mode = "public"`, and the stage logs
  say so. Set it empty to respect each task's own setting.
- **Resetting local state**: submission data lives in `backend/storage/submissions/` (safe to
  delete) and in the `taskeval-pg-data` Docker volume (`docker compose down -v` wipes it).
- **Docker image buildup**: each submission builds a tagged image (`taskeval/{id}:build`). These accumulate over time — `docker image prune -a` reclaims space from ones no
  longer referenced by a submission you care about.
