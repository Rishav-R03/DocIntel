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
- `tables` captures every table: `columns` is the header row, and each entry of `rows` is one
  table row with its cells in column order, copied as printed ("" for an empty cell).
- `key_values` captures labelled fields that are NOT inside a table (label -> value), at most 50,
  most important first. Never repeat table content here.
- `summary` is at most three sentences and only restates what the document says."""

CRITICAL_FIELDS = ("document_type", "title")
# Limits are enforced here, not as maxItems in the wire schema: an array-length cap on nested
# objects blows up Gemini's constrained-decoding grammar ("too many states for serving", HTTP 400
# on Flash-Lite).
MAX_KEY_VALUES = 50
MAX_TABLE_ROWS = 200


class KeyValueWire(WireModel):
    key: ConfidentStr
    value: ConfidentStr


class TableWire(WireModel):
    """One confidence per table rather than per cell: per-cell wrappers would multiply both
    output tokens and the decoding grammar's size for little extra signal."""

    title: str | None = Field(description="Table caption or heading, or null if none.")
    columns: list[str] = Field(description="Header row, as printed.")
    rows: list[list[str]] = Field(description="Data rows; each row's cells in column order.")
    confidence: float = Field(
        ge=0, le=1, description="Probability the table is transcribed correctly."
    )
    page: int | None = Field(ge=1, description="1-based page where the table starts.")


class GenericDocumentWire(WireModel):
    title: ConfidentStr
    document_type: ConfidentStr
    document_date: ConfidentDate
    parties: list[ConfidentStr] = Field(description="People or organisations named as parties.")
    tables: list[TableWire] = Field(description="Every table in the document, in order.")
    key_values: list[KeyValueWire] = Field(
        description="Labelled fields outside tables, most important first, at most 50."
    )
    summary: ConfidentStr
    document_validity_score: float = Field(ge=0, le=1)


class KeyValue(BaseModel):
    key: Extracted[str]
    value: Extracted[str]


class Table(BaseModel):
    title: str | None
    columns: list[str]
    rows: list[list[str]]
    confidence: float
    page: int | None


class GenericDocument(BaseModel):
    title: Extracted[str]
    document_type: Extracted[str]
    document_date: Extracted[date]
    parties: list[Extracted[str]]
    tables: list[Table]
    key_values: list[KeyValue]
    summary: Extracted[str]


def _table(index: int, wire: TableWire, warnings: list[ExtractionWarning]) -> Table:
    field = f"tables[{index}]"
    width = len(wire.columns)
    ragged = [i for i, row in enumerate(wire.rows) if width and len(row) != width]
    if ragged:
        warnings.append(
            ExtractionWarning(
                code="TABLE_SHAPE_MISMATCH",
                field=field,
                detail=f"{len(ragged)} row(s) don't have {width} cells (first: row {ragged[0]})",
            )
        )
    if len(wire.rows) > MAX_TABLE_ROWS:
        warnings.append(
            ExtractionWarning(
                code="TRUNCATED",
                field=field,
                detail=f"kept the first {MAX_TABLE_ROWS} of {len(wire.rows)} rows",
            )
        )
    return Table(
        title=" ".join(wire.title.split()) if wire.title else None,
        columns=[" ".join(c.split()) for c in wire.columns],
        rows=[[" ".join(cell.split()) for cell in row] for row in wire.rows[:MAX_TABLE_ROWS]],
        confidence=wire.confidence,
        page=wire.page,
    )


def to_domain(wire: GenericDocumentWire) -> tuple[GenericDocument, list[ExtractionWarning]]:
    c = Conversion(warnings=[])
    document = GenericDocument(
        title=c.field("title", wire.title, parse_text),
        document_type=c.field("document_type", wire.document_type, parse_text),
        document_date=c.field("document_date", wire.document_date, parse_date),
        parties=[c.field(f"parties[{i}]", p, parse_text) for i, p in enumerate(wire.parties)],
        tables=[_table(i, t, c.warnings) for i, t in enumerate(wire.tables)],
        key_values=[
            KeyValue(
                key=c.field(f"key_values[{i}].key", kv.key, parse_text),
                value=c.field(f"key_values[{i}].value", kv.value, parse_text),
            )
            for i, kv in enumerate(wire.key_values[:MAX_KEY_VALUES])
        ],
        summary=c.field("summary", wire.summary, parse_text),
    )
    if len(wire.key_values) > MAX_KEY_VALUES:
        c.warnings.append(
            ExtractionWarning(
                code="TRUNCATED",
                field="key_values",
                detail=f"kept the first {MAX_KEY_VALUES} of {len(wire.key_values)} key-value pairs",
            )
        )
    return document, c.warnings


def rules(_: GenericDocument) -> list[RuleViolation]:
    return []  # no cross-field invariants for free-form documents
