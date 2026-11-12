"""``GET /api/v1/schemas``: the extraction schemas a client can request (FR-12)."""

from fastapi import APIRouter
from pydantic import BaseModel

from docintel.schemas.registry import REGISTRY

router = APIRouter(prefix="/api/v1/schemas", tags=["schemas"])


class SchemaSummary(BaseModel):
    schema_id: str
    description: str
    max_pages: int
    critical_fields: list[str]


@router.get("")
async def list_schemas() -> list[SchemaSummary]:
    return [
        SchemaSummary(
            schema_id=spec.schema_id,
            description=spec.description,
            max_pages=spec.max_pages,
            critical_fields=list(spec.critical_fields),
        )
        for spec in sorted(REGISTRY.values(), key=lambda s: s.schema_id)
    ]
