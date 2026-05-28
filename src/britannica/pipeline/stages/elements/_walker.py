"""One-level shape-emitting walker.

Linear left-to-right scanner with shape-keyed recognizers.  At every
text position the walker asks "does any recognizer's opener pattern
match here?" — when one does, the recognizer finds its balanced close
and the whole region becomes one extract.  The scanner advances past
the close and continues.

No priority order.  No per-shape passes.  When two recognizers' openers
could match the same position, the one with the more specific opener
(longer match) wins — same semantics any tokenizer uses.  Each
recognizer is a pure function of `(text, position)`.

After the linear scan, an OUTLINE pass runs over the
placeholderized text.  OUTLINE is line-pattern based (indentation
profile), not position-keyed delimiter-balanced, so it operates on
a different alphabet — it runs as a separate phase, the one genuine
exception to "every shape is a recognizer in the linear scan."

Output: ``(text_with_placeholders, [(placeholder, shape, raw), ...])``.
"""

from __future__ import annotations

import re

from britannica.pipeline.stages.elements._registry import (
    ElementRegistry, _PH, _next_placeholder_id,
)
from britannica.pipeline.stages.elements._shapes import (
    SHAPE_BODY,
    SHAPE_BRACE_PIPE,
    SHAPE_CHART2,
    SHAPE_DOUBLE_BRACE,
    SHAPE_DOUBLE_BRACKET,
    SHAPE_FIGURE,
    SHAPE_HTML_SELF_CLOSING,
    SHAPE_HTML_TAG,
    SHAPE_INLINE_IMAGE,
    SHAPE_MIRROR_GLYPH,
    SHAPE_OUTLINE,
    SHAPE_SECTION,
)
from britannica.pipeline.stages.elements._figure import (
    figure_tail_end,
    figure_wrapper_end,
    html_float_figure_end,
)

# An image whose trailing caption run the figure rule may absorb: a bracket
# `[[File:]]`/`[[Image:]]` or a `{{img float}}`/`{{figure}}`/`{{FI}}` template
# (hieroglyph excluded).
_FIG_IMAGE_RAW = re.compile(
    r"\[\[(?:File|Image):|\{\{\s*(?:img\s*float|figure|FI)\b", re.IGNORECASE)


# ── Recognizer patterns ───────────────────────────────────────────────
#
# Each entry is `(shape, pattern)`.  `pattern.match(text, pos)`
# returns the full-extract match starting at `pos`, or None.  The
# scanner tries each recognizer at every "opener hint" position in
# *opener-specificity* order — most-specific opener first.

# CHART2: the multi-template region carved out by today's bespoke
# regex (covers `{{missing table}}`, `{{center|...}}`, `{{EB1911 fine
# print/s}}` wrappers before `{{chart2/start}}…{{chart2/end}}`, plus
# an optional trailing `<poem>` OCR-garbage and `{{EB1911 fine
# print/e}}`).  Most-specific opener — `{{chart2/start` is fully
# unambiguous.
_CHART2_RE = re.compile(
    r"(?:\{\{missing table\}\}\s*(?:\x01PAGE:\d+\x01)?\s*)?"
    r"(?:\{\{center\|[^}]*\}\}\s*)?"
    r"(?:\{\{EB1911 fine print/s\}\}\s*)?"
    r"\{\{chart2/start[^}]*\}\}.*?\{\{chart2/end\}\}"
    r"(?:\s*<poem>.*?</poem>)?"
    r"(?:\s*\{\{EB1911 fine print/e\}\})?",
    re.DOTALL | re.IGNORECASE,
)

# HTML self-closing `<ref ... />` — must match before HTML_TAG's
# `<ref...>...</ref>` because both share the `<ref` opener but only
# self-closing has the `/>` terminator with no `</ref>` to find.
_REF_SELF_RE = re.compile(r"<ref\s[^>]*/\s*>", re.IGNORECASE)

# Wikisource `<pagequality level="N" user="X" />` — page-quality metadata,
# previously inside `<noinclude>` and swept by NOINCLUDE.  With noinclude
# tags wiped upstream, this self-closing tag sits naked in body raw.
# Lifted as its own HTML_SELF_CLOSING element; producer returns "".
_PAGEQUALITY_RE = re.compile(
    r"<pagequality\b[^>]*/\s*>", re.IGNORECASE)

# HTML tag recognizers — each one specific to a single tag name.
# `<ref>` placed AFTER `<ref…/>` (specificity: longer match preferred
# means we try self-closing first; only fall through to the
# `<TAG>…</TAG>` form when self-closing doesn't match).
_REF_RE         = re.compile(r"<ref(?:\s[^>]*)?>.*?</ref>",
                              re.DOTALL | re.IGNORECASE)
_HTML_TABLE_RE  = re.compile(r"<table\b[^>]*>.*?</table>",
                              re.DOTALL | re.IGNORECASE)
