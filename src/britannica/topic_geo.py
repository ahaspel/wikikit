"""Geographic-context disambiguation for topic links.

The reader's-guide bucket path and the topic's own parenthetical both name the
place a geographic topic belongs to — "… > Spain > Towns", "Maryborough
(Queensland)" — and the correct article's lead says it in plain text ("a town of
Spain", "Queensland, Australia").  Matching the two is a FACT, not a proxy, so it
takes precedence over the embedding for geographic buckets.

`location_terms(path, topic)` yields weighted place terms (region/state above
country, expanding EB's abbreviations and country synonyms); `geo_score(lead,
terms)` scores a candidate lead against them.
"""
from __future__ import annotations

import re

# EB parenthetical abbreviations -> full place name (US states, AU/CA/UK regions).
_ABBREV = {
    "o.": "ohio", "ind.": "indiana", "ill.": "illinois", "mass.": "massachusetts",
    "n.j.": "new jersey", "n.y.": "new york", "pa.": "pennsylvania", "va.": "virginia",
    "w.va.": "west virginia", "n.c.": "north carolina", "s.c.": "south carolina",
    "ga.": "georgia", "md.": "maryland", "del.": "delaware", "conn.": "connecticut",
    "r.i.": "rhode island", "vt.": "vermont", "n.h.": "new hampshire", "me.": "maine",
    "wis.": "wisconsin", "mo.": "missouri", "ky.": "kentucky", "tenn.": "tennessee",
    "miss.": "mississippi", "ala.": "alabama", "la.": "louisiana", "ark.": "arkansas",
    "tex.": "texas", "cal.": "california", "calif.": "california", "col.": "colorado",
    "colo.": "colorado", "fla.": "florida", "kan.": "kansas", "kans.": "kansas",
    "neb.": "nebraska", "minn.": "minnesota", "mich.": "michigan", "ia.": "iowa",
    "ind. ter.": "indian territory", "wash.": "washington", "ore.": "oregon",
    "nev.": "nevada", "ariz.": "arizona", "mont.": "montana", "wyo.": "wyoming",
    "n.d.": "north dakota", "s.d.": "south dakota", "okla.": "oklahoma", "n.m.": "new mexico",
    "ont.": "ontario", "que.": "quebec", "b.c.": "british columbia", "man.": "manitoba",
    "n.s.": "nova scotia", "n.b.": "new brunswick",
    "n.s.w.": "new south wales", "vic.": "victoria", "q.": "queensland",
    "s.a.": "south australia", "w.a.": "western australia", "tas.": "tasmania",
    "salop": "shropshire", "hants": "hampshire", "oxon": "oxfordshire",
    "berks": "berkshire", "bucks": "buckinghamshire", "notts": "nottinghamshire",
    "lancs": "lancashire", "yorks": "yorkshire",
}

# Country / region bucket segment -> the strings a lead might use.  A segment not
# listed just contributes itself + its adjective (Spain -> spanish) via _adj.
_SYNONYMS = {
    "united states": ["united states", "u.s.a", "u.s.", "united states of america", "american"],
    "england and wales": ["england", "wales", "welsh", "english"],
    "united kingdom of great britain and ireland": ["england", "scotland", "ireland",
        "wales", "britain", "british", "english", "scottish", "irish", "welsh"],
    "canada and newfoundland": ["canada", "newfoundland"],
    "central america, mexico and west indies": ["mexico", "central america", "west indies"],
    "austria-hungary": ["austria", "hungary", "austrian", "hungarian", "bohemia", "moravia"],
    # Country -> its sub-regions, so a bucket that names the COUNTRY matches a lead
    # that names only a STATE/region within it.  Tasmania's lead says "Tasmania",
    # never "Australia" -- without this an "Australia > Towns" bucket misses it.
    "australia": ["australia", "tasmania", "queensland", "victoria",
                  "new south wales", "south australia", "western australia"],
}

# Bucket segments that carry no discriminating geography (categories, kinds, roots).
_GENERIC = {
    "geography", "history", "engineering", "biology", "art", "towns", "towns, etc",
    "towns, etc.", "towns, etc. (modern names)", "divisions", "divisions and towns",
    "countries (with division and towns)", "subjects", "general list", "general subjects and terms",
    "physical features", "natural history", "locomotion", "biographies",
    "europe", "europe (continental)", "america", "asia", "africa", "australasia",
    "oceania",
}

