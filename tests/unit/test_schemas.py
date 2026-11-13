from datetime import date
from decimal import Decimal

import pytest
from pydantic import BaseModel, Field

from docintel.schemas import contract, financial_statement, invoice
from docintel.schemas.registry import REGISTRY, UnsupportedSchemaError, gemini_json_schema
from tests.fakes import cv, invoice_payload


def _codes(violations: list) -> list[str]:  # type: ignore[type-arg]
    return [v.warning.code for v in violations]


# ----------------------------------------------------------------------------- Gemini schema
def _keywords(node: object) -> set[str]:
    """Schema keywords used anywhere, skipping property/definition *names*."""
    if isinstance(node, list):
        return set().union(*(_keywords(n) for n in node)) if node else set()
    if not isinstance(node, dict):
        return set()
    found: set[str] = set()
    for key, value in node.items():
        if key in ("properties", "$defs"):
            found |= set().union(*(_keywords(v) for v in value.values())) if value else set()
        else:
            found |= {key} | _keywords(value)
    return found


@pytest.mark.parametrize("schema_id", sorted(REGISTRY))
def test_registered_schemas_only_use_supported_keywords(schema_id: str) -> None:
    schema = REGISTRY[schema_id].json_schema
    keywords = _keywords(schema)
    assert not {"title", "default"} & keywords  # stripped: descriptive only
    assert schema["additionalProperties"] is False  # extra="forbid" reaches the model


def test_unsupported_keyword_fails_fast() -> None:
    class Bad(BaseModel):
        code: str = Field(pattern=r"^[A-Z]+$")  # "pattern" is not in Gemini's subset

    with pytest.raises(UnsupportedSchemaError, match="'pattern'"):
        gemini_json_schema(Bad)


def test_ignored_constraints_are_rejected_not_silently_dropped() -> None:
    class Ignored(BaseModel):
        name: str = Field(max_length=10)  # Gemini ignores maxLength: we must not rely on it

    with pytest.raises(UnsupportedSchemaError, match="'maxLength'"):
        gemini_json_schema(Ignored)


# ----------------------------------------------------------------------------- conversion
def test_invoice_conversion_normalises_types() -> None:
    wire = invoice.InvoiceWire.model_validate(invoice_payload(subtotal=cv(1000.005)))
    domain, warnings = invoice.to_domain(wire)
    assert domain.invoice_date.value == date(2026, 10, 1)
    assert domain.currency.value == "USD"
    assert domain.vendor_name.value == "Acme Corp"  # whitespace collapsed
    assert domain.total_amount.value == Decimal("1180.00")
    assert domain.subtotal.value == Decimal("1000.00")  # rounded to cents, never a float
    assert domain.total_amount.page == 2
    assert domain.vendor_tax_id.value is None
    assert warnings == []


def test_unparseable_field_becomes_null_with_zero_confidence_and_warning() -> None:
    wire = invoice.InvoiceWire.model_validate(
        invoice_payload(invoice_date=cv("31/02/2026"), currency=cv("dollars"))
    )
    domain, warnings = invoice.to_domain(wire)
    assert domain.invoice_date.value is None
    assert domain.invoice_date.confidence == 0.0
    assert {w.field for w in warnings} == {"invoice_date", "currency"}
    assert {w.code for w in warnings} == {"UNPARSEABLE_VALUE"}


def test_wire_rejects_unknown_keys_and_out_of_range_confidence() -> None:
    with pytest.raises(ValueError, match="Extra inputs"):
        invoice.InvoiceWire.model_validate(invoice_payload(surprise=1))
    with pytest.raises(ValueError, match="less than or equal to 1"):
        invoice.InvoiceWire.model_validate(invoice_payload(total_amount=cv(1, confidence=1.5)))


# ----------------------------------------------------------------------------- business rules
def _invoice_rules(**overrides):  # type: ignore[no-untyped-def]
    domain, _ = invoice.to_domain(invoice.InvoiceWire.model_validate(invoice_payload(**overrides)))
    return invoice.rules(domain, today=date(2026, 11, 11))


def test_consistent_invoice_has_no_violations() -> None:
    assert _invoice_rules() == []


def test_line_items_not_summing_to_subtotal() -> None:
    assert _codes(_invoice_rules(subtotal=cv(990.0), total_amount=cv(1170.0))) == [
        "LINE_ITEMS_SUM_MISMATCH"
    ]


