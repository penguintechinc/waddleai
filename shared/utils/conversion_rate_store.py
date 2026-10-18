"""Idempotent writers for ``token_conversion_rates`` (UNIQUE(provider, model), spec §4).

``ensure_conversion_rate`` seeds without ever clobbering an admin edit; ``upsert_conversion_rate``
is the admin write path. Both tolerate the concurrent-writer race by catching the unique violation
(penguin-dal runs each statement in its own transaction, so no rollback is needed or possible).
"""

import logging
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from shared.utils.default_conversion_rates import RateRow

logger = logging.getLogger(__name__)


def _match(db, provider: str, model: str):
    """Query selecting the single row for ``(provider, model)``."""
    t = db.token_conversion_rates
    return db((t.provider == provider) & (t.model == model))


def ensure_conversion_rate(db, row: RateRow) -> bool:
    """Insert ``row`` if its (provider, model) is absent; return True only when inserted."""
    if _match(db, row.provider, row.model).select().first() is not None:
        return False
    try:
        db.token_conversion_rates.insert(
            provider=row.provider,
            model=row.model,
            input_rate=row.input_rate,
            output_rate=row.output_rate,
            base_cost_per_waddleai_token=row.base_cost_per_waddleai_token,
        )
    except IntegrityError:
        logger.debug("ensure_conversion_rate lost race for %s:%s", row.provider, row.model)
        return False  # a concurrent seeder won the race
    return True


def upsert_conversion_rate(
    db,
    *,
    provider: str,
    model: str,
    input_rate: float,
    output_rate: float,
    base_cost_per_waddleai_token: float = 0.001,
    enabled: bool = True,
) -> int:
    """Create or update the rate for ``(provider, model)`` and return its row id."""
    values = {
        "input_rate": input_rate,
        "output_rate": output_rate,
        "base_cost_per_waddleai_token": base_cost_per_waddleai_token,
        "enabled": enabled,
        "effective_date": datetime.utcnow(),
    }
    for _ in range(3):
        if _match(db, provider, model).update(**values):
            return int(_match(db, provider, model).select().first().id)
        try:
            return int(db.token_conversion_rates.insert(provider=provider, model=model, **values))
        except IntegrityError:
            logger.debug("upsert_conversion_rate lost insert race for %s:%s", provider, model)
            continue  # lost an insert race: the next loop updates the winner's row
    raise RuntimeError(f"upsert_conversion_rate({provider}:{model}) did not converge")
