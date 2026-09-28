r"""The one owner of a shipped ``SHA256SUMS``.

Three modules wrote this file independently — ``build.py`` (the edition),
``windows.py`` (the enhanced package) and ``release.py`` (the download
directory) — and all three wrote it with ``Path.write_text`` in TEXT mode.  On
Windows that turns every ``\n`` into ``\r\n``, so ``sha256sum -c`` reads each
filename with a trailing ``\r`` and answers ``FAILED open or read`` for every
line.  The one file whose entire purpose is to be machine-checked could not be,
in the standard edition's own archive and in the enhanced one.

It survived every check because ``release.verify_archive`` parses with
``str.splitlines()``, which treats ``\r\n`` as a terminator and strips it: our
reader tolerated exactly the damage the customer's reader does not
([[feedback_verify_the_counter]] — the instrument agreed with itself).

Written here once, in BINARY, so there is no text mode left to get wrong.  That
also makes the release byte-reproducible off any platform, which matters because
the archives carry each other's checksums: a CRLF rebuild would change the
hashes without changing a word.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def sha(path) -> str:
    """SHA-256 of a file, STREAMED.

    Not ``digest(path.read_bytes())``: an ``.mdd`` is 450 MB and the caller is
    usually iterating a whole edition.  ``build.py`` did read them whole.
    """
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def format_checksums(digests) -> str:
    """The ``sha256sum``-format body from an ordered ``{name: digest}`` mapping.

    Separate from :func:`checksum_lines` because ``release.package_standard``
    already holds the digests — it hashes each member while streaming it into
    the new archive, so re-reading the files to format them would be a second
    pass over 570 MB.
    """
    return "".join(f"{value}  {name}\n" for name, value in digests.items())


def checksum_lines(directory, names) -> str:
    """The ``sha256sum``-format body for ``names`` inside ``directory``."""
    directory = Path(directory)
    return format_checksums({name: sha(directory / name) for name in names})


def write_checksums(directory, names) -> None:
    """Write ``directory/SHA256SUMS`` with LF endings on every platform."""
    directory = Path(directory)
    (directory / "SHA256SUMS").write_bytes(
        checksum_lines(directory, names).encode("utf-8"))


def write_shipped_text(path, text: str) -> None:
    """Write a shipped text artifact with LF endings on every platform.

    Same reproducibility reason as ``write_checksums``: these files are
    themselves listed in a ``SHA256SUMS``, so platform-dependent line endings
    would change the published hashes without changing the content.
    """
    Path(path).write_bytes(text.encode("utf-8"))
