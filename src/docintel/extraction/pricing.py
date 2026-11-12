"""Effective-dated model prices (USD per 1M tokens, Standard tier).

Kept as code, not runtime config: a price change is reviewed like any other change, and every
audit row records the model and timestamp, so historical cost can always be recomputed.
Source: Gemini API pricing page, checked 2026-11-11. "Output price (including thinking tokens)".
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

_PER_TOKEN = Decimal(1_000_000)
_COST_QUANTUM = Decimal("0.000001")  # matches NUMERIC(12,6)


@dataclass(frozen=True)
class Price:
    effective_from: date
    input_per_mtok: Decimal
    output_per_mtok: Decimal  # also applies to thinking tokens


PRICES: dict[str, tuple[Price, ...]] = {
    "gemini-3.8-flash": (
        Price(date(2026, 9, 2), Decimal("0.75"), Decimal("3.75")),  # introductory, to 2026-12-31
        Price(date(2027, 1, 1), Decimal("1.50"), Decimal("7.50")),
    ),
    # Fallback model. No scheduled change listed (checked 2026-11-12).
    "gemini-3.5-flash-lite": (Price(date(2026, 1, 1), Decimal("0.30"), Decimal("2.50")),),
}


class UnknownModelPriceError(KeyError):
    pass


def price_for(model: str, at: datetime | None = None) -> Price:
    day = (at or datetime.now(UTC)).date()
    try:
        schedule = PRICES[model]
    except KeyError:
        raise UnknownModelPriceError(model) from None
    applicable = [p for p in schedule if p.effective_from <= day]
    if not applicable:
        raise UnknownModelPriceError(f"{model} has no price effective on {day}")
    return max(applicable, key=lambda p: p.effective_from)


def cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    thought_tokens: int,
    at: datetime | None = None,
) -> Decimal:
    price = price_for(model, at)
    total = (
        Decimal(input_tokens) * price.input_per_mtok
        + Decimal(output_tokens + thought_tokens) * price.output_per_mtok
    ) / _PER_TOKEN
    return total.quantize(_COST_QUANTUM)
