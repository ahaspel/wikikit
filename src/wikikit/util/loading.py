"""Refusing to run over a partial collection — the posture both readers share.

`export.corpus.load_corpus` (exported articles) and `source_pages.load_pages`
(raw wikisource pages) are the project's two total readers, and totality is the
whole of what they promise: collect EVERY failure with its reason, then raise
rather than hand back a subset that reads like a complete answer
([[feedback_honesty_surface_failures]]).

Writing that promise twice made it two promises.  The reason string
`"unreadable/unparseable: {}"` and the "N failed, here are the first 20" message
were byte-identical in both — the dup-constants ratchet caught the literal, which
is how you notice the PROCEDURE was duplicated too
([[project_duplicated_constant_campaign]]).  One owner, two nouns
([[feedback_tune_dont_fork]]).
"""
from __future__ import annotations

from pathlib import Path

_LISTED = 20


def unreadable(exc: Exception) -> str:
    """THE reason string for a payload that would not parse."""
    return f"unreadable/unparseable: {exc}"


class PartialLoadError(RuntimeError):
    """One or more members of a collection could not be loaded.

    ``noun`` names a member ("article payload", "raw source page") and
    ``refusing`` completes "refusing to …" with what a partial answer would
    corrupt.  Both appear in the message because a reader that only said "3
    failed" leaves the caller to guess whether continuing was an option.
    """

    def __init__(self, failures: list[tuple[Path, str]], *,
                 noun: str, refusing: str):
        self.failures = failures
        listed = "\n".join(f"    {p}: {why}" for p, why in failures[:_LISTED])
        more = ("" if len(failures) <= _LISTED
                else f"\n    … and {len(failures) - _LISTED} more")
        super().__init__(
            f"{len(failures)} {noun}(s) failed to load — refusing to "
            f"{refusing}:\n{listed}{more}")