_POEM_RE        = re.compile(r"<poem>.*?</poem>",
                              re.DOTALL | re.IGNORECASE)
_MATH_RE        = re.compile(r"<math[^>]*>.*?</math>",
                              re.DOTALL | re.IGNORECASE)
_SCORE_RE       = re.compile(r"<score[^>]*>.*?</score>",
                              re.DOTALL)
_HIEROGLYPH_TAG_RE = re.compile(r"<hiero>[^<]*</hiero>", re.IGNORECASE)

# Wikisource transclusion marker `<section begin="X"/>` / `<section end/>`.
# A self-closing structural tag carrying boundary identity (the name), no inner
# content.  Recognized so it becomes an owned SECTION element carried raw, rather
# than being swept by the text producer's catch-all HTML strip.
_SECTION_RE = re.compile(r"<section\s+(?:begin|end)\b[^>]*>", re.IGNORECASE)

# `<noinclude>` tags are stripped upstream in `_transform_text_v2`
# before the article walker runs — see the comment there.  The article
# pipeline therefore never sees them.  The plate pipeline still uses
# noinclude as a structural anchor for plate-title extraction and is
# unaffected (it has its own walker).

# Wikisource `<span style="…{{mirrorH}}…">content</span>` — a horizontally
# mirrored glyph (used in ALPHABET to show left-right-flipped letters of
# early/regional alphabets like Etruscan, Italic, Cleonae's reversed E).
# Recognized as its own shape so the mirror SEMANTIC survives end-to-end
# (catch-all stripping silently lost the styling, leaving glyphs displayed
# un-mirrored).  The producer emits `«MIRROR:content«/MIRROR»`; the viewer
# applies `transform: scaleX(-1)`.
_MIRROR_GLYPH_RE = re.compile(
    r'<span\s+style\s*=\s*"[^"]*\{\{mirrorH\}\}[^"]*">(?:[^<]|<(?!/span>))*?</span>',
    re.IGNORECASE | re.DOTALL,
)


# DOUBLE_BRACE template recognizers — named templates with up to four
# levels of `{{…}}` nesting (CASTLE Fig. 9 cap parameters reach four
# deep).  Specificity: each has a fixed template-name opener like
# `{{img float|` or `{{hieroglyph|`, more specific than bare `{{`.
_IMAGE_FLOAT_RE = re.compile(
    r"\{\{(?:img float|figure|FI)\s*\|"
    r"(?:[^{}]|\{\{(?:[^{}]|\{\{(?:[^{}]|\{\{[^{}]*\}\})*\}\})*\}\})*"
    r"\}\}",
    re.DOTALL | re.IGNORECASE,
)
_HIEROGLYPH_TMPL_RE = re.compile(
    r"\{\{hieroglyph\|([^{}]*)\}\}", re.IGNORECASE)