def test_total_not_equal_subtotal_plus_tax() -> None:
    assert _codes(_invoice_rules(total_amount=cv(1200.0))) == ["TOTAL_MISMATCH"]


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"invoice_date": cv("2027-03-01"), "due_date": cv(None, 0.1, None)}, "DATE_IN_FUTURE"),
        ({"invoice_date": cv("2010-01-01"), "due_date": cv(None, 0.1, None)}, "DATE_TOO_OLD"),
        ({"due_date": cv("2026-09-01")}, "DUE_BEFORE_ISSUE"),
    ],
)
def test_date_rules(overrides: dict, code: str) -> None:  # type: ignore[type-arg]
    assert _codes(_invoice_rules(**overrides)) == [code]


def test_unbalanced_balance_sheet() -> None:
    wire = financial_statement.FinancialStatementWire.model_validate(
        {
            "company_name": cv("Acme"),
            "statement_type": cv("Balance Sheet"),
            "period_start": cv(None, 0.1, None),
            "period_end": cv("2026-06-30"),
            "currency": cv("EUR"),
            "total_assets": cv(1000),
            "total_liabilities": cv(400),
            "total_equity": cv(500),
            "revenue": cv(None, 0.1, None),
            "net_income": cv(None, 0.1, None),
            "line_items": [],
            "document_validity_score": 0.9,
        }
    )
    domain, warnings = financial_statement.to_domain(wire)
    assert domain.statement_type.value == "balance_sheet"  # free text normalised to the enum
    assert warnings == []
    assert _codes(financial_statement.rules(domain)) == ["BALANCE_SHEET_UNBALANCED"]


def test_contract_rules() -> None:
    wire = contract.ContractWire.model_validate(
        {
            "title": cv("MSA"),
            "parties": [{"name": cv("Acme"), "role": cv("Supplier")}],
            "effective_date": cv("2026-05-01"),
            "expiration_date": cv("2025-05-01"),
            "governing_law": cv("England"),
            "contract_value": cv(None, 0.1, None),
            "currency": cv(None, 0.1, None),
            "termination_notice_days": cv(30),
            "auto_renews": cv("YES"),
            "key_clauses": [],
            "document_validity_score": 0.9,
        }
    )
    domain, _ = contract.to_domain(wire)
    assert domain.auto_renews.value == "yes"
    assert sorted(_codes(contract.rules(domain))) == ["TERM_REVERSED", "TOO_FEW_PARTIES"]


def test_length_limit_on_array_of_objects_is_rejected() -> None:
    """Regression: maxItems=50 on generic_document.key_values made Gemini 3.5 Flash-Lite reject the
    schema with HTTP 400 "too many states for serving"."""

    class Item(BaseModel):
        name: str

    class Capped(BaseModel):
        items: list[Item] = Field(max_length=50)

    with pytest.raises(UnsupportedSchemaError, match="length limit on an array of objects"):
        gemini_json_schema(Capped)


def test_generic_document_caps_key_values_in_domain_with_warning() -> None:
    from docintel.schemas import generic_document as gd

    kv = {"key": cv("k"), "value": cv("v")}
    wire = gd.GenericDocumentWire.model_validate(
        {
            "title": cv("T"),
            "document_type": cv("memo"),
            "document_date": cv(None, 0.1, None),
            "parties": [],
            "tables": [],
            "key_values": [kv] * 60,
            "summary": cv("s"),
            "document_validity_score": 0.9,
        }
    )
    domain, warnings = gd.to_domain(wire)
    assert len(domain.key_values) == gd.MAX_KEY_VALUES
    assert [(w.code, w.field) for w in warnings] == [("TRUNCATED", "key_values")]


def test_generic_document_tables_keep_structure_and_flag_ragged_rows() -> None:
    from docintel.schemas import generic_document as gd

    table = {
        "title": "Daily  schedule",
        "columns": ["Time Block", "Deliverable"],
        "rows": [["05:30 \u2013 06:30 AM", "Physical conditioning"], ["06:45 AM"]],
        "confidence": 0.9,
        "page": 2,
    }
    wire = gd.GenericDocumentWire.model_validate(
        {
            "title": cv("Plan"),
            "document_type": cv("study guide"),
            "document_date": cv(None, 0.0, None),
            "parties": [],
            "tables": [table],
            "key_values": [],
            "summary": cv("s"),
            "document_validity_score": 0.9,
        }
    )
    domain, warnings = gd.to_domain(wire)
    [t] = domain.tables
    assert t.title == "Daily schedule"
    assert t.rows[0] == ["05:30 \u2013 06:30 AM", "Physical conditioning"]  # row grouping preserved
    assert [(w.code, w.field) for w in warnings] == [("TABLE_SHAPE_MISMATCH", "tables[0]")]
