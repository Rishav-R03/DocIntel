"""``invoice.v1``: invoices and bills."""

from datetime import UTC, date, datetime, timedelta
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
Extract the invoice fields defined by the response schema.
- Monetary amounts are plain numbers in the invoice currency (no symbols, no separators).
- If the invoice shows several totals, `total_amount` is the final amount payable.
- `line_items` lists every billed line in document order; use an empty list if there are none.
- `document_validity_score` reflects how confident you are that this is a legible, complete
  invoice (low for partial scans, non-invoices or unreadable pages)."""

CRITICAL_FIELDS = ("invoice_number", "invoice_date", "vendor_name", "total_amount")
_ARITHMETIC_PENALTY = 0.85
_DATE_PENALTY = 0.9


# ----------------------------------------------------------------------------- wire
class LineItemWire(WireModel):
    description: ConfidentStr
    quantity: ConfidentNumber
    unit_price: ConfidentNumber
    amount: ConfidentNumber = Field(description="Line total as printed.")


class InvoiceWire(WireModel):
    invoice_number: ConfidentStr
    invoice_date: ConfidentDate
    due_date: ConfidentDate
    currency: ConfidentStr = Field(description="ISO 4217 currency code, e.g. USD, EUR, INR.")
    vendor_name: ConfidentStr = Field(description="Issuer / seller of the invoice.")
    vendor_tax_id: ConfidentStr = Field(description="Seller tax id (VAT, GSTIN, EIN), if shown.")
    customer_name: ConfidentStr = Field(description="Billed party / buyer.")
    subtotal: ConfidentNumber = Field(description="Total before tax.")
    tax_amount: ConfidentNumber
    total_amount: ConfidentNumber
    line_items: list[LineItemWire]
    document_validity_score: float = Field(ge=0, le=1)


# ----------------------------------------------------------------------------- domain
class LineItem(BaseModel):
    description: Extracted[str]
    quantity: Extracted[Decimal]
    unit_price: Extracted[Decimal]
    amount: Extracted[Decimal]


class Invoice(BaseModel):
    invoice_number: Extracted[str]
    invoice_date: Extracted[date]
    due_date: Extracted[date]
    currency: Extracted[str]
    vendor_name: Extracted[str]
    vendor_tax_id: Extracted[str]
    customer_name: Extracted[str]
    subtotal: Extracted[Decimal]
    tax_amount: Extracted[Decimal]
    total_amount: Extracted[Decimal]
    line_items: list[LineItem]


def to_domain(wire: InvoiceWire) -> tuple[Invoice, list[ExtractionWarning]]:
    c = Conversion(warnings=[])
    invoice = Invoice(
        invoice_number=c.field("invoice_number", wire.invoice_number, parse_text),
        invoice_date=c.field("invoice_date", wire.invoice_date, parse_date),
        due_date=c.field("due_date", wire.due_date, parse_date),
        currency=c.field("currency", wire.currency, parse_currency),
        vendor_name=c.field("vendor_name", wire.vendor_name, parse_text),
        vendor_tax_id=c.field("vendor_tax_id", wire.vendor_tax_id, parse_text),
        customer_name=c.field("customer_name", wire.customer_name, parse_text),
        subtotal=c.field("subtotal", wire.subtotal, parse_money),
        tax_amount=c.field("tax_amount", wire.tax_amount, parse_money),
        total_amount=c.field("total_amount", wire.total_amount, parse_money),
        line_items=[
            LineItem(
                description=c.field(f"line_items[{i}].description", li.description, parse_text),
                quantity=c.field(f"line_items[{i}].quantity", li.quantity, parse_decimal),
                unit_price=c.field(f"line_items[{i}].unit_price", li.unit_price, parse_money),
                amount=c.field(f"line_items[{i}].amount", li.amount, parse_money),
            )
            for i, li in enumerate(wire.line_items)
        ],
    )
    return invoice, c.warnings


def _violation(code: str, field: str, detail: str, penalty: float) -> RuleViolation:
    return RuleViolation(ExtractionWarning(code=code, field=field, detail=detail), penalty)


def rules(invoice: Invoice, *, today: date | None = None) -> list[RuleViolation]:
    """Cross-field checks a model can't be trusted to satisfy on its own."""
    today = today or datetime.now(UTC).date()
    out: list[RuleViolation] = []
    subtotal, tax, total = (
        invoice.subtotal.value,
        invoice.tax_amount.value,
        invoice.total_amount.value,
    )

    amounts = [li.amount.value for li in invoice.line_items]
    if subtotal is not None and amounts and all(a is not None for a in amounts):
        line_sum = sum((a for a in amounts if a is not None), Decimal(0))
        if abs(line_sum - subtotal) > max(Decimal("0.01"), abs(subtotal) * Decimal("0.01")):
            out.append(
                _violation(
                    "LINE_ITEMS_SUM_MISMATCH",
                    "subtotal",
                    f"sum of line items = {line_sum}, subtotal = {subtotal}",
                    _ARITHMETIC_PENALTY,
                )
            )

    if subtotal is not None and tax is not None and total is not None:
        expected = subtotal + tax
        if abs(expected - total) > Decimal("0.01"):
            out.append(
                _violation(
                    "TOTAL_MISMATCH",
                    "total_amount",
                    f"subtotal + tax = {expected}, total = {total}",
                    _ARITHMETIC_PENALTY,
                )
            )

    issued = invoice.invoice_date.value
    if issued is not None:
        if issued > today + timedelta(days=1):  # 1-day grace for time zones
            out.append(
                _violation(
                    "DATE_IN_FUTURE", "invoice_date", f"{issued} is in the future", _DATE_PENALTY
                )
            )
        elif issued < today - timedelta(days=3653):
            out.append(
                _violation(
                    "DATE_TOO_OLD", "invoice_date", f"{issued} is over 10 years old", _DATE_PENALTY
                )
            )
        due = invoice.due_date.value
        if due is not None and due < issued:
            out.append(
                _violation(
                    "DUE_BEFORE_ISSUE",
                    "due_date",
                    f"due {due} is before issue {issued}",
                    _DATE_PENALTY,
                )
            )
    return out
