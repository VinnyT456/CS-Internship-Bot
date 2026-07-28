"""Thin wrapper around google-genai for the AI commands.

One lazily-created client (the API key never changes at runtime), a vision
helper that takes the resume PNG plus a prompt, and a text-only helper. Calls
are blocking, so command handlers run them via asyncio.to_thread.
"""

import logging
import os

logger = logging.getLogger("cs_internship_bot")

MODEL = os.getenv("GEMMA_MODEL", "gemma-4-26b-a4b-it")

_client = None


def _get_client():
    global _client
    if _client is None:
        from google import genai

        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _client


def ask_text(prompt: str) -> str | None:
    """Text-only completion. Returns the response text, or None on failure."""
    try:
        resp = _get_client().models.generate_content(model=MODEL, contents=prompt)
        return (resp.text or "").strip() or None
    except Exception:
        logger.exception("Gemma text call failed")
        return None


def ask_with_image(image_bytes: bytes, prompt: str) -> str | None:
    """Vision completion — the resume PNG plus a prompt. Returns text or None."""
    try:
        from google.genai import types

        resp = _get_client().models.generate_content(
            model=MODEL,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                prompt,
            ],
        )
        return (resp.text or "").strip() or None
    except Exception:
        logger.exception("Gemma vision call failed")
        return None
