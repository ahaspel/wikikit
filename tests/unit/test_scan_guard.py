"""The empty-answer guard: a model answer that is not a reading of its image
(empty, the prompt's placeholder, far too short for the ink) is read once
more, then recorded as unread — never stored as text.  No model is called:
`ask` is replaced."""
import pytest
from PIL import Image, ImageDraw

from wikikit.scan import transcribe
from wikikit.scan.cut import Item
from wikikit.scan.transcribe import Unread, check_reading, plain_length, read_item


def strip_image(lines=12, width=700):
    """A strip of `lines` printed lines, words drawn as blocks."""
    im = Image.new("L", (width, 40 + lines * 30), 255)
    d = ImageDraw.Draw(im)
    for n in range(lines):
        y = 20 + n * 30
        x = 10
        while x < width - 60:
            d.rectangle((x, y, x + 45, y + 18), fill=0)
            x += 58
    return im


STRIP = Item("text", (0, 0, 700, 400), 1, "c1s1")
HEAD = Item("head", (0, 0, 700, 40), 0, "head")
GOOD = " ".join(["word"] * 400)            # a full strip's worth of text


def test_a_real_reading_passes():
    assert check_reading(strip_image(), STRIP, GOOD) is None
    assert check_reading(strip_image(), HEAD, "{{RunningHeader|abacus|4|abannition}}") is None


@pytest.mark.parametrize("item, text, why", [
    (STRIP, "   ", "empty"),
    (HEAD, "{{RunningHeader|left|181|right}}", "placeholder"),
    (HEAD, "{{RunningHeader|abacus|4", "malformed"),
    (STRIP, "¶\n'''abacus''' (ab′a̤-kus), ''n.''", "too short"),
])
def test_what_is_not_a_reading(item, text, why):
    assert why in check_reading(strip_image(), item, text)


def test_the_etymology_sign_is_text_not_markup():
    # "<" is print in a dictionary; only the vocabulary's own tags are markup
    assert plain_length("[< Gr. βαθύς, deep, + μέτρον.] <sup>1</sup>") == len("[< Gr. βαθύς, deep, + μέτρον.] 1")


def test_a_bad_answer_is_read_again_then_recorded(monkeypatch):
    answers = iter(["", GOOD])
    monkeypatch.setattr(transcribe, "ask", lambda *a, **k: next(answers))
    assert read_item(strip_image(), STRIP) == GOOD                  # second answer stands

    monkeypatch.setattr(transcribe, "ask", lambda *a, **k: "")
    with pytest.raises(Unread, match="empty"):
        read_item(strip_image(), STRIP)