_PAREN = re.compile(r"\(([^)]*)\)")
_ADJ = {"spain": "spanish", "france": "french", "germany": "german", "italy": "italian",
        "russia": "russian", "portugal": "portuguese", "greece": "greek", "china": "chinese",
        "japan": "japanese", "sweden": "swedish", "norway": "norwegian", "denmark": "danish",
        "holland": "dutch", "belgium": "belgian", "switzerland": "swiss", "turkey": "turkish",
        "persia": "persian", "poland": "polish", "ireland": "irish", "scotland": "scottish",
        "england": "english", "india": "indian", "brazil": "brazilian", "peru": "peruvian"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _expand(term: str) -> list[str]:
    t = _norm(term)
    if t in _ABBREV:
        t = _ABBREV[t]
    out = _SYNONYMS.get(t, [t])
    if t in _ADJ:
        out = out + [_ADJ[t]]
    return out


# Recognized places, so the paren tier fires only on real geography — a paren
# like "(commune)" or "(or Foo)" is NOT a location and must not gate resolution.
_PLACES = set(_ABBREV.values()) | set(_ADJ) | {
    "queensland", "victoria", "new south wales", "south australia",
    "western australia", "tasmania", "ontario", "quebec", "british columbia",
    "manitoba", "nova scotia", "new brunswick", "newfoundland", "ireland",
    "scotland", "england", "wales",
}
for _syn in _SYNONYMS.values():
    _PLACES |= set(_syn)


def location_terms(path, topic: str) -> list[tuple[str, float]]:
    """Weighted place terms from the bucket path + the topic parenthetical.

    Finer/nearer terms (the topic paren, the deepest geographic bucket segment)
    outweigh the broad country, so 'Maryborough (Queensland)' beats the bare
    'Australia' bucket.  Weight FINE = paren state/region, ~1.0 = country segment.
    """
    terms: dict[str, float] = {}

    def add_term(e, w):
        if len(e) >= 3:
            terms[e] = max(terms.get(e, 0.0), w)

    # topic parenthetical — the sharpest signal, but ONLY when it names a place
    m = _PAREN.search(topic)
    if m:
        for part in re.split(r"[;,/]| or ", m.group(1)):
            for e in _expand(part):
                if e in _PLACES:
                    add_term(e, FINE)
    # bucket path — later (deeper) segments discriminate better than earlier ones
    segs = [s for s in path if _norm(s) not in _GENERIC]
    for i, seg in enumerate(segs):
        for e in _expand(seg):
            add_term(e, 1.0 + 0.4 * i / max(len(segs), 1))
    return sorted(terms.items(), key=lambda kv: -kv[1])


FINE = 2.0   # weight of a topic-parenthetical term (a specific state/region)


def geo_filter(path, topic: str, cands, lead_of):
    """Narrow candidates by geographic context.  Returns (winners, status):

      "pick"/"narrow" — winners are the geographically-correct candidates (the
          caller lets kind/embedding decide among them; often just one).
      "none"          — the bucket/topic carry no usable geography here.

    The COUNTRY is the bar and the state is a preference: a paren state is used
    when some candidate is in it, but if that state is absent we fall back to the
    country match, never a wrong-country homonym -- 'Waterford (Mass.)' with no
    Massachusetts article still lands on Waterford, N.Y., not the Irish city.
    """
    terms = location_terms(path, topic)
    if not terms:
        return list(cands), "none"
    fine = [(t, w) for t, w in terms if w >= FINE]
    score = {fn: geo_score(lead_of(fn), terms) for fn, _ in cands}
    if fine:
        fscore = {fn: geo_score(lead_of(fn), fine) for fn, _ in cands}
        if max(fscore.values(), default=0) > 0:                 # a lead is in that state
            best = max(score.values())
            return [ft for ft in cands if score[ft[0]] == best], "pick"
        # state named but absent -> fall through to the COUNTRY (coarse) match
    best = max(score.values(), default=0)
    if best <= 0:
        return list(cands), "none"
    return [ft for ft in cands if score[ft[0]] == best], "narrow"


def geo_score(lead: str, terms: list[tuple[str, float]]) -> float:
    """Sum of weights of location terms whose text appears in the lead's LOCATING
    HEAD.  A place article states its own country in the opening clause ("a town
    of Spain"); incidental mentions ("largely settled by Germans") come later, so
    scoring only the head stops Berlin-in-Ontario matching a Germany bucket."""
    low = " " + _norm(lead[:180]) + " "
    total = 0.0
    for term, w in terms:
        # word-ish boundary so 'york' doesn't fire inside 'yorkshire' spuriously,
        # but 'u.s.a' and multiword 'new south wales' still match as substrings.
        if term in low and (len(term) > 5 or re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", low)):
            total += w
    return total