# Contributor-footer templates — the source's structural declaration
# "this is article-attribution metadata, not body content."  Two
# canonical shapes plus the bare-initials shortcuts:
#   * `{{EB1911 footer initials|Full Name|Initials[|name2=…]}}`
#   * `{{EB1911 footer double initials|N1|I1|N2|I2}}`
#   * `{{EB1911 XYZ}}` — bare-initials shortcut (template-name IS the
#     initials reference; ~20 distinct names in corpus: TAs, WABC,
#     WAP, MG, LD, JF-K, DH, RNB, DMn, LJS, JHlR, AMC, AWH, FJH, AN,
#     AFP, JE, HWR*, …).  Caps-leading 1-5 chars distinguishes these
#     from `EB1911 sfrac`/`tfrac` (lowercase) and `EB1911 Coordinates`/
#     `Intra-Article Link`/`Fine Print` (longer than 5 chars).
#
# `extract_contributors` reads these directly from raw page text in
# its own pipeline stage; the producer here returns empty so body
# renders nothing — replaces the `_strip_templates` catch-all path
# that silently dropped them.
_CONTRIBUTOR_FOOTER_RE = re.compile(
    r"\{\{\s*EB1911\s+footer(?:\s+double)?\s+initials\s*\|"
    r"(?:[^{}]|\{\{[^{}]*\}\})*"
    r"\}\}"
    r"|\{\{\s*EB1911\s+[A-Z][A-Za-z*\-]{0,4}\s*\}\}",
    re.IGNORECASE | re.DOTALL,
)
# `{{dual line|A|B}}` — pure layout primitive that stacks two lines
# (`A<br>B`).  Args A and B can carry any inline content including
# nested templates (chem `C{{sub|6}}H{{sub|5}}`, layout `{{gap}}`,
# refs `<ref>…</ref>`).  Up to four levels of `{{…}}` nesting, matching
# `_IMAGE_FLOAT_RE`'s depth — covers every observed case in 611 corpus
# instances.  Lifting this template at the walker level gives the
# classifier a bounded unit to inspect; if its content is chem/math-
# shaped, the classifier will route accordingly (future predicate),
# instead of body-text smuggling the rendering in via a regex pass.
_DUAL_LINE_RE = re.compile(
    r"\{\{\s*dual\s+line\s*\|"
    r"(?:[^{}]|\{\{(?:[^{}]|\{\{(?:[^{}]|\{\{[^{}]*\}\})*\}\})*\}\})*"
    r"\}\}",
    re.DOTALL | re.IGNORECASE,
)
# Labeled display-equation templates — `{{equation|content}}`,
# `{{MathForm1|label|content}}`, `{{ne|content}}`.  These are
# structurally declared as math by their template name — the source's
# own assertion "this is a math expression with its own paragraph
# context."  Walker bounds the template (purely structural); classifier
# applies a math label; producer emits `«EQN:LABEL»content«/EQN»` with
# `\n\n` paragraph margins.
#
# Inline-typography templates (`{{sfrac|...}}`, `{{sub|...}}`,
# `{{sup|...}}`, the fraction variants) are NOT walker chunks even
# though their content is mathematical — they're typography whose
# rendered output flows back into prose, and body-text owns rendering.
# See the chunk-vs-typography principle: the source must declare math
# via the template name itself for the walker to recognize it.
#
# Closer is found via `_find_balanced_template_end` — a depth-counting
# scanner that masks `<math>`/`<nowiki>`/HTML-comment spans so the
# literal `{`/`}` inside LaTeX (e.g. `\text{A}` in `{{ne|<math>…}}`)
# don't trip brace depth.  Regex with `[^{}]` would lose those.
_LABELED_EQUATION_TEMPLATE_NAMES_PATTERN = (
    r"equation"
    r"|MathForm1"
    r"|ne"
)
_LABELED_EQUATION_TEMPLATE_OPENER_RE = re.compile(
    r"\{\{\s*(" + _LABELED_EQUATION_TEMPLATE_NAMES_PATTERN + r")\s*\|",
    re.IGNORECASE,
)


def _find_balanced_template_end(text: str, start: int) -> int | None:
    """Find the position one past the balanced ``}}`` closing the
    `{{...}}` template that begins at ``start``.

    Returns None if no balanced close exists.

    Treats ``<math>…</math>``, ``<nowiki>…</nowiki>`` and ``<!--…-->``
    as opaque atoms — `{`/`}` inside them don't count toward brace
    depth.  Needed for `{{ne|<math>\\text{A}</math>}}` and similar
    where LaTeX content carries literal braces a regex `[^{}]` would
    refuse to cross.
    """
    if text[start:start + 2] != "{{":
        return None
    n = len(text)
    depth = 1
    i = start + 2
    while i < n:
        ch = text[i]
        if ch == "<":
            tail = text[i:i + 6].lower()
            if tail.startswith("<math"):
                end = text.lower().find("</math>", i + 5)
                if end >= 0:
                    i = end + 7
                    continue
            elif tail.startswith("<nowik"):
                end = text.lower().find("</nowiki>", i + 6)
                if end >= 0:
                    i = end + 9
                    continue
            elif text[i:i + 4] == "<!--":
                end = text.find("-->", i + 4)
                if end >= 0:
                    i = end + 3
                    continue
            i += 1
        elif ch == "{" and i + 1 < n and text[i + 1] == "{":
            depth += 1
            i += 2
        elif ch == "}" and i + 1 < n and text[i + 1] == "}":
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    return None
# `{{Css image crop|…}}` — a STANDALONE DjVu crop (multi-line params), optionally
# followed by a `{{center|cap}}` / `{{csc|cap}}` caption.  Recognized as one
# DOUBLE_BRACE image element (→ DJVU_CROP); the producer crops + captions it.
# In-table crops route via the table classifier instead — this catches the
# standalone ones the old `_css_crop_replace` pre-pass used to convert.
_DJVU_CROP_RE = re.compile(
    r"\{\{\s*Css image crop\b(?:[^{}]|\{\{[^{}]*\}\})*\}\}"
    r"(?:\s*\{\{\s*(?:center|csc)\s*\|(?:[^{}]|\{\{[^{}]*\}\})*\}\})?",
    re.IGNORECASE | re.DOTALL)
# `{{raw image|X}}` — EB1911's bare full-image syntax (a DjVu page-ref → full-
# page render, or a plain filename), with an OPTIONAL trailing `{{c|cap}}`.
# Recognized as one DOUBLE_BRACE image element (→ RAW_IMAGE).
_RAW_IMAGE_RE = re.compile(
    r"\{\{\s*raw\s+image\s*\|[^{}|]+\}\}"
    r"(?:\s*\{\{\s*c\s*\|(?:[^{}]|\{\{[^{}]*\}\})*\}\})?",
    re.IGNORECASE | re.DOTALL)

