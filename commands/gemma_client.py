"""Thin wrapper around google-genai for the AI commands.

One lazily-created client (the API key never changes at runtime), a vision
helper that takes the resume PNG plus a prompt, and a text-only helper. Calls
are blocking, so command handlers run them via asyncio.to_thread.

Reliability: the free Gemma tier intermittently returns 500/503 under load. Every
call goes through _generate(), which retries with backoff and, if the primary
model keeps failing, ROTATES to a fallback model so a busy primary doesn't kill
the request.
"""

import logging
import os
import time

logger = logging.getLogger("cs_internship_bot")

# Primary model, then a fallback to rotate to when the primary is overloaded.
MODEL = os.getenv("GEMMA_MODEL", "gemma-4-26b-a4b-it")
FALLBACK_MODEL = os.getenv("GEMMA_FALLBACK_MODEL", "gemma-4-31b-it")

# Models to try in order. Each is attempted with a couple of retries before
# rotating to the next.
_MODEL_CHAIN = [m for m in (MODEL, FALLBACK_MODEL) if m]

_RETRIES_PER_MODEL = int(os.getenv("GEMMA_RETRIES", "2"))
_RETRY_BASE_DELAY = float(os.getenv("GEMMA_RETRY_DELAY", "1.5"))

# HTTP statuses worth retrying / rotating on (transient overload / server error).
_TRANSIENT_STATUSES = {429, 500, 502, 503, 504}

_client = None


def _get_client():
    global _client
    if _client is None:
        from google import genai

        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _client


def _status_of(exc):
    """Best-effort HTTP status code from a google-genai error."""
    for attr in ("code", "status_code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    return None


def _generate(contents, config=None):
    """Call generate_content with retry + model rotation. Returns the response,
    or raises the last error if every model/attempt fails.

    Order: for each model in the chain, try up to _RETRIES_PER_MODEL times with
    exponential backoff on transient (429/5xx) errors; a non-transient error
    aborts immediately."""
    client = _get_client()
    last_exc = None
    for model in _MODEL_CHAIN:
        for attempt in range(_RETRIES_PER_MODEL):
            try:
                return client.models.generate_content(
                    model=model, contents=contents, config=config
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                status = _status_of(exc)
                transient = status in _TRANSIENT_STATUSES
                if not transient:
                    # Real error (bad request, auth, etc.) — don't waste retries.
                    raise
                # Backoff before the next attempt on this model.
                if attempt < _RETRIES_PER_MODEL - 1:
                    delay = _RETRY_BASE_DELAY * (2**attempt)
                    logger.warning(
                        "Gemma %s transient %s — retry %d/%d in %.1fs",
                        model, status, attempt + 1, _RETRIES_PER_MODEL, delay,
                    )
                    time.sleep(delay)
        logger.warning("Gemma %s exhausted retries — rotating model", model)
    if last_exc:
        raise last_exc
    raise RuntimeError("No Gemma model available")


def ask_text(prompt: str) -> str | None:
    """Text-only completion. Returns the response text, or None on failure."""
    try:
        resp = _generate(prompt)
        return (resp.text or "").strip() or None
    except Exception:
        logger.exception("Gemma text call failed")
        return None


def ask_json_text(
    prompt: str,
    max_output_tokens: int = 4000,
    temperature: float = 0.2,
) -> dict | None:
    """Text-only completion constrained to strict JSON. Faster than the vision
    path — use when the resume is already available as text. Returns the parsed
    dict (tolerating truncation), or None."""
    try:
        from google.genai import types

        resp = _generate(
            prompt,
            types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            ),
        )
        text = (resp.text or "").strip()
        if not text and resp.candidates:
            parts = getattr(resp.candidates[0].content, "parts", None) or []
            text = "".join(getattr(p, "text", "") or "" for p in parts).strip()
        if not text:
            return None
        parsed = _loads_lenient(text)
        if parsed is None:
            logger.warning("Gemma JSON (text) reply was not valid JSON")
        return parsed
    except Exception:
        logger.exception("Gemma JSON text call failed")
        return None


def warm_up() -> None:
    """Pre-initialize the client so the first real call isn't cold. Safe to call
    on startup; failures are swallowed."""
    try:
        _get_client()
    except Exception:
        logger.exception("Gemma client warm-up failed")


def ask_with_image(image_bytes: bytes, prompt: str) -> str | None:
    """Vision completion — the resume PNG plus a prompt. Returns text or None."""
    try:
        from google.genai import types

        resp = _generate(
            [
                types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                prompt,
            ]
        )
        return (resp.text or "").strip() or None
    except Exception:
        logger.exception("Gemma vision call failed")
        return None


def ask_json_with_image(
    image_bytes: bytes,
    prompt: str,
    max_output_tokens: int = 4000,
    temperature: float = 0.2,
) -> dict | None:
    """Vision completion constrained to strict JSON — faster and directly
    parseable (no regex scraping). Gemma has no 'thinking' mode to disable;
    speed comes from the JSON constraint, a low temperature, and a token cap.
    Returns the parsed dict, or None on failure / bad JSON."""
    try:
        import json

        from google.genai import types

        resp = _generate(
            [
                types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                prompt,
            ],
            types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            ),
        )
        text = (resp.text or "").strip()
        # On MAX_TOKENS the .text accessor can come back empty even though the
        # candidate holds partial content — pull it straight from the parts.
        if not text and resp.candidates:
            parts = getattr(resp.candidates[0].content, "parts", None) or []
            text = "".join(getattr(p, "text", "") or "" for p in parts).strip()

        if not text:
            fr = resp.candidates[0].finish_reason if resp.candidates else None
            logger.warning("Gemma JSON reply empty (finish_reason=%s)", fr)
            return None

        parsed = _loads_lenient(text)
        if parsed is None:
            logger.warning("Gemma JSON reply was not valid JSON")
        return parsed
    except Exception:
        logger.exception("Gemma JSON vision call failed")
        return None


def _loads_lenient(text: str):
    """Parse JSON, tolerating a reply truncated at the token cap by closing any
    unbalanced braces/brackets. Returns dict/list or None."""
    import json

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Trim to the last complete top-level close first.
    end = text.rfind("}")
    if end != -1:
        try:
            return json.loads(text[: end + 1])
        except json.JSONDecodeError:
            pass
    # Rebalance: drop the trailing partial token, then close open structures.
    s = text
    # An odd number of quotes means a string is still open — cut it off.
    if s.count('"') % 2 == 1:
        s = s[: s.rfind('"')]
    s = s.rstrip()
    # Peel back any dangling separators / an incomplete `"key":` with no value.
    changed = True
    while changed and s:
        changed = False
        stripped = s.rstrip().rstrip(",").rstrip(":").rstrip()
        if stripped != s.rstrip():
            s = stripped
            changed = True
        # A key with no value: ...,"foo"  or  {"foo"  -> drop the key too.
        if s.endswith('"'):
            open_q = s.rfind('"', 0, len(s) - 1)
            if open_q != -1:
                s = s[:open_q].rstrip()
                changed = True
    opens = []
    in_str = False
    esc = False
    for ch in s:
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            opens.append(ch)
        elif ch == "}" and opens and opens[-1] == "{":
            opens.pop()
        elif ch == "]" and opens and opens[-1] == "[":
            opens.pop()
    s = s.rstrip().rstrip(",")
    s += "".join("}" if c == "{" else "]" for c in reversed(opens))
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None
