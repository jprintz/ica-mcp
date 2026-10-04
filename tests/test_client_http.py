"""Offline-tester för IcaClient med mockad HTTP (ingen riktig ICA-trafik).

Täcker hela inloggningsflödet, token-återanvändning, 401-retry, geo-block,
allow_404 samt resolve_list/match_rows."""

import base64
import hashlib
import json

import pytest
from helpers import FakeResponse, valid_state

from ica_mcp.client import (
    API_BASE,
    AUTHORIZE_ENDPOINT,
    LOGIN_ENDPOINT,
    REGISTER_ENDPOINT,
    TOKEN_ENDPOINT,
    IcaAuthError,
    IcaClient,
    IcaError,
)

PASSWORD = "hemligt-lösen"
CLIENT = {"client_id": "cid", "client_secret": "csec", "scope": "sc"}


def _login_handler(login_html='<input type="hidden" name="token" value="SSO123">'):
    def handler(method, url, kw):
        data = kw.get("data") or {}
        if url == TOKEN_ENDPOINT and data.get("grant_type") == "client_credentials":
            return FakeResponse(json_data={"access_token": "BOOT"})
        if url == REGISTER_ENDPOINT:
            return FakeResponse(json_data=CLIENT)
        if url == AUTHORIZE_ENDPOINT and method == "GET":
            return FakeResponse(302, headers={"Location": "https://ims.test/login?state=ST42"})
        if url.startswith("https://ims.test/login"):
            return FakeResponse(200, text="<html></html>")
        if url == LOGIN_ENDPOINT:
            return FakeResponse(200, text=login_html)
        if url == AUTHORIZE_ENDPOINT and method == "POST":
            return FakeResponse(302, headers={"Location": "icacurity://app?code=CODE9&state=ST42"})
        if url == TOKEN_ENDPOINT and data.get("grant_type") == "authorization_code":
            return FakeResponse(json_data={"access_token": "ACC", "refresh_token": "REF",
                                           "expires_in": 900})
        return FakeResponse(500, text=f"oväntat anrop {method} {url}")
    return handler


