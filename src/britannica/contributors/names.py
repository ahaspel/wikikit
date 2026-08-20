"""Contributor NAME VOCABULARY — what is packaging and what is the person.

EB1911's contributor rolls wrap a name in styles, honorifics, peerages and
military ranks: `The Right Hon. Sir Mountstuart Elphinstone Grant Duff`,
`Major-General Sir Charles William Wilson`, `The Ven. William Cunningham`.
Anything that matches a contributor by name has to know which leading words are
packaging, and there was no shared answer — THREE copies, in two spellings:

  * `resolver._TITLE_RE` — anchored at the start, repeating, and it knew
    Prof/Dr/Mr/Mrs/Miss/Sir/Rev plus "The".  No ranks, no peerages.
  * `tools/db/dedup_contributors.py` and its own auditor
    `tools/diagnostics/contributor_dup_audit.py` — byte-identical to each other,
    matching anywhere, and they knew the ranks but not "The".

Each had learned the cases its own data threw at it.  Production held the
NARROWER one, and it was not merely missing matches: `_name_core_tokens` feeds
the first-name score, so an unstripped rank became the FIRST NAME.  73 of 1,508
contributors scored their first name as `Colonel`, `Captain`, `Admiral` or `Hon`
— a reference to "George Earl Church" scored ZERO against
`Colonel George Earl Church`.  Wrong information in the field the matcher scores
on, not a neutral gap.

THE VOCABULARY IS BUILT FROM THE ROLLS, not from a guess about what titles exist.
Every leading token across all 1,508 contributor names was enumerated and the
packaging separated from the given names — which is how the compound ranks
(`Rear-Admiral`, `Field-Marshal`, `Surgeon-Major`, `Lieut.-Colonel`), the styles
(`The`, `Right`, `Rt`, `Very`, `His Eminence`), the peerages (`Baron`, `Count`,
`Earl`) and the clerical ones (`Cardinal`, `Canon`, `Ven`) came in.  A guessed
list is what produced three partial ones ([[feedback_hard_means_unencoded_knowledge]]).

The compound ranks are why the qualifier group exists.  Without it `\\bAdmiral\\s+`
matches INSIDE `Rear-Admiral` and leaves `Rear-W. T. Sampson`, whose first name is
then `Rear-W` — worse than not stripping at all.

VERIFIED over the 1,508 names in the DB: 0 whose first token is still packaging,
0 stripped to nothing, 0 whose SURNAME changed, and 0 collisions — no two
contributors become indistinguishable, which is the route by which a wider strip
could have cost a false positive ([[feedback_contributor_zero_false_positives]]).
"""
from __future__ import annotations

import re

# A style may precede the title: "The Right Hon.", "The Very Rev.", "His Eminence".
_STYLE = r"(?:The\s+)?(?:(?:Right|Rt|Very|Most)\.?\s+)?(?:His\s+Eminence\s+)?"
# A rank may be compounded onto another: "Rear-Admiral", "Major-General",
# "Surgeon-Major", "Lieut.-Colonel", "Field-Marshal".
_RANK_QUALIFIER = (r"(?:(?:Rear|Vice|Lieut|Lieutenant|Lt|Major|Maj|Brigadier|Brig"
                   r"|Field|Surgeon|Sub)\.?[-\s])?")
_TITLES = (r"Prof(?:essor)?|Dr|Mr|Mrs|Miss|Sir|Dame|Rev(?:erend)?|Hon(?:ourable)?|"
           r"Lord|Lady|Baron|Baroness|Count|Countess|Earl|Duke|Cardinal|Canon|Bishop|"
           r"Ven(?:erable)?|Gen(?:eral)?|Col(?:onel)?|Capt(?:ain)?|Maj(?:or)?|"
           r"Lieut(?:enant)?|Lt|Adml|Admiral|Marshal|Commander|Sergeant|Sgt")

# ANCHORED at the start and REPEATING, which the resolver's copy had right and the
# dedup tools' did not.  Packaging PRECEDES a name; matching it anywhere eats real
# name tokens that happen to be title words — `Colonel George Earl Church` came
# back as `George Church`, because EARL is this man's middle name.  Repeating so a
# stack comes off together: `The Right Hon. Sir …`.
HONORIFIC_RE = re.compile(rf"^(?:{_STYLE}{_RANK_QUALIFIER}(?:{_TITLES})\.?\s+)+",
                          re.IGNORECASE)


def strip_honorifics(name: str) -> str:
    """``name`` with its styles, honorifics, peerages and ranks removed."""
    return HONORIFIC_RE.sub("", name or "").strip()


# The abbreviations the rolls use, folded to one spelling so `Col.` and `Colonel`
# compare equal.  Styles (`The`, `Right`, `Very`, `His Eminence`) are deliberately
# NOT here: they attach to rank and peerage rather than distinguishing anyone.
_CANON = {"col": "colonel", "capt": "captain", "gen": "general", "adml": "admiral",
          "lieut": "lieutenant", "lt": "lieutenant", "maj": "major",
          "rev": "reverend", "hon": "honourable", "prof": "professor",
          "dr": "doctor", "sgt": "sergeant", "brig": "brigadier",
          "ven": "venerable"}
_STYLE_WORDS = {"the", "right", "rt", "very", "most", "his", "eminence"}


def honorific_set(name: str) -> set:
    """The honorifics ``name`` carries, folded to canonical spellings.

    EVIDENCE, not packaging to discard.  "Colonel Church" names one of three
    contributors surnamed Church, and it is the only Colonel among them; a
    resolver that strips the rank to compare names has thrown away the one thing
    that identified him ([[feedback_forks_are_dropped_attributes]]).
    """
    m = HONORIFIC_RE.match(name or "")
    if not m:
        return set()
    out = set()
    for word in re.split(r"[\s.\-]+", m.group(0).strip().lower()):
        word = word.strip(".")
        if word and word not in _STYLE_WORDS:
            out.add(_CANON.get(word, word))
    return out


def normalize_name(name: str) -> str:
    """A contributor name reduced to its DEDUP KEY: packaging dropped,
    punctuation dropped, whitespace collapsed, lowercased."""
    s = HONORIFIC_RE.sub("", name or "")
    s = re.sub(r"[^\w\s]", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def normalize_initials_token(initials: str) -> str:
    """An initials token reduced to its key: lowercase, punctuation dropped
    (including curly apostrophes, which the rolls use inconsistently)."""
    return re.sub(r"[^\w]", "", initials or "").lower()
