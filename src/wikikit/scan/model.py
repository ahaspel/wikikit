"""The one way this engine asks a vision model about an image.

Every call is an image and a prompt in, text out.  A call the API refuses
(its content filter blocks some pages of twentieth-century text) raises
`Refused` — a result to record against the page, never one to skip past.

Needs the optional `scan` extra (`wikikit[scan]`, the Anthropic SDK) and
`ANTHROPIC_API_KEY` in the book's private `.env`.
"""
from __future__ import annotations

import base64
import io
import json
from functools import lru_cache

from PIL import Image

from wikikit.settings import settings

#: The model every scan call uses: the most capable, because a misread mark
#: costs a proofreader's time and the calls cost cents.
MODEL = "claude-opus-5-5"


class Refused(Exception):
    """The API declined to answer for this image (e.g. its content filter)."""


@lru_cache(maxsize=1)
def _client():
    try:
        import anthropic
    except ImportError as e:      # the engine works without it for every other book
        raise RuntimeError("wikikit.scan needs the Anthropic SDK: depend on wikikit[scan]") from e
    key = settings.anthropic_api_key
    if key is None:
        raise RuntimeError("no Anthropic API key: set ANTHROPIC_API_KEY in the book's .env")
    return anthropic.Anthropic(api_key=key.get_secret_value())


def png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


#: What the model is billed by, as the API reports it on every call.
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


class Meter:
    """Token counts of every call made while it is open (`metered`): the
    figures a page's cost is computed from, recorded in its provenance."""

    def __init__(self):
        self.calls = 0
        self.tokens = dict.fromkeys(USAGE_FIELDS, 0)

    def add(self, usage) -> None:
        self.calls += 1
        for f in USAGE_FIELDS:
            self.tokens[f] += getattr(usage, f, 0) or 0

    def record(self) -> dict:
        # read now, at the standard rate; a batch's record says True
        return {"model": MODEL, "batch": False, "calls": self.calls, **self.tokens}


_meters: list[Meter] = []


class metered:
    """`with metered() as m:` — every model call inside is counted in `m`."""

    def __enter__(self) -> Meter:
        _meters.append(Meter())
        return _meters[-1]

    def __exit__(self, *exc) -> None:
        _meters.pop()


def request(img: Image.Image, prompt: str, max_tokens: int = 4000,
            instructions: str | None = None, cache_ttl: str | None = None) -> dict:
    """The parameters of one call — the same whether it is made now (`ask`)
    or in a batch (`submit`), so a batched call is the call `ask` would make.

    `instructions` are the fixed part of a prompt — the same for every strip of
    every page (rules, vocabulary, the book's notation: two-thirds of a strip
    call's input, measured 2026-10-03).  They go in the system prompt, marked
    for caching; `prompt` is the short part that follows the image.
    `cache_ttl` "1h" keeps them cached through a batch's own pace."""
    params = {"model": MODEL, "max_tokens": max_tokens, "messages": [{
        "role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.b64encode(png(img)).decode()}},
            {"type": "text", "text": prompt}]}]}
    if instructions:
        cache = {"type": "ephemeral", **({"ttl": cache_ttl} if cache_ttl else {})}
        params["system"] = [{"type": "text", "text": instructions, "cache_control": cache}]
    return params


def _text(msg) -> str:
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def ask(img: Image.Image, prompt: str, max_tokens: int = 4000, instructions: str | None = None) -> str:
    """The model's text answer about one image, now (`request` for the call)."""
    import anthropic
    try:
        msg = _client().messages.create(**request(img, prompt, max_tokens, instructions))
    except anthropic.BadRequestError as e:
        if "content filtering" in str(e):
            raise Refused(str(e)) from e
        raise
    for m in _meters:
        m.add(msg.usage)
    return _text(msg)


def parse_json(text: str) -> dict:
    """The JSON object a JSON-answering prompt returns — the LAST complete one
    when there are several: the model sometimes answers, then corrects itself
    in words and answers again ("Wait, ... Corrected: {...}", 1 of 405 EB1911
    figure answers), and the correction is its answer."""
    decoder, found, i = json.JSONDecoder(), None, text.find("{")
    while i != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            found = obj
        i = text.find("{", end)
    if found is None:
        raise ValueError(f"expected a JSON object from the model, got: {text[:200]!r}")
    return found


def ask_json(img: Image.Image, prompt: str, max_tokens: int = 2000) -> dict:
    """`ask`, for prompts that answer with one JSON object."""
    return parse_json(ask(img, prompt, max_tokens))


# ── the Batch API: the same calls, run when Anthropic has capacity, at half
#    the price of every token (input and output); results within 24 hours ──
def submit(requests: dict[str, dict]) -> str:
    """One batch of calls, keyed by our own ids (letters, digits, - and _)."""
    batch = _client().messages.batches.create(
        requests=[{"custom_id": cid, "params": params} for cid, params in requests.items()])
    return batch.id


def batch_status(batch_id: str) -> tuple[str, dict]:
    """("in_progress" | "canceling" | "ended", request counts)."""
    b = _client().messages.batches.retrieve(batch_id)
    return b.processing_status, b.request_counts.model_dump()


def collect(batch_id: str) -> dict[str, tuple[str, str, object]]:
    """An ended batch's outcomes: id -> ("ok", text, usage) | ("refused", why, None)
    | ("failed", why, None).  A refusal is recognised as `ask` recognises it."""
    out = {}
    for r in _client().messages.batches.results(batch_id):
        res = r.result
        if res.type == "succeeded":
            out[r.custom_id] = ("ok", _text(res.message), res.message.usage)
        elif res.type == "errored" and "content filtering" in str(res.error):
            out[r.custom_id] = ("refused", str(res.error), None)
        else:
            out[r.custom_id] = ("failed", f"{res.type}: {getattr(res, 'error', '')}", None)
    return out
