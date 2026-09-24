# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local platform for grading Terminal-Bench-style task submissions. A user uploads a task zip (`task.toml`, `instruction.md`, `environment/Dockerfile`, `solution/solve.sh`, `tests/test.sh`) and runs it through stages against real Docker containers: Build → Oracle → Nop → Sufficiency → Agent Trials, plus the advisory stages Leakage Scan, Code Smell and Review Report.

Two apps live in one repo: a FastAPI backend in `backend/` and a TanStack Start (React 19, Vite, Tailwind v4, shadcn/ui) frontend at the repo root.

## Lovable constraints (from AGENTS.md)

The repo is connected to Lovable. **Never force-push, and never rebase, amend or squash commits that are already pushed.** Commits pushed to the connected branch (`main`) sync into the Lovable editor, so keep that branch working.

## Commands

```sh
# Postgres (localhost:5433, persistent volume taskeval-pg-data)
docker compose up -d

# Backend (from backend/). Needs Docker running and one provider key in .env
cp .env.example .env
uv sync
uv run uvicorn app.main:app --reload --reload-dir app --port 8001

# Frontend (from repo root). The client defaults to :8000, so set the URL explicitly
bun install
VITE_API_BASE_URL=http://localhost:8001 bun run dev
bun run lint        # eslint
bun run format      # prettier
bun run build
```

There's no test suite and no migrations. Tables come from `Base.metadata.create_all` at startup, so a schema change to an existing table means wiping the DB (`docker compose down -v`). Runtime state lives under `backend/storage/`, which is gitignored and safe to delete.

## Backend architecture

**Request → background task flow.** `routers/submissions.py` exposes `POST /submissions/{id}/<stage>`. Each endpoint marks the row `pending` and hands a coroutine from `services/task_runner.py` to `workers/task_queue.submit(key, coro)`. The queue is in-process asyncio only: resubmitting the same `"{id}:{stage}"` key cancels the running task and restarts it. Blocking Docker calls run through `asyncio.to_thread`, and cancelling doesn't stop a thread already in flight, which is why container names get a random attempt suffix. On startup, `reset_stuck_rows()` marks any leftover `pending`/`running` rows as failed, and `docker_orchestrator.reap_orphans()` removes containers labelled `taskeval.submission_id`.

**Data model.** `Submission` holds build state, the image tags and the cached parsed `task.toml` (`task_config_json`, a `TaskConfig` model). Every other stage result is a `Run` row keyed by `(submission_id, kind, run_index)`. `kind` is one of `oracle`, `nop`, `agent` (one row per trial), `sufficiency`, `leakage_scan`, `code_smell` or `review_report`. `services/submission_service.to_schema` turns runs into the per-stage API shape. For example, it inverts Nop's status, because reward 0 counts as a pass there. `schemas.py` and the types in `src/lib/api.ts` must stay in sync by hand.

**Solve/verify isolation.** This is the core invariant, in `_run_solve_and_verify` and `run_agent_trials`. A host workdir is seeded from the image's WORKDIR. A "doing" container then mounts the workdir plus `/solution` (oracle) or runs the agent (agent trials); Nop skips this step. After that, a **separate** verifier container mounts the workdir plus `/tests`. `tests/` and `solution/` must never be visible in the same container. The reward comes from `/logs/verifier/reward.txt`, which may be float-formatted, and falls back to the exit code only when that file is missing. See `_resolve_reward`.

**Docker details** (`services/docker_orchestrator.py`). Everything is pinned to `linux/amd64`, so it's slow under QEMU on Apple Silicon, and that's expected. Builds shell out to the `docker build` CLI for BuildKit instead of using docker-py's build. When internet is disabled, containers get a bind-mounted static `/etc/hosts` because `network_disabled` wipes the hosts file. Resource limits and timeouts come from `task.toml`, with defaults in `config.py`.

**Agent trials run inside the task's container.** `build_agent_wrapper_image` layers `docker/agent_wrapper/Dockerfile` over the task image and copies `app/services/llm/` and `app/services/agent/` into `/opt/agent_runtime`. As a result:
- Code in `services/llm/` and `services/agent/` runs on **whatever python3 the task's base image ships**, which can be older than 3.11. Keep it compatible with old Python 3: no `datetime.UTC`, no `tomllib`, no newer syntax.
- Those modules must not import `app.config` or anything outside `llm/` and `agent/`. `agent/agent_factory.py` rebuilds the LLM client from plain env vars (passed in by `task_runner._llm_env_vars`) and deliberately duplicates `llm/factory.py`. Change both when you add or change a provider.

**LLM providers.** Set by `LLM_PROVIDER`/`LLM_MODEL` in `backend/.env`. The options are anthropic, openai, gemini, groq and fireworks (the default). Groq and Fireworks reuse `OpenAIClient` with a different `base_url`. All clients implement `llm/base.LLMClient` and share `rate_limiter.TokenBucket`. The LLM-judge stages are `sufficiency_judge`, `code_smell_judge` and `review_report`. Review Report requires every required gate to be in a terminal state first (`all_gates_terminal`).

**Advisory vs. gating stages.** Leakage Scan (regex over task files), Code Smell (an LLM opinion) and Review Report ("passed" only means the report was generated) are advisory. Their `passed: false` does not disqualify a submission, so don't treat them as gates or merge them with each other (see the comments in `src/lib/api.ts`). `static_checks.py` and `infra_marker_scan.py` produce heuristic findings from the task files and the trial logs.

## Frontend

- File-based routing in `src/routes/` (`index.tsx` = submission list/upload, `submissions.$id.tsx` = stage dashboard, which polls with TanStack Query `refetchInterval` while stages run). `src/routeTree.gen.ts` is generated, so don't edit it. `__root.tsx` is the only root layout, so don't add Next.js/Remix-style `pages/` or `layout.tsx` files.
- `vite.config.ts` uses `@lovable.dev/vite-tanstack-config`, which already bundles the TanStack Start, React, Tailwind, tsconfig-paths and nitro plugins. Don't add them again, or you'll get duplicate plugins that break the build.
- `src/components/ui/` is generated shadcn/ui code. The `@/` alias maps to `src/`.
