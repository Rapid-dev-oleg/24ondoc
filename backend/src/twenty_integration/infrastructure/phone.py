"""Phone normalization for Russian numbers.

Twenty's PHONES composite stores the national part (no calling code) in
`primaryPhoneNumber` and the calling code separately in
`primaryPhoneCallingCode`. Any raw phone we receive (from ATS2, from an
operator, from a Telegram user) must be reduced to the same canonical
form before either writing or searching, otherwise filters miss matches
and we get duplicate Person/Location/CallRecord rows.

`normalize_ru_phone` returns exactly the 10-digit national part used by
Twenty's `phones.primaryPhoneNumber` filter and body. Anything that
can't be reduced to 10 digits yields None.
"""
from __future__ import annotations

import re

_DIGITS_RE = re.compile(r"\D+")


def normalize_ru_phone(raw: str | None) -> str | None:
    """Normalize a raw Russian phone to 10-digit national form.

    Accepts variants like '+79991234567', '79991234567', '89991234567',
    '9991234567', '+7 (999) 123-45-67'. Returns '9991234567' or None.
    """
    if not raw:
        return None
    digits = _DIGITS_RE.sub("", raw)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return digits


def to_phones_composite(national: str) -> dict[str, object]:
    """Build the PHONES composite payload for a normalized 10-digit RU number."""
    return {
        "primaryPhoneNumber": national,
        "primaryPhoneCountryCode": "RU",
        "primaryPhoneCallingCode": "+7",
        "additionalPhones": [],
    }
