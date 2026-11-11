"""Extraction schema registry (implementation.md §4.7.1).

Each spec bundles everything one document type needs: the wire model sent to Gemini, the
wire -> domain conversion, business rules, prompt and critical fields. The JSON Schema sent to
Gemini is derived from the wire model and checked against the keywords Gemini's structured-output
mode supports when the module is imported, so an unsupported construct fails at startup and in CI,
never on a live request.
"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from pydantic import BaseModel

from docintel.domain.errors import SchemaNotFoundError
from docintel.schemas import contract, financial_statement, generic_document, invoice
from docintel.schemas.common import ExtractionWarning, RuleViolation

# Gemini structured output (JSON Schema subset). Keywords Gemini documents as "ignored"
# (minLength, maxLength, uniqueItems, ...) are rejected too: an ignored constraint is a constraint
# we would wrongly believe is enforced.
SUPPORTED_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "prefixItems",
        "minItems",
        "maxItems",
        "enum",
        "format",
        "minimum",
        "maximum",
        "anyOf",
        "$ref",
        "$defs",
        "description",
    }
)
SUPPORTED_FORMATS = frozenset({"date-time", "date", "time"})
_STRIPPED_KEYWORDS = frozenset({"title", "default"})  # descriptive / irrelevant to the model


class UnsupportedSchemaError(ValueError):
    pass


def _clean(node: Any, path: str) -> Any:
    if isinstance(node, list):
        return [_clean(item, f"{path}[{i}]") for i, item in enumerate(node)]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _STRIPPED_KEYWORDS:
            continue
        if key in ("properties", "$defs"):  # maps of name -> schema: names aren't keywords
            out[key] = {name: _clean(sub, f"{path}.{key}.{name}") for name, sub in value.items()}
            continue
        if key not in SUPPORTED_KEYWORDS:
            raise UnsupportedSchemaError(f"unsupported JSON Schema keyword {key!r} at {path}")
        if key == "format" and value not in SUPPORTED_FORMATS:
            raise UnsupportedSchemaError(f"unsupported format {value!r} at {path}")
        out[key] = _clean(value, f"{path}.{key}")
    return out


def gemini_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    cleaned: dict[str, Any] = _clean(model.model_json_schema(), model.__name__)
    return cleaned


@dataclass(frozen=True)
class SchemaSpec:
    schema_id: str
    description: str
    wire_model: type[BaseModel]
    to_domain: Callable[[Any], tuple[BaseModel, list[ExtractionWarning]]]
    rules: Callable[[Any], list[RuleViolation]]
    prompt: str
    critical_fields: tuple[str, ...]
    max_pages: int = 50
    thinking_level: str = "low"  # lowest level gemini-3.8-flash accepts ("minimal" errors)
    max_output_tokens: int = 16_384

    @cached_property
    def json_schema(self) -> dict[str, Any]:
        return gemini_json_schema(self.wire_model)


_SPECS = (
    SchemaSpec(
        "invoice.v1",
        "Invoices and bills: parties, dates, totals, line items.",
        invoice.InvoiceWire,
        invoice.to_domain,
        invoice.rules,
        invoice.PROMPT,
        invoice.CRITICAL_FIELDS,
    ),
    SchemaSpec(
        "financial_statement.v1",
        "Balance sheets, income and cash-flow statements.",
        financial_statement.FinancialStatementWire,
        financial_statement.to_domain,
        financial_statement.rules,
        financial_statement.PROMPT,
        financial_statement.CRITICAL_FIELDS,
    ),
    SchemaSpec(
        "contract.v1",
        "Contracts: parties, term, key dates and clauses.",
        contract.ContractWire,
        contract.to_domain,
        contract.rules,
        contract.PROMPT,
        contract.CRITICAL_FIELDS,
    ),
    SchemaSpec(
        "generic_document.v1",
        "Fallback: title, dates, parties and key-value pairs.",
        generic_document.GenericDocumentWire,
        generic_document.to_domain,
        generic_document.rules,
        generic_document.PROMPT,
        generic_document.CRITICAL_FIELDS,
    ),
)

REGISTRY: dict[str, SchemaSpec] = {spec.schema_id: spec for spec in _SPECS}

for _spec in _SPECS:  # fail fast at import (startup + every test run)
    _ = _spec.json_schema


def get_schema(schema_id: str) -> SchemaSpec:
    try:
        return REGISTRY[schema_id]
    except KeyError:
        known = ", ".join(sorted(REGISTRY))
        raise SchemaNotFoundError(
            f"Unknown schema_id '{schema_id[:64]}'. Known schemas: {known}."
        ) from None
