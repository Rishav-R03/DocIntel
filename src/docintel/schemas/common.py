"""Building blocks shared by every extraction schema (implementation.md §4.7.2, ADR-04).

Two layers per schema:

* **Wire** models are what Gemini fills in. They use only JSON-Schema-friendly primitives
  (strings, numbers, nested objects, arrays) and wrap every field with a self-reported
  ``confidence`` and source ``page``. Field ``description``s are part of the prompt.
* **Domain** models are what the backend trusts: ``Decimal`` money, ``date`` values, ISO codes.
  Converting wire -> domain is where messy model output meets strict types; a field that fails to
  convert becomes ``None`` with confidence 0 and a warning, instead of failing the whole document.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_CONFIDENCE = "Probability from 0 to 1 that the value is correct as extracted."
_PAGE = "1-based page number where the value appears, or null if not found."


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfidentStr(WireModel):
    value: str | None = Field(
        description="The value as printed in the document, or null if absent."
    )
    confidence: float = Field(ge=0, le=1, description=_CONFIDENCE)
    page: int | None = Field(default=None, ge=1, description=_PAGE)


class ConfidentNumber(WireModel):
    value: float | None = Field(
        description="Numeric value without currency symbols or thousands separators, or null."
    )
    confidence: float = Field(ge=0, le=1, description=_CONFIDENCE)
    page: int | None = Field(default=None, ge=1, description=_PAGE)


class ConfidentDate(WireModel):
    value: str | None = Field(
        description="Date normalised to ISO 8601 YYYY-MM-DD, or null if absent or ambiguous."
    )
    confidence: float = Field(ge=0, le=1, description=_CONFIDENCE)
    page: int | None = Field(default=None, ge=1, description=_PAGE)


# ----------------------------------------------------------------------------- domain side
class ExtractionWarning(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    field: str | None = None
    detail: str


class Extracted[T](BaseModel):
    value: T | None
    confidence: float
    page: int | None = None


@dataclass(frozen=True)
class RuleViolation:
    warning: ExtractionWarning
    penalty: float  # multiplicative, 0 < penalty <= 1


@dataclass
class Conversion:
    """Accumulates warnings while converting one document from wire to domain types."""

    warnings: list[ExtractionWarning]

    def field[T](
        self,
        name: str,
        wire: ConfidentStr | ConfidentNumber | ConfidentDate,
        parse: Callable[[Any], T],
    ) -> Extracted[T]:
        if wire.value is None:
            return Extracted[T](value=None, confidence=wire.confidence, page=wire.page)
        try:
            return Extracted[T](value=parse(wire.value), confidence=wire.confidence, page=wire.page)
        except (ValueError, ArithmeticError) as exc:
            self.warnings.append(
                ExtractionWarning(
                    code="UNPARSEABLE_VALUE", field=name, detail=f"{type(exc).__name__}: {exc}"
                )
            )
            return Extracted[T](value=None, confidence=0.0, page=wire.page)


# ----------------------------------------------------------------------------- parsers
_ISO_CURRENCY = re.compile(r"^[A-Z]{3}$")


def parse_text(value: str) -> str:
    text = " ".join(value.split())
    if not text:
        raise ValueError("blank")
    return text


def parse_money(value: float) -> Decimal:
    """Floats come off the wire; money must not stay a float. Round to cents via str() so
    1840.5 -> Decimal('1840.50') rather than its binary approximation."""
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError(f"not finite: {value!r}")
    return amount


def parse_decimal(value: float) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError(f"not finite: {value!r}")
    return amount


def parse_date(value: str) -> date:
    return date.fromisoformat(value.strip())


def parse_choice(allowed: frozenset[str]) -> Callable[[str], str]:
    """Normalise a free-text label into one of ``allowed`` (lower-case, underscores)."""

    def parse(value: str) -> str:
        key = "_".join(value.strip().lower().replace("-", " ").split())
        if key not in allowed:
            raise ValueError(f"{value!r} is not one of {sorted(allowed)}")
        return key

    return parse


def parse_currency(value: str) -> str:
    code = value.strip().upper()
    if not _ISO_CURRENCY.fullmatch(code):
        raise ValueError(f"not an ISO 4217 code: {value!r}")
    return code
