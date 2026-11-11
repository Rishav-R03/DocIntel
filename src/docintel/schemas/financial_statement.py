"""``financial_statement.v1``: balance sheets, income and cash-flow statements."""

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
    parse_choice,
    parse_currency,
    parse_date,
    parse_money,
    parse_text,
)

STATEMENT_TYPES = frozenset({"balance_sheet", "income_statement", "cash_flow", "other"})

PROMPT = """\
Extract the financial statement fields defined by the response schema.
- Report amounts in the statement's presentation currency and unit as printed. If the statement
  says "in thousands" or "in millions", multiply so values are in whole currency units.
- Negative amounts (often shown in parentheses) must be negative numbers.
- `line_items` lists each labelled row in document order, with its section heading.
- Leave totals null when the statement does not show them; never compute them yourself."""

CRITICAL_FIELDS = ("company_name", "statement_type", "period_end")
_BALANCE_PENALTY = 0.8


class StatementLineWire(WireModel):
    section: ConfidentStr = Field(description="Section heading the row belongs to, if any.")
    label: ConfidentStr
    amount: ConfidentNumber


class FinancialStatementWire(WireModel):
    company_name: ConfidentStr
    statement_type: ConfidentStr = Field(
        description="One of: balance_sheet, income_statement, cash_flow, other."
    )
    period_start: ConfidentDate
    period_end: ConfidentDate = Field(description="Period end or balance-sheet date.")
    currency: ConfidentStr = Field(description="ISO 4217 currency code.")
    total_assets: ConfidentNumber
    total_liabilities: ConfidentNumber
    total_equity: ConfidentNumber
    revenue: ConfidentNumber
    net_income: ConfidentNumber
    line_items: list[StatementLineWire]
    document_validity_score: float = Field(ge=0, le=1)


class StatementLine(BaseModel):
    section: Extracted[str]
    label: Extracted[str]
    amount: Extracted[Decimal]


class FinancialStatement(BaseModel):
    company_name: Extracted[str]
    statement_type: Extracted[str]
    period_start: Extracted[date]
    period_end: Extracted[date]
    currency: Extracted[str]
    total_assets: Extracted[Decimal]
    total_liabilities: Extracted[Decimal]
    total_equity: Extracted[Decimal]
    revenue: Extracted[Decimal]
    net_income: Extracted[Decimal]
    line_items: list[StatementLine]


def to_domain(
    wire: FinancialStatementWire,
) -> tuple[FinancialStatement, list[ExtractionWarning]]:
    c = Conversion(warnings=[])
    statement = FinancialStatement(
        company_name=c.field("company_name", wire.company_name, parse_text),
        statement_type=c.field(
            "statement_type", wire.statement_type, parse_choice(STATEMENT_TYPES)
        ),
        period_start=c.field("period_start", wire.period_start, parse_date),
        period_end=c.field("period_end", wire.period_end, parse_date),
        currency=c.field("currency", wire.currency, parse_currency),
        total_assets=c.field("total_assets", wire.total_assets, parse_money),
        total_liabilities=c.field("total_liabilities", wire.total_liabilities, parse_money),
        total_equity=c.field("total_equity", wire.total_equity, parse_money),
        revenue=c.field("revenue", wire.revenue, parse_money),
        net_income=c.field("net_income", wire.net_income, parse_money),
        line_items=[
            StatementLine(
                section=c.field(f"line_items[{i}].section", li.section, parse_text),
                label=c.field(f"line_items[{i}].label", li.label, parse_text),
                amount=c.field(f"line_items[{i}].amount", li.amount, parse_money),
            )
            for i, li in enumerate(wire.line_items)
        ],
    )
    return statement, c.warnings


def rules(statement: FinancialStatement) -> list[RuleViolation]:
    out: list[RuleViolation] = []
    assets = statement.total_assets.value
    liabilities = statement.total_liabilities.value
    equity = statement.total_equity.value
    if (
        statement.statement_type.value == "balance_sheet"
        and assets is not None
        and liabilities is not None
        and equity is not None
    ):
        gap = abs(assets - (liabilities + equity))
        if gap > max(Decimal("1"), abs(assets) * Decimal("0.01")):
            out.append(
                RuleViolation(
                    ExtractionWarning(
                        code="BALANCE_SHEET_UNBALANCED",
                        field="total_assets",
                        detail=f"assets {assets} != liabilities {liabilities} + equity {equity}",
                    ),
                    _BALANCE_PENALTY,
                )
            )
    start, end = statement.period_start.value, statement.period_end.value
    if start is not None and end is not None and end < start:
        out.append(
            RuleViolation(
                ExtractionWarning(
                    code="PERIOD_REVERSED", field="period_end", detail=f"{end} is before {start}"
                ),
                0.9,
            )
        )
    return out
