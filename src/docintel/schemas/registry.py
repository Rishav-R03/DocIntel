"""Extraction schema registry.

Phase 2 only needs to validate ``schema_id`` at submit time; Phase 3 adds the wire/domain models,
prompt templates and critical fields to each spec (implementation.md §4.7.1).
"""

from dataclasses import dataclass

from docintel.domain.errors import SchemaNotFoundError


@dataclass(frozen=True)
class SchemaSpec:
    schema_id: str
    description: str
    max_pages: int = 50


_SPECS = (
    SchemaSpec("invoice.v1", "Invoices and bills: parties, dates, totals, line items."),
    SchemaSpec("financial_statement.v1", "Balance sheets, income and cash-flow statements."),
    SchemaSpec("contract.v1", "Contracts: parties, term, key dates and clauses."),
    SchemaSpec("generic_document.v1", "Fallback: title, dates, parties and key-value pairs."),
)

REGISTRY: dict[str, SchemaSpec] = {spec.schema_id: spec for spec in _SPECS}


def get_schema(schema_id: str) -> SchemaSpec:
    try:
        return REGISTRY[schema_id]
    except KeyError:
        known = ", ".join(sorted(REGISTRY))
        raise SchemaNotFoundError(
            f"Unknown schema_id '{schema_id[:64]}'. Known schemas: {known}."
        ) from None
