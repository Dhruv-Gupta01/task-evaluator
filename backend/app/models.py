from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Submission(Base):
    __tablename__ = "submissions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    task_name: Mapped[str] = mapped_column(String)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    zip_path: Mapped[str] = mapped_column(String)
    extracted_path: Mapped[str | None] = mapped_column(String, nullable=True)

    build_status: Mapped[str] = mapped_column(String, default="not-run")
    build_logs: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_tag: Mapped[str | None] = mapped_column(String, nullable=True)
    # Unused since agent trials moved to Harbor; kept because there are no
    # migrations to drop the column from existing databases.
    agent_wrapper_image_tag: Mapped[str | None] = mapped_column(String, nullable=True)

    # cached parsed task.toml, re-derived on validate; stored as JSON text
    task_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    runs: Mapped[list["Run"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (UniqueConstraint("submission_id", "kind", "run_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id"))
    kind: Mapped[str] = mapped_column(String)  # "oracle" | "nop" | "agent"
    run_index: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String, default="pending")
    reward: Mapped[int | None] = mapped_column(Integer, nullable=True)
    logs: Mapped[str | None] = mapped_column(Text, nullable=True)
    container_id: Mapped[str | None] = mapped_column(String, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    submission: Mapped["Submission"] = relationship(back_populates="runs")


class LlmSpend(Base):
    """One row per finished paid LLM run (currently each agent trial). Kept
    separate from Run because re-running agent trials deletes the old Run
    rows, and their cost must still count toward the monthly budget."""

    __tablename__ = "llm_spend"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submission_id: Mapped[str] = mapped_column(String)
    stage: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    cost_usd: Mapped[float] = mapped_column(Float)
    n_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_cache_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
