"""Enhetstester för de rena (nätfria) hjälpfunktionerna i ica_mcp.client.

Dessa rör aldrig ICA:s API — de testar aggregering, formatering, matchning och
parsning. Live-API:t kan inte testas i CI (kräver svensk IP + inloggning)."""

import base64
import hashlib

import pytest

from ica_mcp.client import (
    IcaAuthError,
    IcaClient,
    _ensure_parent_dir,
    resolve_state_file,
)


def _recipe(*ingredients):
    return {"ingredientGroups": [{"ingredients": list(ingredients)}]}


# ----------------------------------------------------------- aggregate_ingredients
def test_aggregate_sums_same_name_and_unit():
    r1 = _recipe({"ingredient": "mjölk", "quantity": 8, "unit": "dl", "text": "8 dl mjölk"})
    r2 = _recipe({"ingredient": "mjölk", "quantity": 2, "unit": "dl", "text": "2 dl mjölk"})
    assert IcaClient.aggregate_ingredients([r1, r2]) == [
        {"name": "mjölk", "quantity": 10.0, "unit": "dl"}]


def test_aggregate_sums_unitless_count():
    r1 = _recipe({"ingredient": "ägg", "quantity": 4, "text": "4 ägg"})
    r2 = _recipe({"ingredient": "ägg", "quantity": 2, "text": "2 ägg"})
    assert IcaClient.aggregate_ingredients([r1, r2]) == [
        {"name": "ägg", "quantity": 6.0, "unit": "st"}]


def test_aggregate_keeps_distinct_lines_on_unit_mismatch():
    r1 = _recipe({"ingredient": "smör", "quantity": 50, "unit": "g", "text": "50 g smör"})
    r2 = _recipe({"ingredient": "smör", "quantity": 1, "unit": "msk", "text": "1 msk smör"})
    assert IcaClient.aggregate_ingredients([r1, r2]) == [
        {"name": "smör", "quantity": 50.0, "unit": "g"},
        {"name": "smör", "quantity": 1.0, "unit": "msk"}]


def test_aggregate_keeps_ingredient_without_text_or_quantity():
    # ICA anger "ingen mängd" som quantity 0.0; varan ska ändå med, en gång
    r1 = _recipe({"ingredient": "salt"})
    r2 = _recipe({"ingredient": "salt", "quantity": 0.0, "text": "salt"})
    assert IcaClient.aggregate_ingredients([r1, r2]) == [
        {"name": "salt", "quantity": None, "unit": None}]


def test_aggregate_normalizes_units():
    r = _recipe({"ingredient": "krossade tomater", "quantity": 2, "unit": "burk"},
                {"ingredient": "krossade tomater", "quantity": 1, "unit": "st"})
    assert IcaClient.aggregate_ingredients([r]) == [
        {"name": "krossade tomater", "quantity": 3.0, "unit": "st"}]


def test_aggregate_maps_unit_aliases():
    r = _recipe({"ingredient": "jäst", "quantity": 1, "unit": "pkt"},
                {"ingredient": "jäst", "quantity": 1, "unit": "förp"},
                {"ingredient": "mjölk", "quantity": 1, "unit": "liter"})
    assert IcaClient.aggregate_ingredients([r]) == [
        {"name": "jäst", "quantity": 2.0, "unit": "förp"},
        {"name": "mjölk", "quantity": 1.0, "unit": "l"}]


def test_aggregate_keeps_unknown_unit_in_name():
    # 3 klyftor vitlök får inte bli '3 st' och slås ihop med riktiga st
    r1 = _recipe({"ingredient": "vitlök", "quantity": 3, "unit": "klyftor"},
                 {"ingredient": "vitlök", "quantity": 1, "unit": "st"},
                 {"ingredient": "salt", "quantity": 1, "unit": "nypa"})
    r2 = _recipe({"ingredient": "vitlök", "quantity": 2, "unit": "klyftor"})
    assert IcaClient.aggregate_ingredients([r1, r2]) == [
        {"name": "vitlök (5 klyftor)", "quantity": None, "unit": None, "amount_in_name": True},
        {"name": "vitlök", "quantity": 1.0, "unit": "st"},
        {"name": "salt (1 nypa)", "quantity": None, "unit": None, "amount_in_name": True}]


def test_aggregate_text_fallback_has_no_extra_quantity():
    # utan ingrediensnamn används radtexten – mängden står redan där
    r = _recipe({"text": "2 dl grädde", "quantity": 2, "unit": "dl"})
    assert IcaClient.aggregate_ingredients([r]) == [
        {"name": "2 dl grädde", "quantity": None, "unit": None}]


