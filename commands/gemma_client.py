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

# Two model tiers routed by task importance:
#   SMART — the "important" evaluative work (match scoring, review). Gemma leads,
#           because we want its judgment; a Flash model backs it up when Gemma
#           is overloaded so the command still returns.
#   FAST  — mechanical/heavy work (extraction, structuring, bullet rewriting,
#           YAML). Flash-lite leads (dramatically faster + steadier than the
#           Gemma free tier); Gemma backs it up.
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "gemma-4-26b-a4b-it")
GEMMA_MODEL_2 = os.getenv("GEMMA_FALLBACK_MODEL", "gemma-4-31b-it")
FLASH_MODEL = os.getenv("FLASH_MODEL", "gemini-flash-lite-latest")
FLASH_MODEL_2 = os.getenv("FLASH_MODEL_2", "gemini-3.1-flash-lite")

def _chain(*models):
    return list(dict.fromkeys([m for m in models if m]))

SMART_CHAIN = _chain(GEMMA_MODEL, GEMMA_MODEL_2, FLASH_MODEL)
FAST_CHAIN = _chain(FLASH_MODEL, FLASH_MODEL_2, GEMMA_MODEL)

# EXTRACT — pure structured extraction (GitHub project scan). Pinned to
# gemini-3.1-flash-lite: it has by far the highest free-tier RPD (500) of the
# text models, ample TPM (250K), and native JSON output. Thinking is disabled
# for these calls (see ask_json_text thinking=False) — the task is extraction,
# not reasoning, so tokens/latency spent "thinking" are wasted. Flash-lite-latest
# backs it up if the pinned id is unavailable.
EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", "gemini-3.1-flash-lite")
EXTRACT_CHAIN = _chain(EXTRACT_MODEL, FLASH_MODEL, FLASH_MODEL_2)

# LITE — pure Flash-lite, no Gemma fallback. For light interactive turns (e.g.
# /leetcode learn follow-up Q&A) where speed matters and Gemma's slower free tier
# isn't wanted. Both entries are flash-lite variants for redundancy.
LITE_CHAIN = _chain(FLASH_MODEL, FLASH_MODEL_2)

# Back-compat: the old single-model default (used where no tier is passed).
MODEL = GEMMA_MODEL
_MODEL_CHAIN = SMART_CHAIN

_RETRIES_PER_MODEL = int(os.getenv("GEMMA_RETRIES", "2"))
_RETRY_BASE_DELAY = float(os.getenv("GEMMA_RETRY_DELAY", "1.5"))

# HTTP statuses worth retrying / rotating on (transient overload / server error).
_TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
# Exception type names that mean a transient network drop (retry-worthy).
_TRANSIENT_EXC_NAMES = {
    "RemoteProtocolError", "ReadTimeout", "ConnectTimeout", "ConnectError",
    "ReadError", "WriteError", "PoolTimeout", "ServerError",
}

# --- API key pool (Tier 2: a second key doubles the real per-minute quota) ---
# Each Gemini free-tier key is a SEPARATE project quota, so spreading calls across
# N keys multiplies the ceiling by N. Provide extra keys via GEMINI_API_KEYS
# (comma-separated); GEMINI_API_KEY stays supported for a single key. Calls
# round-robin across keys, and on a 429 the retry rotates to the NEXT key (not
# just the next model) so a throttled key doesn't sink the request.
def _load_api_keys():
    raw = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY") or ""
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return keys or [None]  # [None] lets genai fall back to its own env lookup


_API_KEYS = _load_api_keys()
_clients = {}  # key-string -> genai.Client (lazy, one per key)
import itertools  # noqa: E402

_key_cycle = itertools.cycle(range(len(_API_KEYS)))


def _client_for(idx):
    """The genai client for key index `idx` (lazily built, cached)."""
    key = _API_KEYS[idx % len(_API_KEYS)]
    if key not in _clients:
        from google import genai

        _clients[key] = genai.Client(api_key=key) if key else genai.Client()
    return _clients[key]


def _get_client():
    """Next client in the round-robin. Kept for callers/tests that want a client;
    _generate does its own per-attempt key rotation."""
    return _client_for(next(_key_cycle))


def key_count():
    """How many API keys are configured (for logging/diagnostics)."""
    return len(_API_KEYS)


