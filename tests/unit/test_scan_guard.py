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
    (STRIP, "   ", "empty"),                      # a strip with type in it, read as nothing
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


def test_nothing_read_is_right_for_a_strip_with_nothing_printed():
    blank = Image.new("L", (700, 300), 255)
    ImageDraw.Draw(blank).point([(100, 50), (400, 200)], fill=0)        # dust
    assert check_reading(blank, Item("text", (0, 0, 700, 300), 1, "c1s3"), "") is None


def test_a_rejected_answer_is_kept_for_whoever_settles_it(monkeypatch):
    monkeypatch.setattr(transcribe, "ask", lambda *a, **k: "far too little")
    with pytest.raises(Unread) as e:
        read_item(strip_image(), STRIP)
    assert e.value.reason.startswith("too short") and e.value.answer == "far too little"


@pytest.mark.parametrize("scale", [1, 2])
def test_the_guard_does_not_depend_on_the_scans_resolution(scale):
    """The same strip scanned at twice the resolution: a complete reading
    passes and a fragment fails, both times (a pixel measure made a 2482-px
    scan's complete readings fail)."""
    im = strip_image(lines=8)
    im = im.resize((im.width * scale, im.height * scale))
    item = Item("text", (0, 0, im.width, im.height), 1, "c1s1")
    complete = " ".join(["word"] * 80)                 # 8 lines of ~10 words
    assert check_reading(im, item, complete) is None
    assert "too short" in check_reading(im, item, "word word word")


def test_a_description_of_a_blank_strip_is_an_empty_reading():
    blank = Image.new("L", (700, 120), 255)
    item = Item("text", (0, 0, 700, 120), 1, "c2s3")
    said = "The strip is blank: there is no printed text to transcribe."
    assert transcribe.settle(blank, item, said) == ("", None)


def test_a_description_where_type_is_printed_is_no_reading():
    said = "The strip appears to be blank: there is no printed text in it to transcribe."
    text, why = transcribe.settle(strip_image(), STRIP, said)
    assert why == "a description of the image, not a reading"
    assert transcribe.settle(strip_image(), STRIP, GOOD) == (GOOD, None)
