"""En rad per vara: att lägga till något som redan finns ökar raden i stället
för att skapa en dubblett, och receptandelarna förklarar varför (nätfritt)."""

import pytest

from ica_mcp import server
from ica_mcp.client import (
    IcaClient,
    IcaError,
    _same_item,  # noqa: PLC2701 — testas direkt
    merge_quantities,
    plan_additions,
    to_item,
)


# ------------------------------------------------------------ mängder
@pytest.mark.parametrize("a, b, expected", [
    ((2, "dl"), (3, "dl"), (5, "dl")),
    ((2, "dl"), (100, "ml"), (3, "dl")),          # större enheten vinner
    ((100, "ml"), (2, "dl"), (3, "dl")),
    ((2, "msk"), (1, "dl"), (1.3, "dl")),
    ((1, "tsk"), (1, "msk"), (4, "tsk")),         # 1,33 msk vore avrundat → mindre enheten
    ((1, "kg"), (5, "g"), (1.005, "kg")),         # små tillägg försvinner inte
    ((2, "l"), (1, "tsk"), (2.005, "l")),
    ((1, "kg"), (0.5, "g"), (1000.5, "g")),
    ((500, "g"), (1, "kg"), (1.5, "kg")),
    ((2, "hg"), (50, "g"), (2.5, "hg")),
    ((None, None), (1, "tsk"), (1, "tsk")),       # mängd saknas på ena sidan
    ((1, "tsk"), (None, None), (1, "tsk")),
    ((None, None), (None, None), (None, None)),
    ((2, "st"), (3, "st"), (5, "st")),
])
def test_merge_quantities(a, b, expected):
    assert merge_quantities(*a, *b) == expected


@pytest.mark.parametrize("a, b", [((50, "g"), (1, "msk")), ((1, "st"), (1, "förp")),
                                  ((1, "kg"), (1, "l")), ((2, "st"), (100, "g"))])
def test_incompatible_units(a, b):
    assert merge_quantities(*a, *b) is None


def test_every_volume_and_weight_unit_converts():
    from ica_mcp.client import UNITS
    volume = ["krm", "tsk", "msk", "ml", "cl", "dl", "l"]
    weight = ["g", "hg", "kg"]
    assert set(volume + weight + ["st", "förp"]) == set(UNITS)
    for group in (volume, weight):
        for a in group:
            for b in group:
                assert merge_quantities(1, a, 1, b) is not None, (a, b)


# ------------------------------------------------------------ samma vara?
def _p(pid):
    return {"id": pid, "parentId": 4, "parentIdExtended": 4}


def test_same_item_rules():
    assert _same_item({"name": "vitlöksklyfta", "product": _p(11802)},
                      {"name": "pressade vitlöksklyftor", "product": _p(11802)})  # samma produkt
    assert not _same_item({"name": "mjölk", "product": _p(1)}, {"name": "mjölk", "product": _p(2)})
    assert _same_item({"name": " Fast  Potatis"}, {"name": "fast potatis", "product": _p(3)})
    assert not _same_item({"name": "potatis"}, {"name": "sötpotatis"})


# ------------------------------------------------------------ planering
def _row(name, oid, qty=None, unit=None, src=-1, grp=None, struck=False, recipes=()):
    r = {"productName": name, "offlineId": oid, "sourceId": src, "isStrikedOver": struck,
         "recipes": list(recipes), "id": 1, "internalOrder": 0}
    if qty is not None:
        r["quantity"] = qty
    if unit:
        r["unit"] = unit
    if grp:
        r["articleGroupId"] = r["articleGroupIdExtended"] = grp
    return r


def _it(name, qty=None, unit=None, product=None, recipes=None, **kw):
    d = {"name": name, "quantity": qty, "unit": unit, **kw}
    if product:
        d["product"] = product
    if recipes:
        d["recipes"] = recipes
    return to_item(d)


