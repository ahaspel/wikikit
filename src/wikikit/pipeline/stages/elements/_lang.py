"""Language / script producer — `{{greek|…}}`, `{{polytonic|…}}`, `{{hebrew|…}}`, ….

A script wrapper carries no presentation we render: the Unicode glyphs ARE the
content and the browser shapes them, so the producer's whole job is to unwrap to
its content and recurse.  That is the SAME output the old `{}` registry strip
gave — but here it is an explicit DECISION ("the glyphs are the text"), scoped to
the known script names, not a blind strip-by-name.  A wrapper that is NOT a known
script never reaches this producer; it leaks, which is the signal it needs a
producer of its own.
"""
from __future__ import annotations


# `{{greek|…}}`/`{{polytonic|…}}`/… folded into the peel/recurse/wrap mechanism: the peel
# (`_recurse_slot_content`, LANG case) takes the post-name content and recurses it; the wrap
# is `_wrap_bare` (the glyphs ARE the text).  No bespoke producer.
