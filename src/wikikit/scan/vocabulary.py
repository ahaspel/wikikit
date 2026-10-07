"""The markup a transcribed page may use: standard Wikisource markup the
engine's producers already read, and nothing else.

ONE list serves three readers, so they cannot drift apart:
- the transcriber's prompt states it (`prompt_text`);
- the proofreading desk renders it from a table the engine measures by running
  each construct through its own chain — preprocess, element walk, inline
  render — not from a second renderer's guesses (`render_table`);
- the parity gate checks the desk's rendering against the engine's.

A construct the engine does not know is a transcription error to correct, never
a reason to grow a producer.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Construct:
    name: str
    open: str
    close: str
    says: str            # what it is for, as the prompt states it


VOCABULARY: tuple[Construct, ...] = (
    Construct("bold", "'''", "'''", "bold"),
    Construct("italic", "''", "''", "italic"),
    Construct("sc", "{{sc|", "}}", "small capitals"),
    Construct("sup", "<sup>", "</sup>", "superscript (a homograph number: bating<sup>1</sup>)"),
    Construct("smaller", "{{smaller|", "}}", "text in the smaller type, inside a paragraph"),
    Construct("smaller block", "{{smaller block|", "}}",
              "a quotation or note set off in smaller type on its own lines"),
    Construct("right", "{{right|", "}}",
              "right-aligned on its own line, e.g. a quotation's source inside its block"),
    Construct("c", "{{c|", "}}", "centred, e.g. a figure's caption"),
    Construct("sub", "<sub>", "</sub>", "subscript, in running text that is not a formula"),
    Construct("block center", "{{block center|", "}}",
              "a display formula set apart on its own line, centred: one display, one block"),
    Construct("math", "<math>", "</math>",
              "a mathematical formula or symbol, in LaTeX, inline or inside a display"),
)

#: Constructs the engine's chain lifts out whole for the reader to typeset
#: (KaTeX in the viewer and on the desk): their content never reaches the
#: inline render, which shows a stand-in.
TYPESET = frozenset({"math"})
#: The HTML among the constructs: the only HTML the prompt allows.
_HTML = [c.open for c in VOCABULARY if c.open.startswith("<")]

#: A blank line between paragraphs; the only structure besides the constructs.
PARAGRAPH = "\n\n"


def prompt_text() -> str:
    """The vocabulary as the transcriber's prompt states it."""
    lines = [f"- {c.says}: {c.open}text{c.close}" for c in VOCABULARY]
    lines.append("- a new paragraph: a blank line before it")
    lines.append(f"- nothing else: no other templates, no HTML but {', '.join(_HTML)}, no tables, no lists")
    return "\n".join(lines)


_PROBE = "QQPROBEQQ"


def _render(wikitext: str) -> str:
    """One snippet through the engine's own chain, as an article body is."""
    from wikikit.db.models import SourcePage
    from wikikit.pipeline.stages.elements import ElementContext, process_elements
    from wikikit.pipeline.stages.preprocess import stream_with_keys
    from wikikit.render.inline import decode_inline
    stream, _, _ = stream_with_keys([SourcePage(volume=1, page_number=1, wikitext=wikitext)], 1)
    return decode_inline(process_elements(stream, ElementContext(volume=1)))


def render_table() -> dict:
    """What the engine renders for each construct, measured, as
    {name: [html_open, html_close, after_newline]} plus "paragraph": html
    between two paragraphs.  `after_newline` is what a single newline right
    after the construct becomes: a block that ends its paragraph makes it a new
    one ("<p>"), anything else a space.  The desk applies this table and
    nothing else."""
    table = {}
    for c in VOCABULARY:
        html = _render(f"A {c.open}{_PROBE}{c.close} Z")
        if c.name in TYPESET:
            # the reader typesets it; the engine's chain shows its stand-in
            stand_in = html.removeprefix("A").removesuffix("Z").strip()
            if _PROBE in stand_in or not stand_in:
                raise ValueError(f"the engine no longer lifts {c.name} out: {html!r}")
            table[c.name] = [stand_in]
            continue
        head, sep, tail = html.partition(_PROBE)
        if not sep:
            raise ValueError(f"the engine lost the content of {c.name}: {html!r}")
        close = tail.removesuffix("Z").strip()
        after = _render(f"A {c.open}{_PROBE}{c.close}\nZ").partition(_PROBE)[2]
        after = after.removeprefix(close).removesuffix("Z")
        table[c.name] = [head.removeprefix("A").strip(), close, after if after.strip() else " "]
    para = _render(f"A{PARAGRAPH}Z")
    table["paragraph"] = para.removeprefix("A").removesuffix("Z")
    return table