def test_merges_into_existing_row_and_reports():
    rows = [_row("mjölk", "A", 1.0, "l", src=11103, grp=10)]
    new, changed, merges = plan_additions(rows, [_it("mjölk", 1, "l", product=_p(11103))])
    assert new == []
    (row,) = changed
    assert (row["offlineId"], row["quantity"], row["unit"]) == ("A", 2.0, "l")
    assert row["id"] == 1 and row["internalOrder"] == 0  # hela raden skickas
    assert merges == [{"name": "mjölk", "before": "1 l mjölk", "after": "2 l mjölk",
                       "items": ["mjölk"], "unsorted": False}]
    assert rows[0]["quantity"] == 1.0  # originalet orört


def test_checked_off_rows_are_not_merged_into():
    rows = [_row("mjölk", "A", 1.0, "l", struck=True)]
    new, changed, _ = plan_additions(rows, [_it("mjölk", 1, "l")])
    assert len(new) == 1 and changed == []


def test_unit_conversion_changes_row_unit():
    rows = [_row("grädde", "A", 100.0, "ml")]
    _, (row,), merges = plan_additions(rows, [_it("grädde", 2, "dl")])
    assert (row["quantity"], row["unit"]) == (3.0, "dl")
    assert merges[0]["after"] == "3 dl grädde"


def test_incompatible_unit_finds_other_row_or_creates_new():
    rows = [_row("smör", "A", 50.0, "g"), _row("smör", "B", 1.0, "msk")]
    new, (row,), _ = plan_additions(rows, [_it("smör", 2, "msk")])
    assert new == [] and row["offlineId"] == "B" and row["quantity"] == 3.0
    new, changed, _ = plan_additions([_row("smör", "A", 50.0, "g")], [_it("smör", 2, "msk")])
    assert len(new) == 1 and changed == []


def test_app_rows_without_unit_count_as_st():
    rows = [_row("tomater", "A", 4.0, None, src=11706)]  # appen: "4 tomater", ingen enhet
    _, (row,), _ = plan_additions(rows, [_it("tomater", 2, "st", product=_p(11706))])
    assert row["quantity"] == 6.0 and "unit" not in row  # appens utseende behålls


def test_row_without_amount_takes_new_amount():
    _, (row,), _ = plan_additions([_row("salt", "A")], [_it("salt", 1, "tsk")])
    assert (row["quantity"], row["unit"]) == (1.0, "tsk")


def test_recipe_shares_are_appended():
    rows = [_row("krossade tomater", "A", 1.0, "st", recipes=[{"id": 1, "quantity": 1.0, "unit": "st"}])]
    item = _it("krossade tomater", 2, "st", recipes=[{"id": 2, "quantity": 2.0, "unit": "st"}])
    _, (row,), _ = plan_additions(rows, [item])
    assert row["quantity"] == 3.0
    assert row["recipes"] == [{"id": 1, "quantity": 1.0, "unit": "st"}, {"id": 2, "quantity": 2.0, "unit": "st"}]


def test_merge_into_row_linked_by_product_with_other_name():
    rows = [_row("vitlöksklyfta", "A", 1.0, "st", src=11802, grp=4)]
    _, (row,), merges = plan_additions(rows, [_it("pressade vitlöksklyftor", 2, "st", product=_p(11802))])
    assert row["productName"] == "vitlöksklyfta" and row["quantity"] == 3.0
    assert merges[0]["after"] == "3 st vitlöksklyfta"


def test_free_text_row_gets_link_or_category():
    _, (row,), _ = plan_additions([_row("fast potatis", "A", 500.0, "g", grp=12)],
                                  [_it("fast potatis", 1, "kg", product=_p(11296))])
    assert (row["sourceId"], row["articleGroupId"], row["quantity"], row["unit"]) == (11296, 4, 1.5, "kg")
    _, (row,), _ = plan_additions([_row("swiffer thing", "A", grp=12)],
                                  [_it("swiffer thing", category="Hem & Fritid")])
    assert row["articleGroupId"] == 11 and row["sourceId"] == -1
    # en redan sorterad rad behåller sin avdelning — inget att skriva
    _, changed, merges = plan_additions([_row("diskborste", "A", src=1001117, grp=9)],
                                        [_it("diskborste", category="Hem & Fritid")])
    assert changed == [] and merges[0]["after"] == "diskborste"


def test_duplicates_within_one_call():
    new, changed, merges = plan_additions([], [_it("mjölk", 1, "l"), _it("Mjölk", 5, "dl")])
    assert changed == [] and merges == []
    assert [(i["name"], i["quantity"], i["unit"]) for i in new] == [("mjölk", 1.5, "l")]