def _status_of(exc):
    """Best-effort HTTP status code from a google-genai error."""
    for attr in ("code", "status_code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    return None


# Concurrency cap (Tier 1): bound simultaneous in-flight API calls so a burst
# (many users, or Smart-Alert scoring) becomes a smooth queue that stays under the
# per-minute limit instead of firing all at once and tripping 429s. Calls are
# blocking and run in threads (to_thread), so a threading.Semaphore is the right
# primitive. Default 4 — comfortably parallel, well under free-tier burst limits.
import threading  # noqa: E402

_MAX_CONCURRENCY = int(os.getenv("GEMINI_MAX_CONCURRENCY", "4"))
_inflight = threading.BoundedSemaphore(_MAX_CONCURRENCY)


def _generate(contents, config=None, chain=None):
    """Call generate_content with a concurrency cap, retry, and model+KEY rotation.
    Returns the response, or raises the last error if every model/key/attempt fails.

    Order: bounded by the in-flight semaphore, then for each model in `chain`
    (defaults to SMART_CHAIN), try up to _RETRIES_PER_MODEL times with exponential
    backoff on transient (429/5xx) errors — EACH attempt uses the next API key in
    the round-robin, so a throttled key is skipped rather than retried. A
    non-transient error aborts immediately."""
    last_exc = None
    with _inflight:  # cap concurrent calls; a burst queues here instead of 429-ing
        for model in (chain or SMART_CHAIN):
            for attempt in range(_RETRIES_PER_MODEL):
                # Rotate the key every attempt — spreads load and skips a throttled
                # key on retry (the main lever when multiple keys are configured).
                client = _client_for(next(_key_cycle))
                try:
                    return client.models.generate_content(
                        model=model, contents=contents, config=config
                    )
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    status = _status_of(exc)
                    exc_name = type(exc).__name__
                    transient = (
                        status in _TRANSIENT_STATUSES
                        or exc_name in _TRANSIENT_EXC_NAMES
                    )
                    if not transient:
                        raise  # real error (bad request, auth) — don't burn retries
                    if attempt < _RETRIES_PER_MODEL - 1:
                        delay = _RETRY_BASE_DELAY * (2**attempt)
                        logger.warning(
                            "Gemma %s transient %s — retry %d/%d in %.1fs (keys=%d)",
                            model, status, attempt + 1, _RETRIES_PER_MODEL, delay,
                            len(_API_KEYS),
                        )
                        time.sleep(delay)
            logger.warning("Gemma %s exhausted retries — rotating model", model)
    if last_exc:
        raise last_exc
    raise RuntimeError("No Gemma model available")


def ask_text(prompt: str, chain=None) -> str | None:
    """Text-only completion. Returns the response text, or None on failure."""
    try:
        resp = _generate(prompt, chain=chain)
        return (resp.text or "").strip() or None
    except Exception:
        logger.exception("Gemma text call failed")
        return None


def ask_json_text(
    prompt: str,
    max_output_tokens: int = 4000,
    temperature: float = 0.2,
    chain=None,
    thinking: bool = True,
) -> dict | None:
    """Text-only completion constrained to strict JSON. Faster than the vision
    path — use when the resume is already available as text. Returns the parsed
    dict (tolerating truncation), or None.

    thinking=False disables the model's thinking budget (thinking_budget=0) —
    use for pure extraction where reasoning tokens are wasted latency. ONLY safe on
    Flash/Flash-lite chains: Gemma models REJECT a thinking budget with a 400
    ('Thinking budget is not supported for this model'), so never pass thinking=False
    on SMART_CHAIN (Gemma-led). Use FAST_CHAIN/LITE_CHAIN if you need it."""
    try:
        from google.genai import types

        cfg_kwargs = dict(
            response_mime_type="application/json",
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        if not thinking:
            try:
                cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
            except Exception:
                pass  # older genai without ThinkingConfig — proceed without it

        resp = _generate(
            prompt,
            types.GenerateContentConfig(**cfg_kwargs),
            chain=chain,
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


def ask_with_image(image_bytes: bytes, prompt: str, chain=None) -> str | None:
    """Vision completion — the resume PNG plus a prompt. Returns text or None."""
    try:
        from google.genai import types

        resp = _generate(
            [
                types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                prompt,
            ],
            chain=chain,
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
    chain=None,
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
            chain=chain,
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
    """Parse JSON, tolerating (a) a ```json ... ``` markdown fence — Gemma wraps
    its JSON in one even when asked not to — and (b) a reply truncated at the token
    cap, by closing any unbalanced braces/brackets. Returns dict/list or None."""
    import json
    import re

    text = (text or "").strip()
    # Strip a leading ```json / ``` fence and any trailing ``` — Gemma adds these
    # despite response_mime_type=application/json. Keep only what's inside.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
        text = text.strip()

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
