# Task Evaluator

A local platform for grading Terminal-Bench-style task submissions. A candidate/task-author
uploads a task zip (environment + solution + tests), and the platform runs it through five
gates against real Docker containers:

1. **Build** — builds the task's `environment/Dockerfile`.
2. **Oracle** — applies the reference solution, confirms the verifier gives reward `1`.
3. **Nop** — runs the verifier against an unsolved workdir, confirms reward `0`.
4. **Sufficiency** — an LLM judge checks whether the hidden test requirements are inferable
   from the visible instructions and files.
5. **Agent Trials** — runs an LLM-driven coding agent against the task N times, reporting the
   pass rate.

Everything runs locally: FastAPI backend, React/Vite frontend, Postgres, and Docker.

## Prerequisites

- **Docker Desktop** (or another local Docker daemon) — running, before you start the backend.
- **Python 3.13+** and [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
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
under `backend/storage/submissions/{id}/`.

## Notes

- **Docker platform**: builds and container runs are pinned to `linux/amd64` to match the real
  grading environment. On Apple Silicon this runs under QEMU emulation and is noticeably slower
  than on a native amd64 Linux host — expected, not a bug.
- **Resetting local state**: submission data lives in `backend/storage/submissions/` (safe to
  delete) and in the `taskeval-pg-data` Docker volume (`docker compose down -v` wipes it).
- **Docker image buildup**: each submission builds 1–2 tagged images (`taskeval/{id}:build`,
  `:agent`). These accumulate over time — `docker image prune -a` reclaims space from ones no
  longer referenced by a submission you care about.