# ------------------------------------------------------------------ inloggning
def test_full_login_flow(make_client):
    c = make_client(_login_handler(), password=PASSWORD)
    c.authenticate(force=True)

    # state sparat på disk med token + klient
    with open(c.state_file, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["client"] == CLIENT
    assert saved["token"]["access_token"] == "ACC"
    assert saved["token"]["refresh_token"] == "REF"
    assert "expiry" in saved["token"]

    # PKCE: verifier skickas i sista token-anropet och matchar challenge
    calls = c.session.calls
    _, final_url, final_kw = calls[-1]
    assert final_url == TOKEN_ENDPOINT
    assert final_kw["data"]["code"] == "CODE9"
    verifier = final_kw["data"]["code_verifier"]
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    authz = next(k for m, u, k in calls if u == AUTHORIZE_ENDPOINT and m == "GET")
    assert authz["params"]["code_challenge"] == expected

    # state från redirecten och SSO-token skickas vidare
    post_authz = next(k for m, u, k in calls if u == AUTHORIZE_ENDPOINT and m == "POST")
    assert post_authz["data"] == {"token": "SSO123", "state": "ST42"}


def test_password_only_sent_to_login_endpoint(make_client):
    c = make_client(_login_handler(), password=PASSWORD)
    c.authenticate(force=True)
    seen_login = False
    for _, url, kw in c.session.calls:
        if url == LOGIN_ENDPOINT:
            seen_login = True
            assert kw["data"]["password"] == PASSWORD
        else:
            assert PASSWORD not in json.dumps(kw, default=str), f"lösenord läckte till {url}"
    assert seen_login


def test_wrong_credentials_raises_auth_error(make_client):
    c = make_client(_login_handler(login_html="<html>Fel uppgifter</html>"))
    with pytest.raises(IcaAuthError):
        c.authenticate(force=True)


def test_no_credentials_and_no_state_raises(make_client):
    c = make_client(username=None, password=None)
    with pytest.raises(IcaAuthError):
        c.authenticate()
    assert c.session.calls == []


def test_valid_cached_token_is_reused_without_auth_calls(make_client):
    c = make_client(lambda m, u, k: FakeResponse(json_data={"shoppingLists": []}),
                    state=valid_state(access="CACHED"))
    c.get_lists()
    assert len(c.session.calls) == 1
    _, url, kw = c.session.calls[0]
    assert url.startswith(API_BASE)
    assert kw["headers"]["Authorization"] == "Bearer CACHED"


# --------------------------------------------------------------------- _api
def test_401_refreshes_once_and_retries(make_client):
    seen = {"api": 0}

    def handler(method, url, kw):
        if url == TOKEN_ENDPOINT:
            assert kw["data"]["grant_type"] == "refresh_token"
            return FakeResponse(json_data={"access_token": "NEW", "expires_in": 900})
        seen["api"] += 1
        if seen["api"] == 1:
            return FakeResponse(401)
        assert kw["headers"]["Authorization"] == "Bearer NEW"
        return FakeResponse(json_data={"shoppingLists": [{"title": "Handla"}]})

    c = make_client(handler, state=valid_state())
    assert c.get_lists() == [{"title": "Handla"}]
    assert seen["api"] == 2
    assert sum(1 for _, u, _ in c.session.calls if u == TOKEN_ENDPOINT) == 1


def test_second_401_raises_and_does_not_loop(make_client):
    def handler(method, url, kw):
        if url == TOKEN_ENDPOINT:
            return FakeResponse(json_data={"access_token": "NEW", "expires_in": 900})
        return FakeResponse(401, text="nej")

    c = make_client(handler, state=valid_state())
    with pytest.raises(IcaError):
        c.get_lists()
    api_calls = [x for x in c.session.calls if x[1].startswith(API_BASE)]
    assert len(api_calls) == 2


def test_451_raises_geo_block_error(make_client):
    c = make_client(lambda m, u, k: FakeResponse(451), state=valid_state())
    with pytest.raises(IcaError, match="geo-block"):
        c.get_lists()


def test_get_product_returns_none_on_404(make_client):
    c = make_client(lambda m, u, k: FakeResponse(404), state=valid_state())
    assert c.get_product("7310000000000") is None


def test_other_http_error_raises(make_client):
    c = make_client(lambda m, u, k: FakeResponse(500, text="trasigt"), state=valid_state())
    with pytest.raises(IcaError, match="500"):
        c.get_product("7310865004703")


# --------------------------------------------------------------- resolve_list
LISTS = [
    {"id": 1, "offlineId": "AAAA-1111", "title": "Handla"},
    {"id": 2, "offlineId": "BBBB-2222", "title": "Fest"},
    {"id": 3, "offlineId": "CCCC-3333", "title": "Festival-prylar"},
    {"id": 4, "offlineId": "DDDD-4444", "title": "Veckans middagar"},
]


@pytest.fixture
def client(make_client, monkeypatch):
    c = make_client()
    monkeypatch.setattr(c, "get_lists", lambda: list(LISTS))
    return c


@pytest.mark.parametrize("ref", [None, "", "  "])
def test_resolve_list_default_is_first(client, ref):
    assert client.resolve_list(ref)["title"] == "Handla"


def test_resolve_list_numeric_id(client):
    assert client.resolve_list(3)["title"] == "Festival-prylar"
    assert client.resolve_list("2")["title"] == "Fest"


def test_resolve_list_offline_id_case_insensitive(client):
    assert client.resolve_list("dddd-4444")["title"] == "Veckans middagar"


def test_resolve_list_exact_title_beats_substring(client):
    assert client.resolve_list("fest")["title"] == "Fest"


def test_resolve_list_unique_substring(client):
    assert client.resolve_list("middag")["title"] == "Veckans middagar"


def test_resolve_list_ambiguous_substring_raises(client):
    with pytest.raises(IcaError, match="Flera listor"):
        client.resolve_list("a")


def test_resolve_list_no_match_raises(client):
    with pytest.raises(IcaError, match="Ingen lista"):
        client.resolve_list("finnsinte")


def test_resolve_list_no_lists_raises(make_client, monkeypatch):
    c = make_client()
    monkeypatch.setattr(c, "get_lists", lambda: [])
    with pytest.raises(IcaError):
        c.resolve_list()


# ----------------------------------------------------------------- match_rows
ROWS = {"rows": [
    {"offlineId": "r-1", "productName": "Mjölk", "isStrikedOver": False},
    {"offlineId": "r-2", "productName": "Mjölk", "isStrikedOver": True},
    {"offlineId": "r-3", "productName": "Havremjölk", "isStrikedOver": False},
    {"offlineId": "mjolk", "productName": "Ost", "isStrikedOver": False},
]}


def test_match_rows_exact_name_before_substring():
    assert [r["offlineId"] for r in IcaClient.match_rows(ROWS, "mjölk")] == ["r-1", "r-2"]


def test_match_rows_offline_id_before_substring():
    # "mjolk" är ingen delsträng av något namn, men är offlineId för "Ost"
    assert [r["productName"] for r in IcaClient.match_rows(ROWS, "MJOLK")] == ["Ost"]


def test_match_rows_exact_name_before_offline_id():
    rows = {"rows": [
        {"offlineId": "x", "productName": "Ost"},
        {"offlineId": "ost", "productName": "Bröd"},
    ]}
    assert [r["offlineId"] for r in IcaClient.match_rows(rows, "ost")] == ["x"]


def test_match_rows_substring():
    assert [r["offlineId"] for r in IcaClient.match_rows(ROWS, "havre")] == ["r-3"]


def test_match_rows_unstruck_only():
    res = IcaClient.match_rows(ROWS, "mjölk", unstruck_only=True)
    assert [r["offlineId"] for r in res] == ["r-1"]
    only_struck = {"rows": [ROWS["rows"][1]]}
    assert IcaClient.match_rows(only_struck, "mjölk", unstruck_only=True) == []


def test_failed_refresh_falls_back_to_full_login(make_client):
    # refresh-token har gått ut hos ICA → full inloggning med lösenordet
    login = _login_handler()

    def handler(method, url, kw):
        if url == TOKEN_ENDPOINT and (kw.get("data") or {}).get("grant_type") == "refresh_token":
            return FakeResponse(400, json_data={"error": "invalid_grant"})
        return login(method, url, kw)

    c = make_client(handler, password=PASSWORD, state=valid_state(access="OLD", seconds=-60))
    c.authenticate()
    grants = [(k.get("data") or {}).get("grant_type") for m, u, k in c.session.calls if u == TOKEN_ENDPOINT]
    assert grants[0] == "refresh_token" and grants[-1] == "authorization_code"
    with open(c.state_file, encoding="utf-8") as f:
        assert json.load(f)["token"]["access_token"] == "ACC"
