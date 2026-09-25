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

# Backend (from backend/). Needs Docker running, the harbor CLI on PATH, and one provider key in .env
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

**Request → background task flow.** `routers/submissions.py` exposes `POST /submissions/{id}/<stage>`. Each endpoint marks the row `pending` and hands a coroutine from `services/task_runner.py` to `workers/task_queue.submit(key, coro)`. The queue is in-process asyncio only: resubmitting the same `"{id}:{stage}"` key cancels the running task and restarts it. Blocking Docker calls run through `asyncio.to_thread`. Cancelling a Harbor-backed stage sends SIGINT to the `harbor` process so it removes its containers. On startup, `reset_stuck_rows()` marks any leftover `pending`/`running` rows as failed, and `docker_orchestrator.reap_orphans()` removes containers labelled `taskeval.submission_id`.

**Data model.** `Submission` holds build state, the image tags and the cached parsed `task.toml` (`task_config_json`, a `TaskConfig` model). Every other stage result is a `Run` row keyed by `(submission_id, kind, run_index)`. `kind` is one of `oracle`, `nop`, `agent` (one row per trial), `sufficiency`, `leakage_scan`, `code_smell` or `review_report`. `services/submission_service.to_schema` turns runs into the per-stage API shape. For example, it inverts Nop's status, because reward 0 counts as a pass there. `schemas.py` and the types in `src/lib/api.ts` must stay in sync by hand.

