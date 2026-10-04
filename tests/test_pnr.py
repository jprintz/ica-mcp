"""Personnummer-normalisering: ICA kräver 12 siffror, användare skriver allt möjligt.
Sekelhärledningen testas mot fasta datum, så att testerna inte beror på idag."""

import datetime

import pytest

from ica_mcp.client import normalize_personnummer

TODAY = datetime.date(2026, 10, 4)


def pnr(value, today=TODAY):
    return normalize_personnummer(value, today=today)


def test_twelve_digits_pass_through():
    assert pnr("197801011234") == "197801011234"


def test_ten_digits_get_century():
    assert pnr("7801011234") == "197801011234"


def test_hyphen_is_stripped():
    assert pnr("780101-1234") == "197801011234"
    assert pnr("19780101-1234") == "197801011234"


def test_spaces_are_stripped():
    assert pnr(" 780101 1234 ") == "197801011234"


@pytest.mark.parametrize("value, expected", [
    ("2501011234", "202501011234"),   # förra året
    ("2601011234", "202601011234"),   # i år
    ("2701011234", "192701011234"),   # nästa år = framtid → 1900-talet
    ("0001011234", "200001011234"),
    ("9912311234", "199912311234"),
])
def test_century_from_two_digit_year(value, expected):
    assert pnr(value) == expected


def test_coordination_number_day_plus_60_survives():
    # Samordningsnummer: dagen är +60. Ska inte påverka sekelhärledningen.
    assert pnr("7801611234") == "197801611234"


def test_none_and_empty():
    assert pnr(None) is None
    assert pnr("") is None


@pytest.mark.parametrize("odd", ["abc", "123"])
def test_odd_lengths_pass_through_digits_only(odd):
    out = pnr(odd)
    assert out is None or out.isdigit()


@pytest.mark.parametrize("value, expected", [
    ("261010+1234", "192610101234"),  # 100 år eller äldre: ett sekel tidigare
    ("261004+1234", "192610041234"),  # fyller 100 i dag
    ("270101+1234", "182701011234"),
    ("991231+1234", "189912311234"),
    ("000101+1234", "190001011234"),
    ("261070+1234", "192610701234"),  # samordningsnummer
])
def test_plus_separator_means_century_earlier(value, expected):
    assert pnr(value) == expected


def test_plus_ignored_for_twelve_digits():
    assert pnr("19261010+1234") == "192610101234"


def test_works_across_century_boundaries():
    assert pnr("9901011234", today=datetime.date(2099, 6, 1)) == "209901011234"
    assert pnr("0001011234", today=datetime.date(2100, 1, 1)) == "210001011234"
    assert pnr("9901011234", today=datetime.date(2100, 1, 1)) == "209901011234"


def test_defaults_to_today():
    yy = datetime.date.today().year % 100
    assert normalize_personnummer(f"{yy:02d}01011234").startswith(str(datetime.date.today().year // 100))
