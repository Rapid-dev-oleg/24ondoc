"""Tests for the location_match module — the cheap fold-based resolver."""
from __future__ import annotations

from twenty_integration.application.location_match import (
    fold,
    match_displayname_in_text,
)


class TestFold:
    def test_empty_inputs(self):
        assert fold(None) == ""
        assert fold("") == ""
        assert fold("   ") == ""

    def test_lowercase_and_whitespace(self):
        # We fold only homoglyph-prone Cyrillic letters (а/о/е/р/...),
        # not every Cyrillic char. That's enough for tolerant comparison
        # because the same fold runs on both sides.
        assert fold("  Аполо   32  ") == "aпoлo 32"

    def test_homoglyphs_cyr_to_lat(self):
        # а→a, п stays, о→o, л stays, о→o
        assert fold("Аполо") == "aпoлo"
        # а→a, с→c, п stays, е→e, т→t
        assert fold("Аспет") == "acпet"
        # 'з' is treated like Latin 'z' so "z7" and "з7" coincide
        assert fold("Аполо з7") == fold("Аполо z7") == "aпoлo z7"

    def test_zero_strip_in_numbers(self):
        assert fold("Аполо 06") == "aпoлo 6"
        assert fold("Аполо 6") == "aпoлo 6"
        assert fold("Аполо 002") == "aпoлo 2"
        # trailing zero is preserved
        assert fold("100") == "100"
        # only-zero stays (one digit must remain after the leading zeros)
        assert fold("00") == "0"

    def test_idempotent(self):
        once = fold("Аполо 06")
        assert fold(once) == once


class TestMatchDisplayname:
    def _cands(self, *pairs):
        return list(pairs)

    def test_no_match(self):
        cands = self._cands(("Аполо 32", "loc-32"), ("Аспет 5", "loc-5"))
        assert match_displayname_in_text(
            "обычный диалог без названия точки", cands,
        ) is None

    def test_simple_match(self):
        cands = self._cands(("Аполо 32", "loc-32"))
        assert match_displayname_in_text(
            "звонок из Аполо 32 по поводу кассы", cands,
        ) == "Аполо 32"

    def test_zero_strip_match(self):
        # Catalog has "Аполо 06"; speaker said "Аполо 6"
        cands = self._cands(("Аполо 06", "loc-06"))
        assert match_displayname_in_text(
            "это аполо 6 беспокоит, у нас касса", cands,
        ) == "Аполо 06"

    def test_latin_cyrillic_swap(self):
        # Whisper wrote "z7" (Latin), catalog has "з7" (Cyrillic)
        cands = self._cands(("Аполо з7", "loc-z7"))
        assert match_displayname_in_text(
            "здравствуйте, аполо z7 беспокоит", cands,
        ) == "Аполо з7"

    def test_longest_wins(self):
        # "Аполо 1" prefix of "Аполо 12" — "12" must beat "1"
        cands = self._cands(
            ("Аполо 1", "loc-1"),
            ("Аполо 12", "loc-12"),
        )
        assert match_displayname_in_text(
            "звоним из аполо 12, не работает чек", cands,
        ) == "Аполо 12"

    def test_word_boundary_avoids_partial(self):
        # Catalog has "Аполо 1"; transcript says "Аполо 12"
        # → "Аполо 1" must NOT match (boundary check)
        cands = self._cands(("Аполо 1", "loc-1"))
        assert match_displayname_in_text(
            "звоним из Аполо 12", cands,
        ) is None

    def test_ambiguous_returns_none(self):
        # Two distinct Locations whose folded names happen to equal each
        # other shouldn't pick a winner — we never guess.
        cands = self._cands(
            ("Аполо 06", "loc-a"),
            ("Аполо 6", "loc-b"),
        )
        assert match_displayname_in_text(
            "это аполо 6", cands,
        ) is None

    def test_short_names_skipped(self):
        # "А1" folds to "a1" (len=2) — under threshold, ignored
        cands = self._cands(("А1", "loc-tiny"))
        assert match_displayname_in_text(
            "случайный текст a1 b2 c3", cands,
        ) is None

    def test_whisper_substitutions_not_in_module(self):
        # We DON'T fold "Поло" or "Аспек" — those are AI's job. Confirm
        # behaviour: catalog "Аполо" + transcript "Поло" must NOT match.
        cands = self._cands(("Аполо 32", "loc-32"))
        assert match_displayname_in_text(
            "это поло 32, у нас проблема", cands,
        ) is None
