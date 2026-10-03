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
import re
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


def ask(img: Image.Image, prompt: str, max_tokens: int = 4000) -> str:
    """The model's text answer about one image."""
    import anthropic
    try:
        msg = _client().messages.create(model=MODEL, max_tokens=max_tokens, messages=[{
            "role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": base64.b64encode(png(img)).decode()}},
                {"type": "text", "text": prompt}]}])
    except anthropic.BadRequestError as e:
        if "content filtering" in str(e):
            raise Refused(str(e)) from e
        raise
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def ask_json(img: Image.Image, prompt: str, max_tokens: int = 2000) -> dict:
    """`ask`, for prompts that answer with one JSON object."""
    text = ask(img, prompt, max_tokens)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"expected a JSON object from the model, got: {text[:200]!r}")
    return json.loads(m.group(0))