def test_nothing_on_list_keeps_order():
    new, _, _ = plan_additions([], [_it("a"), _it("b"), _it("c")])
    assert [i["name"] for i in new] == ["a", "b", "c"]


# ------------------------------------------------------------ recept
def test_aggregate_converts_units_and_keeps_shares():
    r1 = {"id": 1, "ingredientGroups": [{"ingredients": [
        {"ingredient": "olja", "quantity": 2, "unit": "msk"}, {"ingredient": "salt", "quantity": 0.0}]}]}
    r2 = {"id": 2, "ingredientGroups": [{"ingredients": [
        {"ingredient": "olja", "quantity": 1, "unit": "dl"}, {"ingredient": "salt", "quantity": 1, "unit": "tsk"}]}]}
    olja, salt = IcaClient.aggregate_ingredients([r1, r2])
    assert (olja["quantity"], olja["unit"]) == (1.3, "dl")
    assert olja["recipes"] == [{"id": 1, "quantity": 2.0, "unit": "msk"}, {"id": 2, "quantity": 1.0, "unit": "dl"}]
    assert (salt["quantity"], salt["unit"]) == (1.0, "tsk")


# ------------------------------------------------------------ klient + verktyg
class _Fake(IcaClient):
    def __init__(self, rows=()):
        self.rows, self.synced = list(rows), []
        self.lists = [{"title": "Handla", "offlineId": "H"}]

    def product_catalog(self):
        return None

    def get_lists(self):
        return self.lists

    def get_list_raw(self, offline_id):
        return {"title": "Handla", "offlineId": offline_id, "rows": self.rows}

    def _sync(self, offline_id, payload):
        self.synced.append(payload)
        return {}


def test_add_or_merge_writes_once():
    c = _Fake([_row("mjölk", "A", 1.0, "l")])
    out = c.add_or_merge("H", [{"name": "mjölk", "quantity": 1, "unit": "l"}, {"name": "bröd"}])
    (payload,) = c.synced
    assert [r["productName"] for r in payload["createdRows"]] == ["bröd"]
    assert payload["changedRows"][0]["quantity"] == 2.0 and payload["changedRows"][0]["latestChange"]
    assert [i["name"] for i in out["created"]] == ["bröd"] and out["merged"][0]["after"] == "2 l mjölk"


def test_add_or_merge_only_changes():
    c = _Fake([_row("mjölk", "A", 1.0, "l")])
    c.add_or_merge("H", [{"name": "mjölk", "quantity": 1, "unit": "l"}])
    assert set(c.synced[0]) == {"changedRows"}
    with pytest.raises(IcaError):
        c.add_or_merge("H", [{"name": "  "}])


def test_add_items_reply_reports_merges(monkeypatch):
    c = _Fake([_row("mjölk", "A", 1.0, "l"), _row("fast potatis", "B", 500.0, "g", src=11296, grp=4)])
    monkeypatch.setattr(server, "_client", c)
    msg = server.add_items([server.Item(name="mjölk", quantity=1, unit="l"),
                            server.Item(name="fast potatis", quantity=1, unit="kg"),
                            server.Item(name="bröd")])
    assert "La till på 'Handla': bröd." in msg
    assert "1 l mjölk → 2 l mjölk" in msg and "500 g fast potatis → 1,5 kg fast potatis" in msg


def test_add_items_all_merged(monkeypatch):
    monkeypatch.setattr(server, "_client", _Fake([_row("mjölk", "A", 1.0, "l")]))
    msg = server.add_items([server.Item(name="mjölk", quantity=1, unit="l")])
    assert msg.startswith("Inga nya rader på 'Handla'.") and "→ 2 l mjölk" in msg


def test_unchanged_row_is_not_written():
    rows = [_row("gräddfil", "A", 1.5, "dl", src=10565, grp=10)]
    new, changed, merges = plan_additions(rows, [_it("Gräddfil Arla Ko", product=_p(10565))])
    assert new == [] and changed == []
    assert merges == [{"name": "gräddfil", "before": "1,5 dl gräddfil", "after": "1,5 dl gräddfil",
                       "items": ["Gräddfil Arla Ko"], "unsorted": False}]
    c = _Fake(rows)
    out = c.add_or_merge("H", [{"name": "Gräddfil Arla Ko", "product": _p(10565)}])
    assert c.synced == [] and out["merged"]  # inget anrop alls


