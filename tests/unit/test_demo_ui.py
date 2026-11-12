import re

import pytest
from fastapi.testclient import TestClient

from docintel.api.main import create_app
from docintel.web import STATIC_DIR
from tests.conftest import fake_resources, make_settings


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(make_settings(), fake_resources()))


def test_ui_is_served_with_strict_security_headers(client: TestClient) -> None:
    with client:
        page = client.get("/ui/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert page.headers["x-content-type-options"] == "nosniff"
    assert "X-Request-Id".lower() in page.headers  # still goes through our middleware


def test_root_redirects_to_ui_and_assets_load(client: TestClient) -> None:
    with client:
        root = client.get("/", follow_redirects=False)
        assert (root.status_code, root.headers["location"]) == (307, "/ui/")
        for asset in (
            "app.js",
            "app.css",
            "vendor/bootstrap.min.css",
            "vendor/bootstrap.bundle.min.js",
        ):
            assert client.get(f"/ui/{asset}").status_code == 200, asset


def test_ui_is_not_mounted_outside_local_and_dev() -> None:
    settings = make_settings(
        env="staging", db_ssl_mode="verify-full", s3_kms_key_id="arn:k", gemini_api_key="k"
    )
    with TestClient(create_app(settings, fake_resources())) as client:
        assert client.get("/ui/").status_code == 404
        assert client.get("/").status_code == 404


def test_schemas_endpoint_lists_registry(client: TestClient) -> None:
    with client:
        schemas = client.get("/api/v1/schemas").json()
    assert [s["schema_id"] for s in schemas] == [
        "contract.v1",
        "financial_statement.v1",
        "generic_document.v1",
        "invoice.v1",
    ]
    invoice = schemas[-1]
    assert invoice["max_pages"] == 50
    assert "total_amount" in invoice["critical_fields"]


# ----------------------------------------------------------------------------- static guards
def test_js_never_writes_html_from_data() -> None:
    """Extracted values come from untrusted documents: they must only be set via textContent."""
    source = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    sinks = re.compile(
        r"\.\s*(innerHTML|outerHTML)\b|insertAdjacentHTML\s*\(|document\.write\s*\(|\beval\s*\("
    )
    hits = [m.group(0) for m in sinks.finditer(source)]
    assert hits == [], f"HTML-parsing sinks would let document content become markup: {hits}"


def test_html_is_csp_compatible() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline <script> blocked by CSP"
    assert " style=" not in html, "style attributes are blocked by style-src 'self'"
    assert "http://" not in html, "assets must be same-origin (vendored)"
    assert "https://" not in html, "assets must be same-origin (vendored)"
