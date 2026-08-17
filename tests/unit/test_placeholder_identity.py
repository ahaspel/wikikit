"""The element placeholder has ONE mint and ONE matcher.

`\\x03ELEM:N\\x03` is the token the walk hands between its own passes: the walker
mints one per extracted element, the classifier mints synthetic ROW/CELL/CAPTION
keys off the same counter, and producers substitute content back by matching it.
Three sites minted it and four matched it — two of those writing the `\\x03`
delimiter as a literal rather than asking the registry for `_PH`.

Nothing about that fails loudly.  A mint the matcher no longer recognises means
a producer builds its output without substituting a child, and the raw token
travels to export as bytes in an article body — AFRICA's territorial table did
exactly that.  So the ratchet is identity: every site must hold the SAME
function and the SAME compiled pattern, not merely agreeing copies.
"""
from __future__ import annotations

import britannica.pipeline.stages.elements as elements
import britannica.pipeline.stages.elements._classifier as classifier
import britannica.pipeline.stages.elements._indent as indent
import britannica.pipeline.stages.elements._walker as walker
from britannica.pipeline.stages.elements._registry import (
    PLACEHOLDER_RE, ElementRegistry, new_placeholder)


def test_every_minter_is_the_one_minter():
    assert walker.new_placeholder is new_placeholder
    assert classifier.new_placeholder is new_placeholder


def test_every_matcher_is_the_one_matcher():
    assert walker.PLACEHOLDER_RE is PLACEHOLDER_RE
    assert elements.PLACEHOLDER_RE is PLACEHOLDER_RE


def test_what_the_registry_mints_is_what_the_matcher_matches():
    """The round trip the walk depends on, asserted on the real registry."""
    key = ElementRegistry().add("POEM", "raw bytes")
    assert PLACEHOLDER_RE.fullmatch(key), repr(key)


def test_minted_keys_are_unique_across_registries():
    """The counter is module-global on purpose: per-instance counters gave an
    inner registry's ELEM:1 the same key as an outer one, and substitution
    swapped content between the two."""
    a, b = ElementRegistry(), ElementRegistry()
    keys = [a.add("X", "1"), b.add("X", "2"), new_placeholder()]
    assert len(set(keys)) == 3


def test_matcher_finds_a_placeholder_embedded_in_text():
    key = new_placeholder()
    found = PLACEHOLDER_RE.findall(f"lead {key} tail")
    assert found == [key]
