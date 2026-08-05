from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import Base, engine
from app.routers import submissions
from app.services import docker_orchestrator, task_runner

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
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
