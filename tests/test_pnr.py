"""Personnummer-normalisering: ICA kräver 12 siffror, användare skriver allt möjligt."""

import datetime

import pytest

from ica_mcp.client import normalize_personnummer

CURRENT_YY = datetime.date.today().year % 100


def test_twelve_digits_pass_through():
    assert normalize_personnummer("197801011234") == "197801011234"


def test_ten_digits_get_century():
    assert normalize_personnummer("7801011234") == "197801011234"


def test_hyphen_is_stripped():
    assert normalize_personnummer("780101-1234") == "197801011234"
    assert normalize_personnummer("19780101-1234") == "197801011234"


def test_spaces_are_stripped():
    assert normalize_personnummer(" 780101 1234 ") == "197801011234"


def test_recent_year_becomes_2000s():
    yy = f"{max(CURRENT_YY - 1, 0):02d}"
    assert normalize_personnummer(f"{yy}01011234").startswith("20")


def test_future_year_becomes_1900s():
    yy = f"{(CURRENT_YY + 5) % 100:02d}"
    assert normalize_personnummer(f"{yy}01011234").startswith("19")


def test_coordination_number_day_plus_60_survives():
    # Samordningsnummer: dagen är +60. Ska inte påverka sekelhärledningen.
    assert normalize_personnummer("7801611234") == "197801611234"


def test_none_and_empty():
    assert normalize_personnummer(None) is None
    assert normalize_personnummer("") is None


@pytest.mark.parametrize("odd", ["abc", "123"])
def test_odd_lengths_pass_through_digits_only(odd):
    out = normalize_personnummer(odd)
    assert out is None or out.isdigit()