**Oracle, Nop and Agent Trials run through Harbor.** `services/harbor_runner.py` shells out to `harbor run` (`-a oracle`, `-a nop`, or `settings.harbor_agent` for trials) (the real grading pipeline's harness; install with `uv tool install harbor==0.23.0`, the tested version) and reads the reward from `<jobs-dir>/<job>/<trial>/result.json` at `verifier_result.rewards.reward`. **Network policy:** Harbor 0.23+ replaced `allow_internet` with `[environment].network_mode` (`public` default, `no-network`, `allowlist`), and on Docker it only enforces the last two through an egress-control sidecar that needs kernel support Docker Desktop for Mac lacks; otherwise it rejects the task. So `harbor_runner._prepare_task` copies each task and rewrites its `[environment]` to `HARBOR_NETWORK_MODE` (default `public`), leaving the extracted files alone and noting it at the top of the trial logs. Empty disables this. Harbor builds its own image on each run, so these stages don't use `Submission.image_tag`, but the router still requires Build to pass first. `DOCKER_PLATFORM` is passed to Harbor as `DOCKER_DEFAULT_PLATFORM`, because Harbor doesn't pin a platform itself. A run with no reward (build failure, crash, timeout) is stored as `failed` with `reward=None` and must not be shown as a Nop pass.

**Agent trials.** One `harbor run --n-attempts n` job with the Terminus-2 agent by default. The model is `AGENT_MODEL` (a LiteLLM string such as `openai/gpt-6-astra`), or derived from `LLM_PROVIDER`/`LLM_MODEL` when that's empty. `LLM_MAX_ITERS` becomes Terminus's `max_turns`, and `AGENT_REASONING_EFFORT` is passed through when set. `HARBOR_AGENT=codex` switches to OpenAI's Codex agent, which behaves very differently from Terminus-2: it runs inside the task container, so it needs `HARBOR_NETWORK_MODE=public`; Harbor's own install step (apt, an `nvm` download from GitHub, `npm install`, 6+ minutes and easily broken by a flaky route to `raw.githubusercontent.com`) is avoided by `CODEX_BAKE_INTO_IMAGE` (default on): `harbor_runner._prepare_task` appends a layer to the temporary task copy's `environment/Dockerfile` that installs Node (nodejs.org) and Codex (npm) at image build, best-effort, and Harbor skips its install when `command -v codex` succeeds (pin the version with `CODEX_VERSION`; empty bakes whatever is newest at first build, then it is cached). It runs, so and it needs an `openai/*` model and receives the OpenAI key inside the container. Harbor >= 0.23 rejects agent options the agent doesn't declare, so `task_runner._agent_kwargs` gives each agent only its own (Terminus: `max_turns` + `reasoning_effort`; Codex: `reasoning_effort`, `version` from `CODEX_VERSION`, `web_search` from `CODEX_WEB_SEARCH`, and no turn cap, so only the task's agent timeout bounds its cost). `_agent_config_error` fails the trials with a clear message for a bad Codex setup. Codex leaves `reasoning_effort` at its own default unless `AGENT_REASONING_EFFORT` is set. If an agent reports tokens but no cost, `harbor_runner` prices them from `llm/pricing.py`. Each trial's logs start with the agent, model and options used. Terminus-2 runs on the host and drives the task container, so provider keys (`harbor_runner._litellm_key_env`) never enter the container and the task's `allow_internet` setting holds. Harbor copies `tests/` into the container only after the agent finishes, so the agent never sees them. `run_job` polls for each trial's `result.json`, which is written once when the trial finishes, and fills the pre-created `Run` rows in completion order. Token counts and `cost_usd` from Harbor go into each trial's logs.

**Agent timeout and the run cost cap.** `AGENT_TIMEOUT_SEC` (Terminal-Bench 4.0 uses a flat 28800 = 8h) is written into the same temporary task copy as the network policy (`harbor_runner._prepare_task` and `_rewrite_toml`), so Harbor enforces the exact value with no multiplier math, and `task_runner._agent_timeout_sec` feeds it into the backend's own outer timeouts, which would otherwise kill a long run early. Long timeouts make one runaway trial able to spend the whole month before anything checks, so `run_job` has a watchdog: every 10s it sums finished trials' costs plus each in-flight trial's running cost and stops the job (SIGINT, so Harbor removes its containers) once it reaches `AGENT_RUN_BUDGET_USD` or what is left of `LLM_BUDGET_USD`. In-flight cost comes from Terminus's `agent/trajectory.json` (rewritten every turn, on the host) or, for Codex, from `docker exec` reading the session file in the trial's container (`/tmp/codex-home/sessions`), because Codex only copies it out at the end and its own log reports usage once, at the end. Trials cut short report no cost themselves, so `_parse_trial` falls back to the last live figure, and that partial cost is still recorded in `llm_spend`. The check interval means a run can overshoot the cap by a few cents.

**Budget guard** (`services/budget.py`). `LLM_BUDGET_USD` (default 50, `0` = off) caps priced LLM spend per UTC calendar month, recorded in the `llm_spend` table (separate from `Run`, because re-running agent trials deletes the old `Run` rows). Two sources feed it: each finished agent trial's Harbor `cost_usd`, and each judge call's token usage priced by `services/llm/pricing.py` (which includes the cache-write price: the API reports `cache_write_tokens` separately and bills them 25% above plain input for gpt-6-astra, so nearly every fresh prompt pays it). Only models in that price table (currently `gpt-6-astra`) are tracked; Fireworks and other providers are skipped, and the judge endpoints only enforce the cap when the judge model is priced. The agent and judge endpoints refuse to start once the cap is reached, and `run_agent_trials` stops the Harbor job (via `on_trial` returning a reason) after the trial that crosses it. Cost is only known when a trial or call finishes, so in-flight work can overshoot. `GET /budget` returns the month's spend and the limit.

**LLM providers.** Set by `LLM_PROVIDER`/`LLM_MODEL` in `backend/.env`. These drive the LLM-judge stages (and the agent model unless `AGENT_MODEL` is set). The options are anthropic, openai, gemini, groq and fireworks (the default). Groq and Fireworks reuse `OpenAIClient` with a different `base_url`. For the real OpenAI provider, `OpenAIClient` also gets `LLM_REASONING_EFFORT` and `LLM_MAX_OUTPUT_TOKENS` (as `max_completion_tokens`, which includes reasoning tokens) from the factory, and reports each call's token usage to `llm/usage.py`, a ContextVar collector that `task_runner._track_llm_spend` turns into `llm_spend` rows, since the judges create their own client and return only a verdict. All clients implement `llm/base.LLMClient` and share `rate_limiter.TokenBucket`. The LLM-judge stages are `sufficiency_judge`, `code_smell_judge` and `review_report`. The first two read task files through `services/judge_files.py`, which lists every file (size, and whether its contents are shown), detects text by content instead of extension, and caps each file at 20k characters and the dump at 120k. The listing matters: without it a judge that sees only some files reports the others as missing. Review Report requires every required gate to be in a terminal state first (`all_gates_terminal`).

**Advisory vs. gating stages.** Leakage Scan (regex over task files), Code Smell (an LLM opinion) and Review Report ("passed" only means the report was generated) are advisory. Their `passed: false` does not disqualify a submission, so don't treat them as gates or merge them with each other (see the comments in `src/lib/api.ts`). `static_checks.py` and `infra_marker_scan.py` produce heuristic findings from the task files and the trial logs.

## Frontend

- File-based routing in `src/routes/` (`index.tsx` = submission list/upload, `submissions.$id.tsx` = stage dashboard, which polls with TanStack Query `refetchInterval` while stages run). `src/routeTree.gen.ts` is generated, so don't edit it. `__root.tsx` is the only root layout, so don't add Next.js/Remix-style `pages/` or `layout.tsx` files.
- `vite.config.ts` uses `@lovable.dev/vite-tanstack-config`, which already bundles the TanStack Start, React, Tailwind, tsconfig-paths and nitro plugins. Don't add them again, or you'll get duplicate plugins that break the build.
- `src/components/ui/` is generated shadcn/ui code. The `@/` alias maps to `src/`.
