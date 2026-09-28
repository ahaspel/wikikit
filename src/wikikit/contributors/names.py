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
import unicodedata

# A style may precede the title: "The Right Hon.", "The Very Rev.", "His Eminence".
_STYLE = r"(?:The\s+)?(?:(?:Right|Rt|Very|Most)\.?\s+)?(?:His\s+Eminence\s+)?"
# A rank may be compounded onto another: "Rear-Admiral", "Major-General",
# "Surgeon-Major", "Lieut.-Colonel", "Field-Marshal".
_RANK_QUALIFIER = (r"(?:(?:Rear|Vice|Lieut|Lieutenant|Lt|Major|Maj|Brigadier|Brig"
                   r"|Field|Surgeon|Sub)\.?[-\s])?")
_TITLES = (r"Prof(?:essor)?|Dr|Mr|Mrs|Miss|Sir|Dame|Rev(?:erend)?|Hon(?:ourable)?|"
           r"Lord|Lady|Baron|Baroness|Count|Countess|Earl|Duke|Cardinal|Canon|Bishop|"
           r"Monseigneur|Prince|"
           r"Ven(?:erable)?|Gen(?:eral)?|Col(?:onel)?|Capt(?:ain)?|Maj(?:or)?|"
           r"Lieut(?:enant)?|Lt|Adml|Admiral|Marshal|Commander|Sergeant|Sgt")
# `Monseigneur` (Duchesne) and `Prince` (Karageorgevitch, Kropotkin) were the only
# packaging still leading a name after the strip — re-enumerated over the rolls, the
# same way the list was built.  NOT added: `St`, which leads four names and is a
# GIVEN name in every one (`St George Lane Fox-Pitt`, `St George Jackson Mivart`,
# `St George Stock`) or a peerage (`Lord St Helier`).  Stripping it would do exactly
# what the header warns of — take the first name away and score on the surname.

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


def contributor_slug(initials: str) -> str:
    """The contributor's stable URL id, from EB1911's own identity token.

    THE SIGNATURE IS THE IDENTITY.  An article is attributed by the initials it
    is signed with, and the encyclopaedia kept those unique on purpose — where
    two contributors would have collided it starred one of them (`L. D.` and
    `L. D.*` are different men).  So the slug needs no invention: it is the
    signature, encoded for a URL.

    NOT `normalize_initials_token`, which is next door and looks like it would
    do.  That is a DEDUP key and is deliberately lossy — it drops every
    non-word character so that transcription drift collapses onto one form
    (`J. F.-K.` and `J. F. K.` both key to `jfk`, which is what lets the dedup
    pass consider them the same person).  A slug has the opposite job: it must
    keep apart everyone the source kept apart.  Same input, opposite contract —
    two functions, not one with a flag.

    WHAT MUST SURVIVE.  The rolls separate initials three ways, and all three
    are meaningful:

        space    `J. F. K.`   James Furman Kemp        ->  j-f-k
        hyphen   `J. F.-K.`   James Fitzmaurice-Kelly  ->  j-f_k
        nothing  `A. Sp.`     Archibald Sharp          ->  a-sp
                 `A. S.-P.`   Anthyme St Paul          ->  a-s_p

    The hyphen means "one hyphenated surname" and the space means "two names",
    so a slugifier that folds both to `-` merges four pairs of real people
    ([[feedback_forks_are_dropped_attributes]] — the separator is a source
    attribute, and flattening it is a loss).  Hence `_` for the hyphen: the
    only unreserved URL character left that reads cleanly.

    Everything else is safe to drop, because it never distinguishes anyone: the
    abbreviating period, and the apostrophes in the Scots and Irish forms, which
    the rolls write inconsistently anyway (`R. M‘L.`, `J. F. M'L.`, `E. O’N.`).
    Accents fold (`E. Hü.` -> `e-hu`). A star is a suffix, not punctuation, so
    it is spelled out rather than dropped.

    VERIFIED over the 1,508-name roster: 1,507 distinct slugs, all matching
    `[a-z0-9_-]+`, none empty.  The single collision was `L. D.*` claimed by two
    roster rows for one man — Louis Duchesne, entered once bare and once as
    `Monseigneur Louis Marie Olivier Duchesne`, both crediting the same
    biographical article and both claiming ADRIAN, BONIFACE, CLEMENT and
    DAMASUS.  An article carries ONE signature, so two rows holding the same one
    are two spellings of one person; merged through `contributor_aliases.json`.
    The uniqueness is an INVARIANT, gated at emit — see `resolve_contributors_post`.
    """
    s = initials or ""
    star = "-star" * s.count("*")
    tokens = []
    for tok in s.replace("*", "").split():
        t = unicodedata.normalize("NFKD", tok.lower()).replace("-", "_")
        t = re.sub(r"[^a-z0-9_]", "", t).strip("_")
        if t:
            tokens.append(t)
    return "-".join(tokens) + star
