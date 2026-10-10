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
    *,
    agent: str | None = None,
    reasoning_effort: str | None = None,
    trial_index: int | None = None,
    detail: str | None = None,
    n_cache_write_tokens: int | None = None,
    unit: str | None = None,
    unit_count: int | None = None,
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
            agent=agent,
            reasoning_effort=reasoning_effort or None,
            trial_index=trial_index,
            detail=detail,
            n_cache_write_tokens=n_cache_write_tokens,
            unit=unit,
            unit_count=unit_count,
        )
    )


def judge_exceeded_message(db: Session) -> str | None:
    """Same as exceeded_message, but only for judge stages, which cost money
    only when the configured judge model has a known price."""
    if not pricing.is_priced(settings.llm_model):
        return None
    return exceeded_message(db)


def record_calls(
    db: Session,
    submission_id: str,
    stage: str,
    calls: list[tuple[str, Usage]],
    reasoning_effort: str | None = None,
) -> None:
    """Record collected judge calls. A model without a known price is still
    recorded (cost 0, flagged in `detail`) so the cost report shows the call
    happened instead of silently omitting it."""
    for i, (model, usage) in enumerate(calls, start=1):
        cost = pricing.cost_usd(model, usage)
        record(
            db,
            submission_id,
            stage,
            model,
            cost if cost is not None else 0.0,
            usage.input_tokens,
            usage.cached_tokens,
            usage.output_tokens,
            agent="direct API call",
            reasoning_effort=reasoning_effort,
            detail=f"call {i}" + ("" if cost is not None else " (model not in price table; cost not computed)"),
            n_cache_write_tokens=usage.cache_write_tokens,
        )


def gptzero_cost_usd(words: int) -> float:
    """GPTZero is billed per word in INR; convert at the configured rate."""
    return words * settings.gptzero_inr_per_word / settings.usd_inr_rate