# DOUBLE_BRACKET image — `[[File:…]]` or `[[Image:…]]` with optional
# trailing caption block (EXTCAP form).  The regex absorbs nothing more —
# inline-vs-block is decided structurally by lookahead at dispatch time
# (see _is_inline_image_position below); the walker advances past `]]`
# (or past the EXTCAP) and the surrounding bytes stay in the
# placeholderized text intact.
_IMAGE_RE = re.compile(
    r"\[\[(?:File|Image):[^\]]+\]\]"
    r"(?:\s*\n\n?("
    r"(?:<[a-z]+[^>\n]*>\s*)?"
    r"(?:"
    r"(?:\{\{sm\||\{\{center\||\{\{(?:sc|small-caps|Fine)\|Fig[.}]"
    r"|Fig[. ]\d|Plate\s|\d+\.\s*[—–])"
    r"[^\n]*"
    r"(?:\n(?:<[a-z]+[^>\n]*>\s*)?"
    r"(?:\{\{center\||\{\{(?:sc|small-caps|Fine)\|Fig[.}]"
    r"|Fig[. ]\d|\d+\.\s*[—–])"
    r"[^\n]*)?"
    r"|"
    r"(?:From|After|Photo|Copyright|Modified)\s[^\n]*"
    r"\n(?:<[a-z]+[^>\n]*>\s*)?"
    r"(?:\{\{center\||\{\{(?:sc|small-caps|Fine)\|Fig[.}]"
    r"|Fig[. ]\d|\d+\.\s*[—–])"
    r"[^\n]*"
    r")"
    r"))?",
    re.IGNORECASE,
)


# Inline-image structural recognition.  At the moment the walker has just
# matched an `[[File:…]]` (no EXTCAP), it checks the text immediately AFTER
# `]]` for an inline-glyph signal: same-line content that ISN'T a line-ender
# (`\n` / `<br>`) and ISN'T a wikitable cell separator (`|`).  No bytes
# consumed; the placeholderized text keeps its surrounding context.  When the
# signal is present the walker emits SHAPE_INLINE_IMAGE instead of
# SHAPE_DOUBLE_BRACKET; the classifier maps that shape to its own label and
# the dedicated producer stamps `align=inline`.
_BR_TAG_RE = re.compile(r"<br\s*/?\s*>", re.IGNORECASE)


def _is_inline_image_position(text: str, pos: int) -> bool:
    """True iff position ``pos`` (right after a matched `]]`) sits in an
    inline-prose context — same-line non-structural content follows.

    Structural separators / closers are NOT inline; they indicate the
    image sits at a container boundary where the container owns layout:
      * line-ender ``\\n`` or ``<br>`` — paragraph / line break
      * wikitable cell pipe ``|`` (``|`` or ``||`` or ``|-``)
      * template close ``}`` (``}}`` of a wrapper template)
      * template open ``{`` (``{{brace2|…}}`` decoration, ``{{Ts|…}}``
        cell styling — non-prose; an inline content template would be
        inside a body sentence and the image would have at least one
        separating character, e.g. punctuation or space-then-alpha)
      * HTML close tag ``</…>`` (``</td>``, ``</tr>``, ``</span>``, …)
        — the image is the LAST thing in its enclosing HTML element.
    Inline-element OPEN tags (``<ref>``, ``<sub>``, etc.) are NOT
    structural separators here — same-line elements after an image
    are part of the inline flow.
    """
    end = pos
    n = len(text)
    while end < n and text[end] in " \t":
        end += 1
    if end >= n:
        return False
    nxt = text[end]
    if nxt == "\n" or nxt == "|" or nxt == "}" or nxt == "{":
        return False
    if nxt == "<":
        if _BR_TAG_RE.match(text, end):
            return False
        if end + 1 < n and text[end + 1] == "/":
            return False
    return True


