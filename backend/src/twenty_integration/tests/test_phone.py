"""Tests for normalize_ru_phone + to_phones_composite."""
from __future__ import annotations

import pytest

from twenty_integration.infrastructure.phone import (
    normalize_ru_phone,
    to_phones_composite,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+79991234567", "9991234567"),
        ("79991234567", "9991234567"),
        ("89991234567", "9991234567"),
        ("9991234567", "9991234567"),
        ("+7 (999) 123-45-67", "9991234567"),
        ("  +7-999-123-45-67  ", "9991234567"),
        ("tel:+79991234567", "9991234567"),
        # Non-mobile leading digit still accepted — РФ stationary codes exist.
        ("+74951234567", "4951234567"),
        ("84951234567", "4951234567"),
    ],
)
def test_normalize_known_formats(raw: str, expected: str) -> None:
    assert normalize_ru_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", None, "abc", "+7999", "123", "99912345678", "7999123456700"],
)
def test_normalize_rejects_garbage(raw: str | None) -> None:
    assert normalize_ru_phone(raw) is None


def test_composite_shape() -> None:
    assert to_phones_composite("9991234567") == {
        "primaryPhoneNumber": "9991234567",
        "primaryPhoneCountryCode": "RU",
        "primaryPhoneCallingCode": "+7",
        "additionalPhones": [],
    }
