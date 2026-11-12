"""Per-model request settings that differ across Gemini generations.

Thinking levels are NOT portable: gemini-3.8-flash rejects ``minimal`` but accepts ``low``, while
gemini-3.5-flash-lite documents ``minimal`` (its default, recommended for extraction), ``medium``
and ``high`` but never ``low``. Sending one model's level to another is an HTTP 400, so the level
belongs to the model, not to the schema. Sources: Gemini 3.8 / 3.6 "what's new" pages, 2026-11-12.
"""

DEFAULT_THINKING_LEVEL = "medium"  # documented default across the 3.x Flash family

THINKING_LEVELS: dict[str, str] = {
    "gemini-3.8-flash": "low",  # lowest accepted ("minimal" -> 400); keeps latency down
    "gemini-3.5-flash-lite": "minimal",  # default; Google's recommendation for extraction
}


def thinking_level_for(model: str) -> str:
    return THINKING_LEVELS.get(model, DEFAULT_THINKING_LEVEL)
