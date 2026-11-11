"""``contract.v1``: contracts and agreements."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from docintel.schemas.common import (
    ConfidentDate,
    ConfidentNumber,
    ConfidentStr,
    Conversion,
    Extracted,
    ExtractionWarning,
    RuleViolation,
    WireModel,
    parse_currency,
    parse_date,
    parse_decimal,
    parse_money,
    parse_text,
)

PROMPT = """\
Extract the contract fields defined by the response schema.
- `parties` lists every contracting party with its role as stated (e.g. Licensor, Customer).
- Dates are the dates the contract states, not the dates it was signed, unless they coincide.
- `key_clauses` covers termination, liability caps, payment, confidentiality, renewal and
  governing law where present, each summarised in one sentence. Do not invent clauses."""

CRITICAL_FIELDS = ("title", "effective_date")


class PartyWire(WireModel):
    name: ConfidentStr
    role: ConfidentStr = Field(description="Role as stated in the contract.")


class ClauseWire(WireModel):
    clause_type: ConfidentStr = Field(description="e.g. termination, liability, payment.")
    summary: ConfidentStr = Field(description="One-sentence summary of the clause.")


class ContractWire(WireModel):
    title: ConfidentStr
    parties: list[PartyWire]
    effective_date: ConfidentDate
    expiration_date: ConfidentDate
    governing_law: ConfidentStr = Field(description="Governing law jurisdiction as stated.")
    contract_value: ConfidentNumber = Field(description="Total contract value, if stated.")
    currency: ConfidentStr = Field(description="ISO 4217 code of contract_value, if any.")
    termination_notice_days: ConfidentNumber
    auto_renews: ConfidentStr = Field(description='"yes", "no", or null if not addressed.')
    key_clauses: list[ClauseWire]
    document_validity_score: float = Field(ge=0, le=1)


class Party(BaseModel):
    name: Extracted[str]
    role: Extracted[str]


class Clause(BaseModel):
    clause_type: Extracted[str]
    summary: Extracted[str]


class Contract(BaseModel):
    title: Extracted[str]
    parties: list[Party]
    effective_date: Extracted[date]
    expiration_date: Extracted[date]
    governing_law: Extracted[str]
    contract_value: Extracted[Decimal]
    currency: Extracted[str]
    termination_notice_days: Extracted[Decimal]
    auto_renews: Extracted[str]
    key_clauses: list[Clause]


def _yes_no(value: str) -> str:
    answer = value.strip().lower()
    if answer not in {"yes", "no"}:
        raise ValueError(f"expected yes/no, got {value!r}")
    return answer


def to_domain(wire: ContractWire) -> tuple[Contract, list[ExtractionWarning]]:
    c = Conversion(warnings=[])
    contract = Contract(
        title=c.field("title", wire.title, parse_text),
        parties=[
            Party(
                name=c.field(f"parties[{i}].name", p.name, parse_text),
                role=c.field(f"parties[{i}].role", p.role, parse_text),
            )
            for i, p in enumerate(wire.parties)
        ],
        effective_date=c.field("effective_date", wire.effective_date, parse_date),
        expiration_date=c.field("expiration_date", wire.expiration_date, parse_date),
        governing_law=c.field("governing_law", wire.governing_law, parse_text),
        contract_value=c.field("contract_value", wire.contract_value, parse_money),
        currency=c.field("currency", wire.currency, parse_currency),
        termination_notice_days=c.field(
            "termination_notice_days", wire.termination_notice_days, parse_decimal
        ),
        auto_renews=c.field("auto_renews", wire.auto_renews, _yes_no),
        key_clauses=[
            Clause(
                clause_type=c.field(f"key_clauses[{i}].clause_type", k.clause_type, parse_text),
                summary=c.field(f"key_clauses[{i}].summary", k.summary, parse_text),
            )
            for i, k in enumerate(wire.key_clauses)
        ],
    )
    return contract, c.warnings


def rules(contract: Contract) -> list[RuleViolation]:
    out: list[RuleViolation] = []
    start, end = contract.effective_date.value, contract.expiration_date.value
    if start is not None and end is not None and end < start:
        out.append(
            RuleViolation(
                ExtractionWarning(
                    code="TERM_REVERSED",
                    field="expiration_date",
                    detail=f"expires {end} before it takes effect {start}",
                ),
                0.85,
            )
        )
    if len(contract.parties) < 2:
        out.append(
            RuleViolation(
                ExtractionWarning(
                    code="TOO_FEW_PARTIES",
                    field="parties",
                    detail=f"{len(contract.parties)} party found; contracts need at least two",
                ),
                0.85,
            )
        )
    return out
