from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import get_settings
from app.db import Base, engine
from app.routers import submissions
from app.services import docker_orchestrator, task_runner

settings = get_settings()


def _add_missing_llm_spend_columns() -> None:
    """create_all never alters an existing table, so databases created before
    the cost-report columns existed get them added here (nullable, no data loss)."""
    with engine.begin() as conn:
        for name, sql_type in (
            ("agent", "VARCHAR"),
            ("reasoning_effort", "VARCHAR"),
            ("trial_index", "INTEGER"),
            ("detail", "VARCHAR"),
            ("n_cache_write_tokens", "INTEGER"),
            ("unit", "VARCHAR"),
            ("unit_count", "INTEGER"),
        ):
            conn.execute(text(f"ALTER TABLE llm_spend ADD COLUMN IF NOT EXISTS {name} {sql_type}"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _add_missing_llm_spend_columns()
    docker_orchestrator.reap_orphans()
    task_runner.reset_stuck_rows()
    yield


app = FastAPI(title="Task Evaluator Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    allow_credentials=False,
)

app.include_router(submissions.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