def test_recipe_share_on_unchanged_amount_is_written():
    rows = [_row("salt", "A", 1.0, "tsk")]
    _, (row,), merges = plan_additions(rows, [_it("salt", recipes=[{"id": 9, "quantity": 0.0}])])
    assert row["recipes"] == [{"id": 9, "quantity": 0.0}] and merges[0]["before"] == merges[0]["after"]


def test_reply_marks_unchanged_amounts(monkeypatch):
    monkeypatch.setattr(server, "_client", _Fake([_row("salt", "A", 1.0, "tsk")]))
    msg = server.add_items([server.Item(name="salt")])
    assert "1 tsk salt (oförändrad mängd)" in msg


def test_amount_in_name_is_not_merged_by_product():
    # 'vitlök (3 klyftor)' kopplas till samma produkt som '1 st vitlök' men får
    # inte slås ihop – då försvinner klyftorna utan spår
    rows = [_row("vitlök", "A", 1, None, src=10500, grp=4)]
    item = {"name": "vitlök (3 klyftor)", "quantity": None, "unit": None,
            "amount_in_name": True, "product": _p(10500)}
    new, changed, merges = plan_additions(rows, [item])
    assert [i["name"] for i in new] == ["vitlök (3 klyftor)"]
    assert changed == [] and merges == []


# ------------------------------------------------------------ pålitliga kopplingar
class _Catalog:
    """Minimal ProductCatalog: match() på exakt namn."""
    def __init__(self, names):
        self.names = names

    def match(self, name):
        pid = self.names.get(" ".join(str(name).lower().split()))
        return {"id": pid} if pid else None


def test_fallback_link_does_not_merge_into_other_name():
    # recensionens scenario A: 'körsbärstomater på burk' fick tomatens id via ingredientId
    rows = [_row("tomater", "A", 4, None, src=11706, grp=4)]
    item = {**_it("körsbärstomater på burk", 2, "st", product=_p(11706)), "weak_link": True}
    new, changed, merges = plan_additions(rows, [item], _Catalog({"tomater": 11706}))
    assert [i["name"] for i in new] == ["körsbärstomater på burk"] and changed == [] and merges == []


def test_fallback_link_does_not_merge_within_one_call():
    # scenario B: båda raderna i samma recept
    a = _it("tomater", 4, "st", product=_p(11706))
    b = {**_it("körsbärstomater på burk", 2, "st", product=_p(11706)), "weak_link": True}
    new, _, merges = plan_additions([], [a, b])
    assert [i["name"] for i in new] == ["tomater", "körsbärstomater på burk"] and merges == []


def test_row_linked_by_fallback_does_not_swallow_other_name():
    # en äldre rad som kopplades via ingredientId: radens namn matchar inte produkten
    rows = [_row("körsbärstomater på burk", "A", 2, None, src=11706, grp=4)]
    new, changed, _ = plan_additions(rows, [_it("tomater", 4, "st", product=_p(11706))],
                                     _Catalog({"tomater": 11706}))
    assert [i["name"] for i in new] == ["tomater"] and changed == []


def test_trusted_links_with_other_names_still_merge_with_catalog():
    rows = [_row("vitlöksklyfta", "A", 1.0, "st", src=11802, grp=4)]
    _, (row,), _ = plan_additions(rows, [_it("pressade vitlöksklyftor", 2, "st", product=_p(11802))],
                                  _Catalog({"vitlöksklyfta": 11802}))
    assert row["quantity"] == 3.0


def test_fallback_link_with_same_name_still_merges():
    rows = [_row("körsbärstomater på burk", "A", 1, "st")]
    item = {**_it("körsbärstomater på burk", 2, "st", product=_p(11706)), "weak_link": True}
    _, (row,), _ = plan_additions(rows, [item])
    assert row["quantity"] == 3.0


