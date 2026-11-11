"""Live smoke test against the real Gemini API (Phase 3 exit criteria).

Renders a synthetic invoice with known ground truth (no customer data), runs the production
extraction pipeline N times, and reports field accuracy, validity, latency, tokens and cost.

    docker compose run --rm --no-deps test python scripts/live_smoke.py --runs 3

Requires DOCINTEL_GEMINI_API_KEY (loaded from the git-ignored .env by compose). Costs ~$0.01/run.
"""

import argparse
import asyncio
import io
import statistics
import sys
import time

from google import genai
from PIL import Image, ImageDraw, ImageFont

from docintel.api.resources import build_pipeline
from docintel.extraction.pipeline import Attempt
from docintel.logging import configure_logging
from docintel.schemas.registry import get_schema
from docintel.settings import Settings

EXPECTED = {
    "invoice_number": "INV-2026-0417",
    "invoice_date": "2026-10-14",
    "due_date": "2026-11-13",
    "currency": "EUR",
    "vendor_name": "Nordwind Logistics GmbH",
    "customer_name": "Helios Retail BV",
    "subtotal": "1250.00",
    "tax_amount": "237.50",
    "total_amount": "1487.50",
}

_LINES = [
    ("Nordwind Logistics GmbH", 36),
    ("Hafenstrasse 12, 20457 Hamburg  -  USt-IdNr. DE298374651", 20),
    ("", 20),
    ("INVOICE", 44),
    ("Invoice no.: INV-2026-0417", 24),
    ("Invoice date: 14 October 2026", 24),
    ("Due date: 13 November 2026", 24),
    ("", 20),
    ("Bill to: Helios Retail BV, Keizersgracht 88, Amsterdam", 22),
    ("", 20),
    ("Description                        Qty     Unit price      Amount", 22),
    ("Pallet transport HAM-AMS              5       EUR 200.00    EUR 1,000.00", 22),
    ("Customs documentation                 1       EUR 250.00      EUR 250.00", 22),
    ("", 20),
    ("Subtotal                                                EUR 1,250.00", 24),
    ("VAT 19%                                                   EUR 237.50", 24),
    ("TOTAL DUE                                               EUR 1,487.50", 30),
]


def render_invoice() -> bytes:
    image = Image.new("RGB", (1240, 1754), "white")  # A4 @ 150 dpi
    draw = ImageDraw.Draw(image)
    y = 80
    for text, size in _LINES:
        draw.text((80, y), text, fill="black", font=ImageFont.load_default(size=size))
        y += int(size * 1.9)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


async def main(runs: int) -> int:
    # Gemini settings come from the environment like the service; DB/AWS aren't used here.
    placeholders = {"db_password": "unused", "s3_bucket": "unused", "sqs_jobs_url": "unused"}
    settings = Settings(**placeholders)
    if settings.gemini_api_key is None:
        print("DOCINTEL_GEMINI_API_KEY is not set (add it to .env).", file=sys.stderr)
        return 2
    configure_logging(settings.model_copy(update={"log_level": "WARNING"}))
    client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
    pipeline = build_pipeline(settings, client)
    spec, document = get_schema("invoice.v1"), render_invoice()
    print(f"model={settings.gemini_model} schema={spec.schema_id} document={len(document):,} B PNG")

    latencies: list[int] = []
    try:
        for run in range(1, runs + 1):
            attempts: list[Attempt] = []
            start = time.perf_counter()
            outcome = await pipeline.run(
                document=document,
                mime_type="image/png",
                spec=spec,
                deadline=time.monotonic() + settings.sync_deadline_s,
                attempts=attempts,
            )
            latencies.append(int((time.perf_counter() - start) * 1000))
            correct = {f: outcome.data[f]["value"] == want for f, want in EXPECTED.items()}
            print(
                f"run {run}: {latencies[-1]:,} ms | fields {sum(correct.values())}/{len(correct)}"
                f" | validity {outcome.validity.adjusted} (model {outcome.validity.model_reported})"
                f" | tokens in={outcome.input_tokens} out={outcome.output_tokens}"
                f" think={outcome.thought_tokens} | ${outcome.cost_usd}"
                f" | attempts={[a.outcome for a in attempts]}"
                f" | prompt={attempts[-1].prompt_version}"
            )
            for f, ok in correct.items():
                if not ok:
                    print(
                        f"   MISMATCH {f}: got {outcome.data[f]['value']!r}, want {EXPECTED[f]!r}"
                    )
            if outcome.warnings:
                print(f"   warnings: {[w.code for w in outcome.warnings]}")
            line_items = [li["amount"]["value"] for li in outcome.data["line_items"]]
            print(f"   line items: {line_items}")
    finally:
        await client.aio.aclose()
    print(
        f"latency ms: min={min(latencies):,} median={int(statistics.median(latencies)):,} "
        f"max={max(latencies):,} (PRD target: 3-page PDF < 4,000 ms p95)"
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    sys.exit(asyncio.run(main(parser.parse_args().runs)))
