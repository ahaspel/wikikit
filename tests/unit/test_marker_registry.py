"""``RENDERED_GUILLEMET_MARKER_NAMES`` must list every marker the producers emit.

The constant says of itself that keeping it in lockstep "is exactly what this
constant exists to enforce".  Nothing ever failed when it wasn't, so it drifted:
`P`, `TITLE`, `OLI`, `OUTLINE`, `IOUTLINE` and `DHRI` shipped in every build and
none was listed.  That is not cosmetic — ``export/markdown.py`` grounds its
"TOTAL by construction" claim in this constant, so the names the registry omits are
exactly the ones it has no rule for, and 151 articles shipped a raw `«OUTLINE»` in
the download bundle as the direct consequence.  (The OUTLINE family is gone now —
`:` is an INDENT and a list is a LIST, [[project_outline_arc]] — so the registry
lists `OL`/`UL`/`LI`; the lesson is the drift, not the particular names.)

This is the consumer that fails when the registry isn't true.  The transform
snapshots are PRODUCED marker streams, so they sample what the producers actually
emit at unit-test speed; the same check runs corpus-wide in the quality report as
``unregistered_marker``.

The discriminator is the oracle's own pattern, which requires a `:`/`»` terminator —
so OCR mojibake (`Â«ff`, `«this`: 29 occurrences of garbled Greek and math across
the corpus) cannot match, and no exemption list is needed to keep it out.
"""
from collections import Counter
from pathlib import Path

from britannica.markers import RENDERED_GUILLEMET_MARKER_NAMES
from britannica.markers import marker_names

SNAPS = Path(__file__).resolve().parents[1] / "snapshots" / "transform"
_REGISTERED = frozenset(RENDERED_GUILLEMET_MARKER_NAMES)


def _emitted_names() -> Counter:
    """Marker name → occurrences across every produced snapshot body.

    ``marker_names`` is the SAME extractor the corpus-wide ``unregistered_marker``
    check uses, so the two tiers can't disagree about what a marker is.
    """
    names = Counter()
    for path in sorted(SNAPS.glob("*.body.txt")):
        names.update(marker_names(path.read_text(encoding="utf-8")))
    return names


def test_the_sample_is_not_empty():
    """A vacuous pass is the bug this whole file exists to catch.

    `markdown.py`'s `_OUTLINE_RE` matched nothing for five weeks and read as clean;
    a registry test over zero fixtures would read the same way.  Assert the sample
    has substance before asserting anything about it.
    """
    names = _emitted_names()
    assert len(list(SNAPS.glob("*.body.txt"))) >= 15, "snapshot bodies missing"
    assert sum(names.values()) >= 1000, f"too few markers sampled: {names.total()}"


def test_every_emitted_marker_name_is_registered():
    unregistered = Counter({n: k for n, k in _emitted_names().items()
                            if n not in _REGISTERED})
    assert not unregistered, (
        "marker names emitted by the producers but absent from "
        "RENDERED_GUILLEMET_MARKER_NAMES: "
        + ", ".join(f"«{n}» ({k}×)" for n, k in unregistered.most_common())
        + ".  Add them to the registry AND give every consumer grounded in it "
          "(export/markdown.py) a rule for each."
    )
