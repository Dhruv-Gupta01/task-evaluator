"""Monthly spending cap for priced LLM use: agent trials (cost from Harbor's
per-trial cost_usd) and LLM judge calls (cost from token usage and the price
table in llm/pricing.py). Cost is only known once a trial or call finishes,
so a run can overshoot the cap by whatever is already in flight."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import LlmSpend
from app.services.llm import pricing
from app.services.llm.base import Usage

settings = get_settings()


def _month_start() -> datetime:
    now = datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def spent_this_month(db: Session) -> float:
    total = db.execute(
        select(func.coalesce(func.sum(LlmSpend.cost_usd), 0.0)).where(
            LlmSpend.created_at >= _month_start()
        )
    ).scalar_one()
    return float(total)


def exceeded_message(db: Session) -> str | None:
    """None while under budget (or budget disabled), else a user-facing reason."""
    if settings.llm_budget_usd <= 0:
        return None
    spent = spent_this_month(db)
    if spent < settings.llm_budget_usd:
        return None
    return (
        f"Monthly LLM budget reached: ${spent:.2f} of ${settings.llm_budget_usd:.2f} "
        "spent this month. Raise LLM_BUDGET_USD in backend/.env to continue."
    )


def record(
    db: Session,
    submission_id: str,
    stage: str,
    model: str,
    cost_usd: float,
    n_input_tokens: int | None,
    n_cache_tokens: int | None,
    n_output_tokens: int | None,
) -> None:
    db.add(
        LlmSpend(
            submission_id=submission_id,
            stage=stage,
            model=model,
            cost_usd=cost_usd,
            n_input_tokens=n_input_tokens,
            n_cache_tokens=n_cache_tokens,
            n_output_tokens=n_output_tokens,
            created_at=datetime.now(UTC),
        )
    )


def judge_exceeded_message(db: Session) -> str | None:
    """Same as exceeded_message, but only for judge stages, which cost money
    only when the configured judge model has a known price."""
    if not pricing.is_priced(settings.llm_model):
        return None
    return exceeded_message(db)


def record_calls(
    db: Session, submission_id: str, stage: str, calls: list[tuple[str, Usage]]
) -> None:
    """Record collected judge calls; models without a known price are skipped."""
    for model, usage in calls:
        cost = pricing.cost_usd(model, usage)
        if cost is None:
            continue
        record(
            db,
            submission_id,
            stage,
            model,
            cost,
            usage.input_tokens,
            usage.cached_tokens,
            usage.output_tokens,
        )
