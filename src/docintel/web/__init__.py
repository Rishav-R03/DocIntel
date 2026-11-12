"""Minimal demo UI (static HTML/CSS/JS + vendored Bootstrap 5.3.3), served by the API at ``/ui``.

Same origin as the API, so no CORS configuration is needed. Mounted only in ``local``/``dev``:
the product is a headless service (PRD §4), and production exposes no browser surface.

The page renders model output, which is derived from *untrusted documents*. The JS only ever
writes it with ``textContent`` (never ``innerHTML``), and the strict Content-Security-Policy below
blocks inline/third-party script as a second layer: a PDF containing ``<script>`` text stays text.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

STATIC_DIR = Path(__file__).parent / "static"

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",  # no inline scripts, no CDNs
        "style-src 'self'",
        "img-src 'self' data: blob:",  # Bootstrap's inline SVG icons; image preview
        "frame-src blob:",  # PDF preview of the locally selected file
        "object-src 'none'",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

_SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class _SecureStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers.update(_SECURITY_HEADERS)
        return response


def mount_demo_ui(app: FastAPI) -> None:
    app.mount("/ui", _SecureStaticFiles(directory=STATIC_DIR, html=True), name="ui")

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/ui/")