# Recognizer dispatch table in opener-specificity order.  At each
# opener-hint position the linear scanner walks this list and uses
# the first recognizer whose pattern matches.
_REGEX_RECOGNIZERS: list[tuple[str, re.Pattern]] = [
    (SHAPE_CHART2,            _CHART2_RE),
    (SHAPE_SECTION,           _SECTION_RE),
    (SHAPE_MIRROR_GLYPH,      _MIRROR_GLYPH_RE),
    (SHAPE_HTML_SELF_CLOSING, _REF_SELF_RE),
    (SHAPE_HTML_SELF_CLOSING, _PAGEQUALITY_RE),
    (SHAPE_HTML_TAG,          _REF_RE),
    (SHAPE_HTML_TAG,          _HTML_TABLE_RE),
    (SHAPE_HTML_TAG,          _POEM_RE),
    (SHAPE_HTML_TAG,          _MATH_RE),
    (SHAPE_HTML_TAG,          _SCORE_RE),
    (SHAPE_HTML_TAG,          _HIEROGLYPH_TAG_RE),
    (SHAPE_DOUBLE_BRACE,      _DJVU_CROP_RE),
    (SHAPE_DOUBLE_BRACE,      _RAW_IMAGE_RE),
    (SHAPE_DOUBLE_BRACE,      _IMAGE_FLOAT_RE),
    (SHAPE_DOUBLE_BRACE,      _HIEROGLYPH_TMPL_RE),
    (SHAPE_DOUBLE_BRACE,      _CONTRIBUTOR_FOOTER_RE),
    (SHAPE_DOUBLE_BRACE,      _DUAL_LINE_RE),
    (SHAPE_DOUBLE_BRACKET,    _IMAGE_RE),
]


# Combined opener-hint regex.  Used only for efficiency — `re.search`
# jumps to the next position that could possibly begin an extract,
# so we don't iterate position-by-position over megabytes of prose.
# The actual recognizer dispatch happens via `.match()` at the
# hinted position.  Order doesn't affect correctness (any match
# triggers dispatch); the order below is just readable grouping.
_OPENER_HINT_RE = re.compile(
    r"\{\{chart2/start"             # CHART2
    r"|\{\|"                        # BRACE_PIPE
    r"|<ref\b"                      # HTML_SELF_CLOSING ref / HTML_TAG ref
    r"|<pagequality\b"              # HTML_SELF_CLOSING pagequality metadata
    r"|<section\s+(?:begin|end)\b"  # SECTION transclusion marker
    r"|<(?:table|poem|math|score|hiero)\b"  # HTML_TAG tag variants
    r"|<span\s+style\s*=\s*\"[^\"]*\{\{mirrorH"  # MIRROR_GLYPH span
    r"|<(?:span|div)\b[^>]*\bfloat\s*:"  # FIGURE HTML float-wrapper
    r"|\[\[(?:File|Image):"         # DOUBLE_BRACKET image
    r"|\{\{\s*(?:center|block\s*center|c|c?sc|small-caps)\s*\|"  # FIGURE wrapper (image inside)
    r"|\{\{\s*(?:img float|figure|FI|hieroglyph|Css image crop|raw\s+image|dual\s+line|EB1911)\b"  # DOUBLE_BRACE templates
    r"|\{\{\s*(?:" + _LABELED_EQUATION_TEMPLATE_NAMES_PATTERN + r")\s*\|",  # labeled-equation templates
    re.IGNORECASE,
)


# Spans whose `{|` / `|}` are NOT wiki-table syntax: LaTeX inside
# `<math>`, verbatim `<nowiki>`, HTML comments.  Masked off before the
# balanced-table scanner runs so a stray `\frac{|…}` in math doesn't
# read as a table opener.
_NON_TABLE_BRACE_SPAN_RE = re.compile(
    r"<math\b[^>]*>.*?</math\s*>"
    r"|<nowiki\b[^>]*>.*?</nowiki\s*>"
    r"|<!--.*?-->",
    re.DOTALL | re.IGNORECASE,
)


def _find_brace_pipe_end(text: str, start: int) -> int | None:
    """Find the byte position one past the balanced ``|}`` (or
    ``</table>``) closing the wikitable that begins at ``start``.

    Returns None if no balanced close exists.

    Mirrors the legacy balanced scanner's depth tracking: ``{{…}}``
    template blocks are skipped wholesale, nested ``{|…|}`` increment
    depth, ``<table>…</table>`` HTML pairs maintain their own depth
    counter so a ``</table>`` inside a nested HTML table doesn't
    masquerade as a wiki-table close.
    """
    masked = _NON_TABLE_BRACE_SPAN_RE.sub(
        lambda m: " " * len(m.group(0)), text)
    if masked[start:start+2] != "{|":
        return None
    depth = 0
    html_depth = 0
    i = start
    n = len(text)
    while i < n - 1:
        if masked[i:i+2] == "{{":
            tdepth = 1
            j = i + 2
            while j < n - 1 and tdepth > 0:
                if masked[j:j+2] == "{{":
                    tdepth += 1
                    j += 2
                elif masked[j:j+2] == "}}":
                    tdepth -= 1
                    j += 2
                else:
                    j += 1
            if tdepth == 0:
                i = j
            else:
                # Malformed `{{` without matching `}}` — treat as
                # literal and keep scanning for the table close.
                i += 2
            continue
        if (i + 7 < n and masked[i:i+6].lower() == "<table"
                and masked[i+6] in (" ", ">", "\t", "\n")):
            html_depth += 1
            j = text.find(">", i)
            i = j + 1 if j >= 0 else i + 6
            continue
        if masked[i:i+8].lower() == "</table>" and html_depth > 0:
            html_depth -= 1
            i += 8
            continue
        if masked[i:i+2] == "{|":
            depth += 1
            i += 2
        elif (masked[i:i+2] == "|}"
              or (masked[i:i+8].lower() == "</table>" and depth > 0)):
            depth -= 1
            closer_len = 2 if masked[i:i+2] == "|}" else 8
            if depth == 0:
                return i + closer_len
            i += closer_len
        else:
            i += 1
    return None


