"""OpenAPI snapshot: any change to the public contract shows up as a diff in the PR.

Regenerate intentionally with ``UPDATE_SNAPSHOTS=1 pytest tests/contract``.
"""

import json
import os
from pathlib import Path

from docintel.api.main import create_app
from tests.conftest import fake_resources, make_settings

SNAPSHOT = Path(__file__).with_name("openapi.snapshot.json")


def test_openapi_matches_snapshot() -> None:
    schema = create_app(make_settings(version="snapshot"), fake_resources()).openapi()
    rendered = json.dumps(schema, indent=2, sort_keys=True) + "\n"

    if os.environ.get("UPDATE_SNAPSHOTS") == "1" or not SNAPSHOT.exists():
        SNAPSHOT.write_text(rendered, encoding="utf-8")

    assert rendered == SNAPSHOT.read_text(encoding="utf-8"), (
        "OpenAPI contract changed. If intentional, run UPDATE_SNAPSHOTS=1 pytest tests/contract"
    )
