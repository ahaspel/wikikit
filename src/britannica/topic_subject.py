"""Subject/field disambiguation for biography topics.

The reader's-guide bucket names the FIELD of a biography ("Geology > Biographies",
"Art > Painting > Biographies", "Medical Science > Biographies") and the article's
lead states the person's profession in a fixed slot right after the dates --
"SMITH, WILLIAM (1760-1839), English geologist, ...".  Matching the bucket field
to the lead profession is the same DIRECT fact the geo matcher uses for places,
so it disambiguates same-name people ('Smith, William' the geologist vs the actor
vs the sonneteer) without a proxy.

`field_terms(path)` yields the profession words the bucket implies; `field_score`
scores a candidate lead against them.
"""
from __future__ import annotations

import re

# Bucket category / subcategory  ->  profession words that appear in a lead.
# Keyed by the lower-cased bucket segment (matched by substring, so "Painting"
# under an "Art" path fires on the segment, and "medical" matches "Medical
# Science").  A person's lead names one of these right after the dates.
_FIELD = {
    "archaeology": ["archaeologist", "antiquary", "antiquarian", "assyriologist",
                    "egyptologist", "numismatist", "epigraphist", "orientalist"],
    "antiquities": ["antiquary", "antiquarian", "archaeologist"],
    "anthropology": ["anthropologist", "ethnologist", "ethnographer"],
    "ethnology": ["ethnologist", "anthropologist"],
    "painting": ["painter", "artist"],
    "sculpture": ["sculptor"],
    "architecture": ["architect"],
    "engraving": ["engraver"],
    "music": ["musician", "composer", "singer", "violinist", "pianist",
              "organist", "conductor", "soprano", "vocalist"],
    "stage": ["actor", "actress", "tragedian", "comedian", "dancer", "dramatist"],
    "dancing": ["actor", "actress", "dancer"],
    "art": ["painter", "sculptor", "artist", "architect"],
    "astronomy": ["astronomer"],
    "biology": ["biologist", "naturalist", "botanist", "zoologist", "physiologist"],
    "botany": ["botanist"],
    "zoology": ["zoologist", "naturalist"],
    "chemistry": ["chemist"],
    "economics": ["economist", "statistician", "sociologist"],
    "education": ["educationalist", "educator", "schoolmaster", "teacher", "paedagogue"],
    "engineering": ["engineer"],
    "geography": ["geographer", "explorer", "traveller"],
    "geology": ["geologist", "mineralogist", "palaeontologist"],
    "language": ["philologist", "grammarian", "lexicographer", "orientalist", "linguist"],
    "writing": ["philologist", "grammarian", "lexicographer"],
    "law": ["jurist", "lawyer", "judge", "advocate", "barrister"],
    "political science": ["statesman", "politician"],
    "literature": ["poet", "novelist", "dramatist", "author", "writer",
                   "playwright", "essayist", "sonneteer", "critic", "journalist"],
    "mathematics": ["mathematician", "geometer"],
    "medical": ["physician", "surgeon", "doctor", "anatomist", "pathologist",
                "physiologist", "oculist"],
    "military": ["general", "soldier", "officer", "marshal", "commander",
                 "field-marshal", "colonel", "captain"],
    "naval": ["admiral", "sailor", "naval", "commodore", "seaman"],
    "philosophy": ["philosopher"],
    "psychology": ["psychologist"],
    "physics": ["physicist", "natural philosopher"],
    "religion": ["theologian", "divine", "bishop", "priest", "saint", "cardinal",
                 "rabbi", "preacher", "missionary", "archbishop", "abbot", "monk",
                 "ecclesiastic", "clergyman", "dean"],
    "theology": ["theologian", "divine", "bishop", "priest", "saint"],
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def field_terms(path) -> list[str]:
    """Profession words implied by the bucket's category/subcategory segments."""
    out: dict[str, None] = {}
    for seg in path:
        low = _norm(seg)
        for key, words in _FIELD.items():
            if key in low:
                for w in words:
                    out[w] = None
    return list(out)


def field_score(lead: str, terms: list[str]) -> float:
    """1.5 per profession word present in the lead's HEAD (the is-a slot after the
    dates).  The weight sits above a lone country match (1.0) so field wins when
    nationality can't, e.g. three English Smiths split only by profession."""
    if not terms:
        return 0.0
    low = " " + _norm(lead[:200]) + " "
    hit = any(re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", low) for t in terms)
    return 1.5 if hit else 0.0


def field_hits(lead: str, terms: list[str]):
    low = " " + _norm(lead[:200]) + " "
    return [t for t in terms if re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", low)]