def test_link_flags_survive_to_item():
    from ica_mcp.client import to_item
    it = to_item({"name": "x", "weak_link": True, "amount_in_name": True})
    assert it["weak_link"] is True and it["amount_in_name"] is True
    assert "weak_link" not in to_item({"name": "x", "weak_link": "yes"})


def test_names_match_across_unicode_forms():
    import unicodedata
    nfd = unicodedata.normalize("NFD", "mjölk")
    assert nfd != "mjölk" and _same_item({"name": nfd}, {"name": "MJÖLK"})


@pytest.mark.parametrize("shares", [[{"quantity": 1}], [{"id": 7, "quantity": None}], ["x"]])
def test_odd_server_shares_do_not_crash(shares):
    rows = [_row("mjölk", "A", 1, "l", recipes=shares)]
    _, (row,), _ = plan_additions(rows, [_it("mjölk", 1, "l")])
    assert row["quantity"] == 2.0
    assert all(isinstance(s, dict) and s.get("id") for s in row["recipes"])


def test_merge_into_unsorted_row_is_flagged():
    rows = [_row("swiffer thing", "A", grp=12)]
    _, _, (m,) = plan_additions(rows, [_it("Swiffer Thing")])
    assert m["unsorted"] is True and m["items"] == ["Swiffer Thing"]


# ------------------------------------------------------------ hela vägen genom verktygen
def _real_catalog():
    from ica_mcp.products import ProductCatalog, trim_article
    arts = [{"id": 11706, "name": "tomat", "pluralName": "tomater", "parentId": 4,
             "parentIdExtended": 4, "status": 2},
            {"id": 10500, "name": "vitlök", "pluralName": "", "parentId": 4,
             "parentIdExtended": 4, "status": 2}]
    return ProductCatalog([trim_article(a) for a in arts])


class _CatFake(_Fake):
    def __init__(self, rows=()):
        super().__init__(rows)
        cat = _real_catalog()
        self._products = type("C", (), {"get": staticmethod(lambda: cat)})()

    def product_catalog(self):
        return self._products.get()


def _recipe(rid, *ings):
    return {"id": rid, "title": "R", "ingredientGroups": [{"ingredients": list(ings)}]}


def test_recipe_fallback_does_not_swallow_existing_row(monkeypatch):
    c = _CatFake([_row("tomater", "A", 4, None, src=11706, grp=4)])
    monkeypatch.setattr(server, "_client", c)
    monkeypatch.setattr(c, "get_recipe", lambda rid: _recipe(rid, {
        "ingredient": "körsbärstomater på burk", "quantity": 2, "unit": "st", "ingredientId": 11706}),
        raising=False)
    server.add_recipe_to_shopping_list(1)
    (payload,) = c.synced
    assert [r["productName"] for r in payload["createdRows"]] == ["körsbärstomater på burk"]
    assert "changedRows" not in payload


def test_unknown_unit_amount_survives_add_or_merge():
    # flaggan måste överleva to_item i add_or_merge, annars slukas klyftorna av 'vitlök'-raden
    c = _CatFake([_row("vitlök", "A", 1, None, src=10500, grp=4)])
    items = IcaClient.aggregate_ingredients([_recipe(1, {
        "ingredient": "vitlök", "quantity": 3, "unit": "klyftor"})])
    c.link_products(items, suggestions=0)
    out = c.add_or_merge("H", items)
    assert [i["name"] for i in out["created"]] == ["vitlök (3 klyftor)"] and out["merged"] == []


def test_merge_false_always_adds_rows(monkeypatch):
    c = _Fake([_row("mjölk", "A", 1.0, "l")])
    monkeypatch.setattr(server, "_client", c)
    msg = server.add_items([server.Item(name="mjölk", quantity=1, unit="l")], merge=False)
    (payload,) = c.synced
    assert set(payload) == {"createdRows"} and "La till på 'Handla': 1 l mjölk." in msg


def test_unlinked_item_merged_into_unsorted_row_is_reported(monkeypatch):
    c = _CatFake([_row("swiffer thing", "A", grp=12)])
    monkeypatch.setattr(server, "_client", c)
    msg = server.add_items([server.Item(name="swiffer thing")])
    assert "Ospecificerad" in msg and "swiffer thing" in msg.split("Ospecificerad")[-1]
