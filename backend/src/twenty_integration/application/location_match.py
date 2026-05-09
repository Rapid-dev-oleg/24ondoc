"""Cheap, deterministic resolver: find a Location.displayName inside a
free-form transcript string.

Why a separate module? The same fold/match rules are needed in three
places — the live ATS path, the Telegram path, and any future backfill —
so they live here, not on a use-case. Logic here is pure: no IO, no
external clients, no logger side-effects.

Rules (in order):
- latin↔cyrillic homoglyphs that Whisper typically emits in name
  numbers ("Аполо z7" → "Аполо з7", "Аспет m4" → "Аспет м4"),
- lowercase + whitespace collapse,
- leading zeros in numeric runs are stripped ("Аполо 06" matches
  "Аполо 6", and the same for any 0N),
- substring search with word boundaries,
- longest match wins,
- if several distinct Locations tie for the longest match we treat the
  result as ambiguous and return None (we never "guess").
"""
from __future__ import annotations

import re
from collections.abc import Iterable

# Whisper writes some Cyrillic letters as their Latin look-alikes when
# they sit next to digits in store codes ("Аполо z7", "Аспет m4"). Folding
# every glyph in this map to its Latin counterpart gives us a stable form
# to compare display names against transcripts.
_CYR_TO_LAT = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x",
    "у": "y", "к": "k", "м": "m", "т": "t", "в": "b", "н": "h",
    "А": "a", "Е": "e", "О": "o", "Р": "p", "С": "c", "Х": "x",
    "У": "y", "К": "k", "М": "m", "Т": "t", "В": "b", "Н": "h",
    "з": "z", "З": "z",
    "ё": "e", "Ё": "e",
})

_WHITESPACE_RE = re.compile(r"\s+")
_LEADING_ZEROS_RE = re.compile(r"\b0+(?=\d)")


def fold(text: str | None) -> str:
    """Normalize a string for tolerant Location.displayName comparison.

    Returns "" for empty/None inputs.
    """
    if not text:
        return ""
    folded = _WHITESPACE_RE.sub(" ", text.lower().translate(_CYR_TO_LAT)).strip()
    return _LEADING_ZEROS_RE.sub("", folded)


# Display names shorter than this (after folding) are excluded from the
# substring search — a 3-char name like "А1" produces too many false
# positives in long transcripts.
_MIN_FOLDED_NAME_LEN = 4


def match_displayname_in_text(
    text: str | None,
    candidates: Iterable[tuple[str, str]],
) -> str | None:
    """Pick a single displayName that occurs in the transcript.

    `candidates` is an iterable of (displayName, location_id) pairs.

    Returns the original displayName when exactly one Location matches at
    the longest fold-length. Returns None when there's no match, or when
    several Locations tie at the top length (ambiguous).
    """
    folded_text = fold(text)
    if not folded_text:
        return None

    # Pre-fold candidates and drop too-short ones.
    folded_candidates: list[tuple[str, str, str]] = []  # (folded, original, id)
    for original, loc_id in candidates:
        f = fold(original)
        if len(f) >= _MIN_FOLDED_NAME_LEN:
            folded_candidates.append((f, original, loc_id))
    if not folded_candidates:
        return None

    # (folded_len, displayName, loc_id) for each name that hits with a word
    # boundary on both sides.
    hits: list[tuple[int, str, str]] = []
    for folded_name, original, loc_id in folded_candidates:
        if _hit_with_boundaries(folded_text, folded_name):
            hits.append((len(folded_name), original, loc_id))
    if not hits:
        return None

    hits.sort(key=lambda h: h[0], reverse=True)
    top_len = hits[0][0]
    top = [h for h in hits if h[0] == top_len]
    distinct_ids = {h[2] for h in top}
    if len(distinct_ids) != 1:
        return None
    return top[0][1]


def _hit_with_boundaries(haystack: str, needle: str) -> bool:
    """True iff `needle` occurs in `haystack` flanked by non-alphanumerics
    (or start/end of string) on both sides — so that "Аполо 1" doesn't
    swallow "Аполо 12" and "Аспет 3" doesn't match the "3" inside "13".
    """
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx < 0:
            return False
        left_ok = idx == 0 or not haystack[idx - 1].isalnum()
        right_idx = idx + len(needle)
        right_ok = right_idx >= len(haystack) or not haystack[right_idx].isalnum()
        if left_ok and right_ok:
            return True
        start = idx + 1