def _new_placeholder() -> str:
    return f"{_PH}ELEM:{_next_placeholder_id()}{_PH}"


def _walk_balanced_shapes(
    text: str, _allow_figure: bool = True,
) -> tuple[str, list[tuple[str, str, str]]]:
    """Single linear pass: find every position where a recognizer's
    opener could match, dispatch in opener-specificity order, take
    the first successful match.  Returns the placeholderized text
    plus the extract tuples in source order.
    """
    extracts: list[tuple[str, str, str]] = []
    output: list[str] = []
    pos = 0
    n = len(text)
    figures = _allow_figure

    while pos < n:
        hint = _OPENER_HINT_RE.search(text, pos)
        if hint is None:
            output.append(text[pos:])
            break

        opener_pos = hint.start()
        if opener_pos > pos:
            output.append(text[pos:opener_pos])

        # Try every recognizer at this position in specificity order.
        # First successful match wins.
        matched: tuple[int, str, str] | None = None

        # Figure wrapper: a `{{center|…image…}}` enclosing an image IS the
        # figure unit — recognized before its inner image so the caption that
        # lives inside the wrapper stays intact.  Also the HTML float-wrapper
        # variant `<span/div style="float:…">…image…</…>` (WATERBUCK family).
        if figures:
            w = figure_wrapper_end(text, opener_pos)
            if w is None:
                w = html_float_figure_end(text, opener_pos)
            if w is not None:
                matched = (w, SHAPE_FIGURE, text[opener_pos:w])

        if matched is None:
            for shape, pattern in _REGEX_RECOGNIZERS:
                m = pattern.match(text, opener_pos)
                if m is not None:
                    matched = (m.end(), shape, m.group(0))
                    break

            # Figure tail: a bare image followed by a structural caption run
            # becomes one FIGURE (image + caption), stopping at body prose.
            if (figures and matched is not None
                    and matched[1] in (SHAPE_DOUBLE_BRACKET, SHAPE_DOUBLE_BRACE)
                    and _FIG_IMAGE_RAW.match(matched[2])):
                fig_end = figure_tail_end(text, matched[0])
                if fig_end > matched[0]:
                    matched = (fig_end, SHAPE_FIGURE, text[opener_pos:fig_end])

            # Inline-image lookahead: a bare `[[File:…]]` (no EXTCAP, no
            # figure-tail upgrade) sitting in same-line prose is structurally
            # an inline glyph.  Lookahead-only — no bytes absorbed; the
            # surrounding text keeps its newlines and separators intact.
            if (matched is not None
                    and matched[1] == SHAPE_DOUBLE_BRACKET
                    and matched[2].endswith("]]")
                    and _is_inline_image_position(text, matched[0])):
                matched = (matched[0], SHAPE_INLINE_IMAGE, matched[2])

        # Labeled-display-equation templates use a balanced-brace
        # scanner (not a regex) because `{{ne|<math>...</math>}}`
        # carries LaTeX with literal `{`/`}` (`\text{A}` etc.) that a
        # `[^{}]`-based regex can't span.  The scanner masks
        # `<math>`/`<nowiki>`/comment spans before counting braces.
        if matched is None and _LABELED_EQUATION_TEMPLATE_OPENER_RE.match(
                text, opener_pos):
            end = _find_balanced_template_end(text, opener_pos)
            if end is not None:
                matched = (end, SHAPE_DOUBLE_BRACE,
                           text[opener_pos:end])

        # BRACE_PIPE doesn't have a regex pattern — its closer
        # requires balanced depth tracking.  Try it last (lowest
        # specificity) only if no regex recognizer fired.
        if matched is None and text[opener_pos:opener_pos+2] == "{|":
            end = _find_brace_pipe_end(text, opener_pos)
            if end is not None:
                matched = (end, SHAPE_BRACE_PIPE, text[opener_pos:end])

        if matched is None:
            # Opener-hint matched (the bytes LOOK like an opener) but
            # no recognizer fully matched (no closer found, or the
            # full pattern didn't fit).  Advance past one character
            # and continue scanning — same fail-safe as today's
            # regex.sub which silently skips no-match positions.
            output.append(text[opener_pos])
            pos = opener_pos + 1
            continue

        end_pos, shape, raw = matched
        ph = _new_placeholder()
        output.append(ph)
        extracts.append((ph, shape, raw))
        pos = end_pos

    return "".join(output), extracts


