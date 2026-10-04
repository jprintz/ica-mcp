"""Nya listor kopplas till en butik (sortingStore) — annars visar ICA-appen
inga kategorier. Nätfritt: _api och butiksuppslag ersätts."""

import pytest

from ica_mcp.client import IcaClient, IcaError


class _Fake(IcaClient):
    def __init__(self, favorites=(9713, 1234), lists=()):  # ingen auth/state behövs
        self.favorites = list(favorites)
        self.lists = list(lists)
        self.posted = []

    def get_favorite_store_ids(self):
        return self.favorites

    def get_favorite_stores(self):
        names = {9713: "Maxi ICA Stormarknad Partille", 1234: "ICA Nära Hemma"}
        return [{"id": i, "name": names.get(i), "city": None} for i in self.favorites]

    def get_lists(self):
        return self.lists

    def _api(self, method, path, json_body=None, **kw):
        self.posted.append((method, path, json_body))

    def get_list_raw(self, offline_id):
        body = self.posted[-1][2]
        return {**body, "id": 1}


def test_store_id_defaults_to_primary_favorite():
    assert _Fake().store_id_for() == 9713
    assert _Fake().store_id_for("  ") == 9713


def test_store_id_by_name_or_id():
    assert _Fake().store_id_for("nära") == 1234
    assert _Fake().store_id_for("1234") == 1234


def test_store_id_without_favorites_is_zero():
    assert _Fake(favorites=()).store_id_for() == 0


def test_unknown_store_name_raises():
    with pytest.raises(IcaError):
        _Fake().store_id_for("Willys")


def test_create_list_sends_sorting_store():
    c = _Fake()
    L = c.create_list("Fest", store_id=9713)
    assert c.posted[0][2]["sortingStore"] == 9713 and L["sortingStore"] == 9713
    assert _Fake().create_list("Fest")["sortingStore"] == 0  # bakåtkompatibelt


def test_resolve_or_create_list_sets_store_only_when_creating():
    c = _Fake(lists=[{"title": "Veckans middagar", "offlineId": "X"}])
    assert c.resolve_or_create_list("veckans middagar")["offlineId"] == "X"
    assert c.posted == []  # befintlig lista — ingen butik ändras
    L = _Fake().resolve_or_create_list("Veckans middagar", "nära")
    assert L["sortingStore"] == 1234


# ------------------------------------------------------------ set_list_store
class _SyncRecorder(_Fake):
    def _sync(self, offline_id, payload):
        self.posted.append(("SYNC", offline_id, payload))
        return {}


def test_set_list_store_sends_changed_properties():
    c = _SyncRecorder()
    c.set_list_store("LIST-1", 1234)
    _, oid, payload = c.posted[0]
    props = payload["changedShoppingListProperties"]
    assert oid == "LIST-1" and props["sortingStore"] == 1234 and props["latestChange"]
    assert set(payload) == {"changedShoppingListProperties"}  # inga rader rörs


def test_set_list_store_tool(monkeypatch):
    from ica_mcp import server
    c = _SyncRecorder(lists=[{"title": "Handla", "offlineId": "H"}, {"title": "Fest", "offlineId": "F"}])
    monkeypatch.setattr(server, "_client", c)
    msg = server.set_list_store("fest", "nära")
    assert c.posted[-1][1] == "F"
    assert c.posted[-1][2]["changedShoppingListProperties"]["sortingStore"] == 1234
    assert "ICA Nära Hemma" in msg


def test_set_list_store_is_annotated_as_write():
    import asyncio
    from ica_mcp.server import mcp
    tool = next(t for t in asyncio.run(mcp.list_tools()) if t.name == "set_list_store")
    assert tool.annotations.readOnlyHint is False and tool.annotations.destructiveHint is False
