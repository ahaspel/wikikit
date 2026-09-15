"""Shipped checksum files must be readable by `sha256sum -c`, not just by us.

The defect these tests exist for: every `SHA256SUMS` in the downloads was written
with `Path.write_text` in TEXT mode, so on Windows each line ended `\r\n`.
`sha256sum -c` then reads the filename with a trailing `\r` and reports
"FAILED open or read" for every entry — the one file whose whole purpose is to be
machine-checked could not be, inside the customer's own archive.

It passed every existing check because `release.verify_archive` parses with
`str.splitlines()`, which strips `\r\n` — our reader tolerated exactly the damage
the customer's reader does not ([[feedback_verify_the_counter]]: the instrument
agreed with itself).  So these tests assert against a STRICT parser, the way
`sha256sum` actually reads the file, and a class-level guard keeps the next
shipped artifact from taking the unsafe path.
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from britannica.mdx.checksums import (
    format_checksums, sha, write_checksums, write_shipped_text)

MDX_PACKAGE = Path(__file__).resolve().parents[2] / "src" / "britannica" / "mdx"
# Every artifact that leaves the building inside a download.
SHIPPED_NAMES = ("SHA256SUMS", "README.md", "README.txt", "manifest.json",
                 "installation.json", "source-link-issues.json", "release.json")


def _strict_parse(text: str) -> dict:
    """Parse the way `sha256sum -c` does: split the RAW line, no CR tolerance."""
    entries = {}
    for line in text.split("\n"):
        if not line:
            continue
        expected, name = line.split("  ", 1)
        assert "\r" not in name, f"filename carries a stray CR: {name!r}"
        assert "\r" not in expected, f"digest carries a stray CR: {expected!r}"
        entries[name] = expected
    return entries


def test_write_checksums_is_lf_and_verifies_strictly(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"alpha")
    (tmp_path / "b.bin").write_bytes(b"beta")

    write_checksums(tmp_path, ["a.bin", "b.bin"])

    raw = (tmp_path / "SHA256SUMS").read_bytes()
    assert b"\r" not in raw, "SHA256SUMS must be LF on every platform"
    entries = _strict_parse(raw.decode("utf-8"))
    assert entries == {
        "a.bin": hashlib.sha256(b"alpha").hexdigest(),
        "b.bin": hashlib.sha256(b"beta").hexdigest(),
    }


def test_write_shipped_text_is_lf(tmp_path):
    write_shipped_text(tmp_path / "README.md", "one\ntwo\n")

    assert (tmp_path / "README.md").read_bytes() == b"one\ntwo\n"


def test_format_checksums_matches_sha256sum_layout():
    body = format_checksums({"x": "d" * 64})

    assert body == "d" * 64 + "  x\n"
    assert _strict_parse(body) == {"x": "d" * 64}


def test_sha_streams_and_matches_hashlib(tmp_path):
    blob = b"britannica" * 5000
    (tmp_path / "big.bin").write_bytes(blob)

    assert sha(tmp_path / "big.bin") == hashlib.sha256(blob).hexdigest()


def test_the_strict_parser_would_catch_the_regression(tmp_path):
    """Guard on the guard: CRLF must actually FAIL the check above.

    Without this, a future change that silently reintroduced text-mode writes
    could pass by making the assertions vacuous.
    """
    (tmp_path / "SHA256SUMS").write_bytes(("d" * 64 + "  a.bin\r\n").encode())

    # read_bytes, not read_text: text mode would strip the very CR under test.
    with pytest.raises(AssertionError):
        _strict_parse((tmp_path / "SHA256SUMS").read_bytes().decode("utf-8"))


@pytest.mark.parametrize("module", sorted(p.name for p in MDX_PACKAGE.glob("*.py")))
def test_no_shipped_artifact_uses_text_mode_write(module):
    """`write_text` opens in text mode; a shipped artifact must not take it.

    Class-level, not instance-level: the same bug was live in three modules at
    once (`build.py`, `windows.py`, `release.py`), which is the tell of a missing
    owner rather than three slips ([[feedback_whack_a_mole]]).  Use
    `write_shipped_text` / `write_checksums` instead.
    """
    source = (MDX_PACKAGE / module).read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "write_text"):
            continue
        segment = ast.get_source_segment(source, node) or ""
        hits = [n for n in SHIPPED_NAMES if n in segment]
        if hits:
            offenders.append((node.lineno, hits))
    assert not offenders, (
        f"{module} writes a shipped artifact through text mode: {offenders}. "
        "Use write_shipped_text/write_checksums so the bytes are LF everywhere.")