def _walk_outline(
    text: str,
) -> tuple[str, list[tuple[str, str, str]]]:
    """Run the OUTLINE line-pattern scanner over `text` (already
    placeholderized for balanced shapes).  Returns the further-
    placeholderized text and the new outline extracts.

    OUTLINE is the one shape that isn't position-keyed delimiter-
    balanced — it operates on indentation profile across lines —
    so it runs as a separate phase rather than as a recognizer in
    the linear scan.
    """
    from britannica.pipeline.stages.elements._outline import _extract_outlines
    # `_extract_outlines` registers into an `ElementRegistry`; we
    # convert its output to the shape-tagged tuple form afterwards.
    bucket = ElementRegistry()
    text = _extract_outlines(text, bucket)
    outline_extracts = [
        (ph, SHAPE_OUTLINE, raw)
        for ph, (_name, raw) in bucket.elements.items()
    ]
    return text, outline_extracts


_PLACEHOLDER_RE = re.compile(rf"{re.escape(_PH)}ELEM:\d+{re.escape(_PH)}")

# Template names whose ``{{name|…}}`` wrappers must stay ATOMIC during
# body-splitting — placeholders inside them belong to the wrapper's body
# run, not to a separate split.  Otherwise the wrapper's ``{{`` opener
# and ``}}`` closer land in different BODY runs and the body producer's
# template-handler regexes (which require both ends visible) can't match.
#
# Two families:
#
#   * Layout wrappers — ``_unwrap_layout_templates`` strips them to inner
#     content (``{{center|…}}``, ``{{larger|…}}``, …).  Same names as
#     ``body_text._LAYOUT_TEMPLATES``.
#   * Content-transform wrappers — ``_unwrap_content_templates`` converts
#     them to markers (``{{ne||content|(N)}}`` → ``«EQN:N»content«/EQN»``).
#     Without atomicity, the BODY-as-element refactor splits these at any
#     embedded element placeholder (e.g. ``<math>`` extracted from inside
#     ``{{ne|}}``) and the regex no longer sees both ends.
_LAYOUT_WRAPPER_NAMES: tuple[str, ...] = (
    # Layout (unwrap-to-content)
    "block center", "center", "c", "fine block",
    "EB1911 Fine Print", "larger", "smaller", "nowrap",
    "Fine", "sm",
    # Content-transform (rewrite to marker — must stay whole around
    # placeholders for the rewrite regex to match)
    "ne",
    "sans-serif",
    "xx-larger", "x-larger",
)

# HTML wrapper tags whose body content includes extracted-element
# placeholders (e.g. ``<div style="float:right">[[File:…]]</div>`` in
# ACCUMULATOR).  Same atomic-span discipline as the wikitext layout
# wrappers above — keeping the wrapper whole lets the body producer's
# Family A wrapper-strip rule pair its open/close across the embedded
# placeholder.  Enumerated; mirrors ``body_text``'s wrapper-strip set.
_HTML_WRAPPER_TAGS: tuple[str, ...] = (
    "div", "span", "small", "big", "p", "ins",
)

# Paired begin/end markers — `{{NAME/s}}…{{NAME/e}}` — that span body
# content (potentially with embedded element placeholders).  The body
# producer's regex needs to see the whole pair to emit a marker; if
# body-wrap fragments the span at a placeholder boundary, the regex
# can't match across body runs.  Listed here so atomic-span finder
# keeps them whole.  Match-text built dynamically (no `|` arg syntax
# like the brace-counted wrappers above).
_PAIRED_WRAPPER_NAMES: tuple[str, ...] = (
    "EB1911 fine print",
    "c",
    "block center",
    "center block",
)


