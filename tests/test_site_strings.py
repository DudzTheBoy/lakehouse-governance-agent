"""The two language dictionaries in the portal must carry the same keys.

This exists because they drifted twice. The second time, eight keys were missing from
Portuguese and the page rendered the word "undefined" in place of every one of them,
in production, for a reader who switched language. Nothing failed and nothing warned:
a missing key in JavaScript is not an error, it is `undefined`, and `undefined`
renders happily into HTML.

The cause the second time was a patch that searched for an escaped accent while the
file held a real one, so every Portuguese insertion silently did nothing. A test that
compares the key sets catches that class of failure regardless of how it happens.
"""

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "build_site.py"
KEY = re.compile(r"^\s{4}([a-zA-Z][a-zA-Z0-9]*)\s*:", re.MULTILINE)


def dictionary(lang: str) -> dict[str, int]:
    """Keys of one language block in the page template, with their line numbers."""
    text = SRC.read_text(encoding="utf-8")
    start = text.index(f"\n  {lang}: {{")
    end = text.index("\n  },", start)
    block = text[start:end]
    offset = text[:start].count("\n")
    return {
        m.group(1): offset + block[: m.start()].count("\n") + 1
        for m in KEY.finditer(block)
    }


@pytest.fixture(scope="module")
def dicts():
    return dictionary("en"), dictionary("pt")


def test_both_dictionaries_are_found(dicts):
    en, pt = dicts
    assert len(en) > 20, "the English dictionary did not parse"
    assert len(pt) > 20, "the Portuguese dictionary did not parse"


def test_portuguese_has_every_english_key(dicts):
    en, pt = dicts
    missing = sorted(set(en) - set(pt))
    assert not missing, (
        f"{len(missing)} key(s) missing from the Portuguese dictionary, which render "
        f"as the literal text 'undefined' on the page: {missing}"
    )


def test_english_has_every_portuguese_key(dicts):
    en, pt = dicts
    missing = sorted(set(pt) - set(en))
    assert not missing, f"key(s) only in Portuguese: {missing}"


def test_no_key_is_declared_twice(dicts):
    text = SRC.read_text(encoding="utf-8")
    for lang in ("en", "pt"):
        start = text.index(f"\n  {lang}: {{")
        block = text[start : text.index("\n  },", start)]
        names = KEY.findall(block)
        duplicates = sorted({n for n in names if names.count(n) > 1})
        assert not duplicates, f"duplicate key(s) in `{lang}`: {duplicates}"