def test_aggregate_empty():
    assert IcaClient.aggregate_ingredients([]) == []


# ----------------------------------------------------------- recipe helpers
def test_recipe_ingredient_texts_prefers_text():
    r = _recipe(
        {"text": "8 dl mjölk", "ingredient": "mjölk"},
        {"ingredient": "salt"},  # ingen text -> faller tillbaka på ingredient
    )
    assert IcaClient.recipe_ingredient_texts(r) == ["8 dl mjölk", "salt"]


def test_recipe_summary_maps_nested_portions():
    r = {"id": 1, "title": "Soppa", "cookingTime": "Under 30 minuter",
         "averageRating": 4.5, "details": {"portions": 4}}
    s = IcaClient.recipe_summary(r)
    assert s["id"] == 1 and s["title"] == "Soppa" and s["portions"] == 4 and s["rating"] == 4.5


# ----------------------------------------------------------- format_offer
def test_format_offer_builds_multi_part_deal():
    o = {"name": "Kaffe", "brand": "ICA", "isPersonal": True,
         "requiresLoyaltyCard": False, "category": {"articleGroupName": "Dryck"},
         "parsedMechanics": {"value1": "2 för", "value2": "115", "unitSign": ":-"}}
    f = IcaClient.format_offer(o)
    assert f["deal"] == "2 för 115:-"
    assert f["name"] == "Kaffe" and f["category"] == "Dryck" and f["personal"] is True


def test_format_offer_simple_price_and_missing_mechanics():
    assert IcaClient.format_offer(
        {"name": "X", "parsedMechanics": {"value1": "18", "unitSign": ":-"}}
    )["deal"] == "18:-"
    assert IcaClient.format_offer({"name": "Y"})["deal"] is None


# ----------------------------------------------------------- match_rows
def _list():
    return {"rows": [
        {"productName": "mjölk", "offlineId": "A", "isStrikedOver": False},
        {"productName": "lättmjölk", "offlineId": "B", "isStrikedOver": True},
        {"productName": "ägg", "offlineId": "C", "isStrikedOver": False},
    ]}


def test_match_rows_exact_beats_substring():
    m = IcaClient.match_rows(_list(), "mjölk")
    assert [r["offlineId"] for r in m] == ["A"]  # inte lättmjölk


def test_match_rows_substring_and_unstruck_filter():
    assert {r["offlineId"] for r in IcaClient.match_rows(_list(), "jölk")} == {"A", "B"}
    assert [r["offlineId"] for r in IcaClient.match_rows(_list(), "jölk", unstruck_only=True)] == ["A"]


def test_match_rows_by_offline_id():
    assert [r["offlineId"] for r in IcaClient.match_rows(_list(), "C")] == ["C"]


# ----------------------------------------------------------- _qs / _hidden
def test_qs_extracts_from_query_and_custom_scheme():
    assert IcaClient._qs("https://x/y?state=abc&code=def", "state") == "abc"
    assert IcaClient._qs("icacurity://app?code=xyz&state=q", "code") == "xyz"


def test_qs_raises_when_absent():
    with pytest.raises(IcaAuthError):
        IcaClient._qs("https://x/y", "state")


def test_hidden_extracts_both_attribute_orders():
    assert IcaClient._hidden('<input type="hidden" name="token" value="TT"/>', "token") == "TT"
    assert IcaClient._hidden('<input value="SS" name="state">', "state") == "SS"


def test_hidden_raises_when_absent():
    with pytest.raises(IcaAuthError):
        IcaClient._hidden("<form></form>", "token")


# ----------------------------------------------------------- _pkce
def test_pkce_challenge_is_s256_of_verifier():
    challenge, verifier = IcaClient._pkce()
    assert verifier.isalnum() and "=" not in challenge
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected


# ----------------------------------------------------------- state-file helpers
def test_resolve_state_file_honours_override(monkeypatch, tmp_path):
    target = tmp_path / "sub" / "auth.json"
    monkeypatch.setenv("ICA_STATE_FILE", str(target))
    assert resolve_state_file() == str(target)


def test_ensure_parent_dir_bare_filename_is_noop():
    _ensure_parent_dir("bare.json")  # får inte kasta (dirname == "")


def test_ensure_parent_dir_creates_dirs(tmp_path):
    p = tmp_path / "a" / "b" / "f.json"
    _ensure_parent_dir(str(p))
    assert (tmp_path / "a" / "b").is_dir()
