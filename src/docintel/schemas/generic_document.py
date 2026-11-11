"""``generic_document.v1``: fallback for documents without a dedicated schema."""

from datetime import date

from pydantic import BaseModel, Field

from docintel.schemas.common import (
    ConfidentDate,
    ConfidentStr,
    Conversion,
    Extracted,
    ExtractionWarning,
    RuleViolation,
    WireModel,
    parse_date,
    parse_text,
)

PROMPT = """\
Extract the general fields defined by the response schema.
- `document_type` is a short noun phrase (e.g. "purchase order", "bank statement").
- `key_values` captures labelled fields as printed (label -> value), at most 50, most
  important first.
- `summary` is at most three sentences and only restates what the document says."""

CRITICAL_FIELDS = ("document_type", "title")


class KeyValueWire(WireModel):
    key: ConfidentStr
    value: ConfidentStr


class GenericDocumentWire(WireModel):
    title: ConfidentStr
    document_type: ConfidentStr
    document_date: ConfidentDate
    parties: list[ConfidentStr] = Field(description="People or organisations named as parties.")
    key_values: list[KeyValueWire] = Field(max_length=50)
    summary: ConfidentStr
    document_validity_score: float = Field(ge=0, le=1)


class KeyValue(BaseModel):
    key: Extracted[str]
    value: Extracted[str]


class GenericDocument(BaseModel):
    title: Extracted[str]
    document_type: Extracted[str]
    document_date: Extracted[date]
    parties: list[Extracted[str]]
    key_values: list[KeyValue]
    summary: Extracted[str]


def to_domain(wire: GenericDocumentWire) -> tuple[GenericDocument, list[ExtractionWarning]]:
    c = Conversion(warnings=[])
    document = GenericDocument(
        title=c.field("title", wire.title, parse_text),
        document_type=c.field("document_type", wire.document_type, parse_text),
        document_date=c.field("document_date", wire.document_date, parse_date),
        parties=[c.field(f"parties[{i}]", p, parse_text) for i, p in enumerate(wire.parties)],
        key_values=[
            KeyValue(
                key=c.field(f"key_values[{i}].key", kv.key, parse_text),
                value=c.field(f"key_values[{i}].value", kv.value, parse_text),
            )
            for i, kv in enumerate(wire.key_values)
        ],
        summary=c.field("summary", wire.summary, parse_text),
    )
    return document, c.warnings


def rules(_: GenericDocument) -> list[RuleViolation]:
    return []  # no cross-field invariants for free-form documents
