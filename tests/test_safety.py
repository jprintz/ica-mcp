"""Säkerhetstester: exakt listmatchning, streckkodsvalidering, MCP-annotationer."""
import asyncio

import pytest

from ica_mcp import server
from ica_mcp.client import IcaClient, IcaError, validate_barcode

LISTS = [
    {"id": 1, "offlineId": "AAA-111", "title": "Handla"},
    {"id": 2, "offlineId": "BBB-222", "title": "Fest"},
    {"id": 3, "offlineId": "CCC-333", "title": "Helg"},
]


def make_client(lists=LISTS):
    c = IcaClient.__new__(IcaClient)  # hoppa över __init__ (ingen inloggning)
    c.get_lists = lambda: lists
    return c


@pytest.mark.parametrize("ref", ["", "   ", None])
def test_exact_rejects_empty(ref):
    with pytest.raises(IcaError):
        make_client().resolve_list(ref, exact=True)


@pytest.mark.parametrize("ref", ["", "  ", None])
def test_default_empty_is_primary(ref):
    assert make_client().resolve_list(ref)["title"] == "Handla"


def test_substring_default_vs_exact():
    c = make_client()
    assert c.resolve_list("es")["title"] == "Fest"  # oförändrat beteende
    with pytest.raises(IcaError):
        c.resolve_list("es", exact=True)


def test_exact_matches_title_id_offlineid():
    c = make_client()
    assert c.resolve_list("  fest ", exact=True)["id"] == 2
    assert c.resolve_list("3", exact=True)["title"] == "Helg"
    assert c.resolve_list(3, exact=True)["title"] == "Helg"
    assert c.resolve_list("aaa-111", exact=True)["title"] == "Handla"


def test_exact_duplicate_titles_raise():
    c = make_client(LISTS + [{"id": 4, "offlineId": "DDD-444", "title": "fest"}])
    with pytest.raises(IcaError, match="Flera"):
        c.resolve_list("Fest", exact=True)


@pytest.mark.parametrize("raw,clean", [("7310865001962", "7310865001962"),
                                       (" 7310 8650 01962 ", "7310865001962"),
                                       ("12345678", "12345678"),
                                       ("12345678901234", "12345678901234")])
def test_validate_barcode_ok(raw, clean):
    assert validate_barcode(raw) == clean


@pytest.mark.parametrize("bad", ["", "1234567", "123456789012345", "abc12345678",
                                 "../../x", "1234-5678", None, "١٢٣٤٥٦٧٨"])
def test_validate_barcode_bad(bad):
    with pytest.raises(IcaError):
        validate_barcode(bad)


def _no_http(*a, **k):
    raise AssertionError("HTTP-anrop gjordes")


def test_get_product_invalid_makes_no_http_call():
    c = IcaClient.__new__(IcaClient)
    c._api = _no_http
    with pytest.raises(IcaError):
        c.get_product("../admin")


def test_server_tools_handle_invalid_barcode(monkeypatch):
    c = IcaClient.__new__(IcaClient)
    c._api = _no_http
    monkeypatch.setattr(server, "client", lambda: c)
    r = server.get_product("abc")
    assert r["found"] is False and r["ean"] == "abc" and "streckkod" in r["message"]
    assert "streckkod" in server.add_product_to_shopping_list("abc")


def test_server_tools_surface_api_errors(monkeypatch):
    # Giltig kod men ICA svarar med fel (451, 5xx …) — ska inte se ut som "finns inte"
    c = IcaClient.__new__(IcaClient)

    def _fail(*a, **k):
        raise IcaError("HTTP 451 från ICA-gatewayen")
    c._api = _fail
    monkeypatch.setattr(server, "client", lambda: c)
    for tool in (server.get_product, server.add_product_to_shopping_list):
        with pytest.raises(IcaError, match="451"):
            tool("7310865004703")


def test_requirements_txt_matches_pyproject():
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent
    req = re.search(r"^mcp([^\s#]*)", (root / "requirements.txt").read_text(), re.M).group(1)
    assert f'"mcp{req}"' in (root / "pyproject.toml").read_text()


def test_delete_shopping_list_requires_exact(monkeypatch):
    c = make_client()
    c.delete_list = _no_http
    monkeypatch.setattr(server, "client", lambda: c)
    for bad in ["", "  ", "es"]:
        with pytest.raises(IcaError):
            server.delete_shopping_list(bad)


READ = {"list_shopping_lists", "view_shopping_list", "list_saved_recipes", "get_recipe",
        "random_recipes", "list_stores", "get_offers", "get_bonus", "get_product",
        "offers_on_my_list"}
DESTRUCTIVE = {"delete_shopping_list", "remove_item", "clear_checked"}


def test_tool_annotations():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert READ | DESTRUCTIVE <= set(tools)
    for name, t in tools.items():
        a = t.annotations
        assert a is not None, name
        if name in READ:
            assert a.readOnlyHint is True, name
        elif name in DESTRUCTIVE:
            assert a.readOnlyHint is False and a.destructiveHint is True, name
        else:
            assert a.readOnlyHint is False and a.destructiveHint is False, name
