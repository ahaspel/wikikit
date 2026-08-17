"""The sweeper ratchet: a marker string-op outside its owner is a regression.

The J-campaign closed the preprocess chain and the K-series closed the edges
(2026-08-15/16): every string-op on marker tokens in shipping code now lives
in the marker's own producer, a sanctioned decoder, or a reader — and the
viewer JS has NONE.  Nothing stopped the next `ftCleanMarkup` or preface
decoder from growing back, so this test makes the closed state a standing
invariant, the way the dup-constants ratchet guards literals and
`test_preprocess_discipline` guards the chain: writing a sweeper made the
measured number go DOWN at the moment it was written
([[feedback_sweepers_hide_bugs]]); here it makes a test go RED instead.

The instrument is the K-survey's own: a line that both names a marker token
(`«` literal or `\\u00ab` escape) and performs a string operation
(`re.sub` / `.replace` / `re.compile` in Python; a regex-method call in JS).
The unit of judgment is the FILE — either a file owns marker work or it
does not; line numbers churn.  A new hit means one of two things:

  * the op belongs in a producer/decoder that already owns the marker —
    move it (the usual verdict); or
  * the file genuinely IS a new owner — add it to OWNERS below with a
    reason, where the diff reviewer can see the claim being made.

`tools/diagnostics` and tests are out of scope on purpose: audit code reads
markers by trade (the leak oracle IS marker patterns), and a diagnostic
that transforms is caught by what it ships — nothing.
"""
from __future__ import annotations

import glob
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# file (repo-relative, /-separated) -> why it may operate on marker tokens.
OWNERS = {
    # the lexicon: THE readers/rewriters (iter/sub_*), strip helpers, emitters
    "src/britannica/markers.py": "the marker lexicon",
    # sanctioned decoders (marker -> target output)
    "src/britannica/render/inline.py": "the site/epub decoder",
    "src/britannica/render/article.py": "the render shell (TITLE/SH peels)",
    "src/britannica/export/markdown.py": "the markdown decoder",
    # producers: peel/emit their OWN constructs
    "src/britannica/pipeline/stages/elements/__init__.py":
        "walker + body/sub-sup producers",
    "src/britannica/pipeline/stages/elements/_anchor.py": "anchor producer",
    "src/britannica/pipeline/stages/elements/_classifier.py": "TITLE peel",
    "src/britannica/pipeline/stages/elements/_link.py": "link wire form",
    "src/britannica/pipeline/stages/elements/_section_anchors.py":
        "section-anchor producer",
    "src/britannica/pipeline/stages/elements/_shapes.py": "TITLE peel",
    "src/britannica/pipeline/stages/elements/_title.py": "title producer",
    "src/britannica/pipeline/stages/elements/_walker.py": "the walker",
    "src/britannica/pipeline/stages/quote_runs.py": "quote-run producer («B»)",
    "src/britannica/pipeline/stages/super_walker.py": "the super walker",
    # readers (throwaway copies / node extraction, never the shipping stream)
    "src/britannica/pipeline/stages/detect_boundaries.py":
        "heading-comparison copies",
    "src/britannica/pipeline/stages/transform_articles/__init__.py":
        "title-node reader",
    # sanctioned decorators (own ONE marker param each)
    "tools/pipeline/annotate_math_markers.py": "«MATH[hint]» owner",
    # annotate_table_markers.py was here: the «TABLE[cols|wide]» grammar moved
    # into the lexicon (`markers.iter_table_spans` / `set_table_wide`) once the
    # render, the annotator, the width-cache key and the measurer turned out to
    # be spelling it four separate ways.  The annotator now stamps through the
    # lexicon and spells no marker of its own.
    # page-policy bake for the generated ancillary pages (through the lexicon
    # readers; its one compile is the masthead-title drop)
    "tools/viewer/ancillary_render.py": "ancillary page policy",
}

_PY_OP = re.compile(r"re\.sub\(|\.replace\(|re\.compile\(")
_PY_GLOBS = ["src/**/*.py", "tools/pipeline/*.py", "tools/viewer/*.py",
             "tools/*.py"]
_JS_OP = re.compile(r"(?:replace|match|split)\s*\(\s*/")
_JS_GLOBS = ["tools/viewer/*.js", "tools/viewer/*.html"]


def _marker_line(line: str) -> bool:
    return "«" in line or "\\u00ab" in line


def _scan(globs, op_re):
    hits: dict[str, list[int]] = {}
    for pat in globs:
        for f in glob.glob(str(ROOT / pat), recursive=True):
            if "__pycache__" in f:
                continue
            rel = str(Path(f).relative_to(ROOT)).replace("\\", "/")
            with open(f, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if _marker_line(line) and op_re.search(line):
                        hits.setdefault(rel, []).append(i)
    return hits


def test_marker_ops_only_in_owners():
    hits = _scan(_PY_GLOBS, _PY_OP)
    # Guard on the guard: a broken glob would pass vacuously.
    assert "src/britannica/markers.py" in hits, \
        "scanner found nothing in markers.py — the scan itself is broken"
    strays = {f: lines for f, lines in hits.items() if f not in OWNERS}
    assert not strays, (
        "marker string-op outside the owner ledger — a new sweeper, or a new "
        "owner to claim explicitly:\n" + "\n".join(
            f"  {f}: lines {lines}" for f, lines in sorted(strays.items()))
        + "\nMove the op into the marker's producer/decoder, or add the file "
          "to OWNERS with its reason.")


def test_owner_ledger_carries_no_ghosts():
    """An OWNERS entry whose file no longer operates on markers (or no longer
    exists) is a stale claim — prune it, so the ledger stays the truth."""
    hits = _scan(_PY_GLOBS, _PY_OP)
    ghosts = [f for f in OWNERS if f not in hits]
    assert not ghosts, (
        "OWNERS entries with no marker ops left — remove them:\n  "
        + "\n  ".join(ghosts))


def test_viewer_js_has_no_marker_regexes():
    """The viewer decodes NOTHING — rendered_html comes baked, and the last
    JS marker regexes (ftCleanMarkup, formatPreview, renderImg's alt sweep)
    guarded provably-plain inputs.  Zero is the contract
    ([[feedback_viewer_no_regex]])."""
    hits = _scan(_JS_GLOBS, _JS_OP)
    assert not hits, (
        "marker regex in viewer JS — the viewer is mechanical; decode "
        "belongs in Python:\n" + "\n".join(
            f"  {f}: lines {lines}" for f, lines in sorted(hits.items())))