def _find_atomic_wrapper_spans(text: str) -> list[tuple[int, int]]:
    """Find every balanced wrapper span that should stay atomic during
    body-splitting.  Returns ``[(start, end), …]`` sorted by start.

    Two wrapper families covered:

      * ``{{NAME|…}}`` wikitext layout wrappers (``{{center|…}}``,
        ``{{larger|…}}``, …) — brace-counted matching so an inner
        template with a literal single ``{`` (e.g. ``{{xx-larger|√ {}}``
        in STEAM_ENGINE's Q=V equation) doesn't break the pairing.
      * ``<TAG…>…</TAG>`` HTML wrappers (``<div style="float:right">``
        around ``[[File:…]]`` in ACCUMULATOR, similar inline spans).
        Non-greedy DOTALL match — sufficient for the non-nested
        wrapper cases that exist in the corpus today; nested cases
        would need depth tracking, but the body producer's wrapper-
        strip already handles content correctness, so we only need
        ``_wrap_body_runs`` to KEEP the span together.

    Placeholders inside any returned span stay in the surrounding BODY
    run; the producer's wrapper-strip then sees the whole wrapper and
    can pair its open/close cleanly."""
    spans: list[tuple[int, int]] = []
    text_lower = text.lower()
    n = len(text)
    for name in _LAYOUT_WRAPPER_NAMES:
        prefix = "{{" + name.lower() + "|"
        pos = 0
        while pos < n:
            idx = text_lower.find(prefix, pos)
            if idx < 0:
                break
            content_start = idx + len(prefix)
            depth = 1
            j = content_start
            while j < n - 1:
                if text[j:j + 2] == "{{":
                    depth += 1
                    j += 2
                elif text[j:j + 2] == "}}":
                    depth -= 1
                    if depth == 0:
                        break
                    j += 2
                else:
                    j += 1
            if depth == 0:
                spans.append((idx, j + 2))
                pos = j + 2
            else:
                pos = content_start
    for tag in _HTML_WRAPPER_TAGS:
        pattern = re.compile(
            rf"<{tag}\b[^>]*>.*?</{tag}>",
            re.IGNORECASE | re.DOTALL)
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end()))
    for name in _PAIRED_WRAPPER_NAMES:
        esc = re.escape(name)
        pattern = re.compile(
            rf"\{{\{{\s*{esc}\s*/s\s*\}}\}}.*?"
            rf"\{{\{{\s*{esc}\s*/e\s*\}}\}}",
            re.IGNORECASE | re.DOTALL)
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end()))
    spans.sort()
    return spans


def _wrap_body_runs(
    text: str,
    extracts: list[tuple[str, str, str]],
) -> tuple[str, list[tuple[str, str, str]]]:
    """Wrap residual prose runs in ``text`` as SHAPE_BODY extracts.

    After ``_walk_balanced_shapes`` + ``_walk_outline``, the article-level
    placeholderized text is a mix of placeholders and residual body prose.
    This phase makes it homogeneous: each prose run becomes its own
    BODY-shape extract with its own placeholder, so the body producer is
    just another producer in the dispatch table and article assembly
    collapses to ordered concatenation of element markers.

    Layout-template wrappers (``{{center|…}}``, ``{{larger|…}}``, …) are
    treated as ATOMIC — a placeholder inside such a wrapper stays in the
    surrounding BODY run rather than splitting it.  The body producer's
    brace-counted ``_unwrap_layout_templates`` then peels the wrapper
    cleanly; the inner placeholder rides through into the BODY marker,
    where ``substitute_top_level_markers`` resolves it cross-reference-
    style (the same mechanism a child marker inside a parent marker
    already uses)."""
    atomic_spans = _find_atomic_wrapper_spans(text)

    def _is_in_atomic_span(pos: int) -> bool:
        for s, e in atomic_spans:
            if s <= pos < e:
                return True
            if pos < s:
                return False
        return False

    out: list[str] = []
    body_buf: list[str] = []
    n = len(text)
    pos = 0

    def _flush_body() -> None:
        if not body_buf:
            return
        body_raw = "".join(body_buf)
        body_buf.clear()
        if not body_raw:
            return
        ph = _new_placeholder()
        out.append(ph)
        extracts.append((ph, SHAPE_BODY, body_raw))

    while pos < n:
        m = _PLACEHOLDER_RE.match(text, pos)
        if m and not _is_in_atomic_span(pos):
            _flush_body()
            out.append(m.group(0))
            pos = m.end()
        else:
            body_buf.append(text[pos])
            pos += 1
    _flush_body()
    return "".join(out), extracts


def walk(
    text: str,
    _allow_outline: bool = True,
    _allow_figure: bool = True,
    _wrap_body: bool = False,
) -> tuple[str, list[tuple[str, str, str]]]:
    """One-level shape-emitting walker.

    Linear scan + optional OUTLINE phase + optional BODY-wrap phase.
    Returns the placeholderized text plus `(placeholder, shape, raw)`
    tuples.

    ``_allow_outline=False`` is passed when the parent shape is
    OUTLINE so the outline scanner doesn't re-trigger on its own
    bytes.  ``_allow_figure=False`` is passed for every recursive
    (inside-an-element) descent so a figure — a body-level construct —
    is only recognized at the article level, never inside another
    element or the figure producer's own re-processing of its span.
    ``_wrap_body=True`` is passed at the article entry point so
    residual prose between extracted elements becomes its own SHAPE_BODY
    extracts; recursive walks (inside cells, captions, figure interiors)
    pass False — those contexts don't have an article-level "body."
    """
    text, extracts = _walk_balanced_shapes(text, _allow_figure)
    if _allow_outline:
        text, outline_extracts = _walk_outline(text)
        extracts.extend(outline_extracts)
    if _wrap_body:
        text, extracts = _wrap_body_runs(text, extracts)
    return text, extracts
