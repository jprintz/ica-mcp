"""Produktregistret: exakt matchning, sökning, cache (minne + disk) och
koppling av varor till ICA-produkter. Nätfritt — hämtningen ersätts."""

import datetime as dt
import json

import pytest

from ica_mcp import products, server
from ica_mcp.client import IcaClient, IcaError, to_item
from ica_mcp.products import ARTICLE_GROUPS, CATEGORY_IDS, ProductCache, ProductCatalog, trim_article


def _a(id, name, parent=9, ext=None, status=2, plural="", category=None):
    """En produkt i ICA:s råformat (som /articles returnerar)."""
    return {"id": id, "name": name, "pluralName": plural, "parentId": parent,
            "parentIdExtended": ext or parent, "status": status,
            "maxiFormatCategoryName": category or "", "latestChange": "2017-07-06T15:48:58"}


ARTS = [
    _a(11103, "mjölk", 10, category="Mjölk"),
    _a(80178, "krossad tomat", 9, plural="krossade tomater", category="Grönsakskonserver"),
    _a(1002859, "Krossade tomater", 9),
    _a(11706, "tomat", 4, plural="tomater"),
    _a(10001, "havredryck", 9, status=0),   # lägre id men ej aktiv
    _a(10073, "havredryck", 9),
    _a(376552, "havredryck", 9, status=0),
    _a(10992, "lök", 4),
    _a(55192, "gul lök", 4),
    _a(50738, "majonäs", 9, status=3),       # finns bara med annan status
    _a(10318, "chèvre", 6, 13),
    _a(11014, "majskorn", 9),
    _a(10565, "gräddfil", 10),
]


TRIMMED = [trim_article(a) for a in ARTS]


@pytest.fixture
def catalog():
    return ProductCatalog(TRIMMED)


# ------------------------------------------------------------ exakt matchning
@pytest.mark.parametrize("name, expected", [
    ("mjölk", 11103),
    ("  MJÖLK ", 11103),              # skiftläge och mellanslag
    ("krossade tomater", 1002859),    # namn slår pluralnamn (80178)
    ("tomater", 11706),               # pluralnamn
    ("havredryck", 10073),            # status 2 före lägre id med status 0
    ("majonäs", 50738),               # bara icke-aktiv finns — används ändå
    ("gul lök", 55192),
])
def test_match_exact(catalog, name, expected):
    assert catalog.match(name)["id"] == expected


def test_match_ignores_notes_in_brackets(catalog):
    assert catalog.match("krossade tomater (à ca 400 g)")["id"] == 1002859
    assert catalog.match("(à 150 g) majskorn")["id"] == 11014
    assert catalog.match("(bara anteckning)") is None


@pytest.mark.parametrize("name", ["mjölkk", "krossade tomat", "lökar", "", "   ", None])
def test_match_never_guesses(catalog, name):
    assert catalog.match(name) is None


def test_get_by_id(catalog):
    assert catalog.get(80178)["name"] == "krossad tomat"
    assert catalog.get("80178")["name"] == "krossad tomat"
    assert catalog.get(1) is None and catalog.get("x") is None and catalog.get(None) is None


def test_catalog_skips_malformed_articles():
    c = ProductCatalog([{"id": None, "name": "x"}, {"id": 5, "name": ""}, trim_article(_a(6, "salt"))])
    assert len(c) == 1 and c.match("salt")["id"] == 6


# ------------------------------------------------------------ sökning
def _ids(results):
    return [a["id"] for a in results]


def test_search_exact_name_first_then_plural(catalog):
    assert _ids(catalog.search("krossade tomater", 2)) == [1002859, 80178]


def test_search_substring_finds_compounds(catalog):
    ids = _ids(catalog.search("tomat"))
    assert ids[0] == 11706 and 80178 in ids


def test_search_word_in_query(catalog):
    assert _ids(catalog.search("gul lök", 2)) == [55192, 10992]


def test_search_inflection_and_spelling(catalog):
    assert catalog.search("krossade tomat", 1)[0]["id"] in (80178, 1002859)
    assert catalog.search("chevre", 1)[0]["id"] == 10318


def test_search_no_hits_and_limits(catalog):
    assert catalog.search("swiffer thing") == []
    assert catalog.search("") == []
    assert len(catalog.search("havredryck", 1)) == 1
    assert len(catalog.search("tomat", 0)) == 1  # minst 1


def test_search_dedupes_products(catalog):
    ids = _ids(catalog.search("tomater"))
    assert len(ids) == len(set(ids))


def test_summary_and_trim():
    assert ProductCatalog.summary(TRIMMED[1]) == {
        "product_id": 80178, "name": "krossad tomat", "plural": "krossade tomater",
        "category": "Grönsakskonserver"}
    assert ProductCatalog.summary(trim_article(_a(1, "salt"))) == {"product_id": 1, "name": "salt"}
    raw = {"id": 1, "name": "x", "pluralName": "", "parentId": 9, "parentIdExtended": 9,
           "status": 2, "maxiFormatCategoryName": "", "supermarketFormatCategoryName": "Skafferi",
           "latestChange": "2017-01-01", "naraFormatCategoryId": "1"}
    t = trim_article(raw)
    assert t["category"] == "Skafferi" and "latestChange" not in t and "naraFormatCategoryId" not in t


# ------------------------------------------------------------ cache
class _Fetch:
    def __init__(self, result=ARTS):
        self.result, self.calls = result, 0

    def __call__(self):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def clock(monkeypatch):
    t = {"now": dt.datetime(2026, 10, 4, 12, tzinfo=dt.timezone.utc)}
    monkeypatch.setattr(products, "_now", lambda: t["now"])
    return t


def _write_cache(path, fetched, articles=ARTS):
    path.write_text(json.dumps({"version": 1, "fetched": fetched.isoformat(),
                                "articles": [trim_article(a) for a in articles]}), encoding="utf-8")


def test_cache_downloads_once_and_saves_trimmed(tmp_path, clock):
    f, path = _Fetch(), tmp_path / "products.json"
    pc = ProductCache(f, path=str(path))
    assert pc.get().match("mjölk")["id"] == 11103
    assert pc.get() is pc.get() and f.calls == 1  # minnet återanvänds
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["version"] == 1 and len(saved["articles"]) == len(ARTS)
    assert set(saved["articles"][0]) == {"id", "name", "pluralName", "parentId",
                                         "parentIdExtended", "status", "category"}
    assert not list(tmp_path.glob(".products.*"))  # inga temp-filer kvar


def test_fresh_disk_cache_needs_no_download(tmp_path, clock):
    path = tmp_path / "products.json"
    _write_cache(path, clock["now"] - dt.timedelta(hours=23))
    f = _Fetch()
    assert ProductCache(f, path=str(path)).get().match("tomater")["id"] == 11706
    assert f.calls == 0


def test_stale_disk_cache_is_refreshed(tmp_path, clock):
    path = tmp_path / "products.json"
    _write_cache(path, clock["now"] - dt.timedelta(hours=25), articles=[_a(1, "gammal")])
    f = _Fetch()
    c = ProductCache(f, path=str(path)).get()
    assert f.calls == 1 and c.match("mjölk") and not c.match("gammal")
    assert json.loads(path.read_text(encoding="utf-8"))["fetched"] == clock["now"].isoformat()


def test_failed_refresh_keeps_stale_cache_and_backs_off(tmp_path, clock):
    path = tmp_path / "products.json"
    _write_cache(path, clock["now"] - dt.timedelta(days=3))
    f = _Fetch(IcaError("HTTP 451"))
    pc = ProductCache(f, path=str(path))
    assert pc.get().match("mjölk") and f.calls == 1
    assert pc.get().match("mjölk") and f.calls == 1  # inget nytt försök direkt
    clock["now"] += products.RETRY_AFTER_FAILURE + dt.timedelta(seconds=1)
    pc.get()
    assert f.calls == 2


def test_no_cache_and_failure_gives_none(tmp_path, clock):
    f = _Fetch(ConnectionError("nere"))
    assert ProductCache(f, path=str(tmp_path / "p.json")).get() is None


def test_empty_download_counts_as_failure(tmp_path, clock):
    path = tmp_path / "p.json"
    assert ProductCache(_Fetch([]), path=str(path)).get() is None
    assert not path.exists()


def test_corrupt_or_old_format_cache_is_ignored(tmp_path, clock):
    path = tmp_path / "p.json"
    for content in ("{inte json", json.dumps({"version": 0, "fetched": "x", "articles": []})):
        path.write_text(content, encoding="utf-8")
        f = _Fetch()
        assert ProductCache(f, path=str(path)).get().match("mjölk") and f.calls == 1


def test_memory_cache_expires(tmp_path, clock):
    f = _Fetch()
    pc = ProductCache(f, path=str(tmp_path / "p.json"))
    pc.get()
    clock["now"] += products.CACHE_TTL + dt.timedelta(seconds=1)
    pc.get()
    assert f.calls == 2


def test_cache_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ICA_CACHE_DIR", str(tmp_path))
    assert products.resolve_cache_file() == str(tmp_path / "products.json")


# ------------------------------------------------------------ klienten
class _Client(IcaClient):
    """Klient utan auth: produktregistret från ARTS, /sync spelas in."""

    def __init__(self, tmp_path, fetch=None):
        self._products = ProductCache(fetch or _Fetch(), path=str(tmp_path / "p.json"))
        self.synced = []
        self.lists = [{"title": "Handla", "offlineId": "H"}]
        self.rows = []

    def get_lists(self):
        return self.lists

    def get_list_raw(self, offline_id):
        return {"title": "Handla", "offlineId": offline_id, "rows": self.rows}

    def _sync(self, offline_id, payload):
        self.synced.append(payload)
        return {}


def _items(*specs):
    return [to_item(s) for s in specs]


def test_link_exact_name(tmp_path):
    items = _items({"name": "Mjölk", "quantity": 1, "unit": "l"}, {"name": "swiffer"})
    res = _Client(tmp_path).link_products(items)
    assert res["available"] and res["linked"] == 1
    assert items[0]["product"]["id"] == 11103 and items[0]["name"] == "Mjölk"  # namnet behålls
    assert "product" not in items[1]
    assert res["unlinked"] == [{"name": "swiffer", "suggestions": []}]


def test_link_explicit_product_id_keeps_name(tmp_path):
    items = _items({"name": "tomater på burk", "product_id": 80178})
    res = _Client(tmp_path).link_products(items)
    assert res["linked"] == 1 and items[0]["product"]["id"] == 80178
    assert items[0]["name"] == "tomater på burk" and "product_id" not in items[0]


def test_link_unknown_product_id(tmp_path):
    items = _items({"name": "mjölk", "product_id": 999}, {"name": "krossade tomat", "product_id": 998})
    res = _Client(tmp_path).link_products(items)
    assert items[0]["product"]["id"] == 11103  # faller tillbaka på exakt namn
    u = res["unlinked"][0]
    assert u["name"] == "krossade tomat" and u["reason"] == "okänt produkt-id 998"
    assert {s["product_id"] for s in u["suggestions"]} & {80178, 1002859}
    assert len(u["suggestions"]) <= 3


def test_link_without_suggestions_skips_search(tmp_path, monkeypatch):
    c = _Client(tmp_path)
    catalog = c.product_catalog()
    monkeypatch.setattr(catalog, "search", lambda *a, **k: pytest.fail("ska inte söka"))
    res = c.link_products(_items({"name": "hemlig krydda"}), suggestions=0)
    assert res["unlinked"] == [{"name": "hemlig krydda", "suggestions": []}]


def test_link_without_catalog(tmp_path):
    items = _items({"name": "mjölk"})
    res = _Client(tmp_path, fetch=_Fetch(IcaError("451"))).link_products(items)
    assert res == {"available": False, "linked": 0, "unlinked": []}
    assert "product" not in items[0]


def test_add_rows_linked_and_free_text(tmp_path):
    c = _Client(tmp_path)
    items = _items({"name": "chèvre", "quantity": 1, "unit": "st"}, {"name": "swiffer"})
    c.link_products(items)
    c.add_rows("H", items)
    linked, free = c.synced[0]["createdRows"]
    assert (linked["sourceId"], linked["articleGroupId"], linked["articleGroupIdExtended"]) == (10318, 6, 13)
    assert free["sourceId"] < 0 and "articleGroupId" not in free


def test_to_item_product_id_validation():
    assert to_item({"name": "x", "product_id": 5})["product_id"] == 5
    for bad in (0, -1, True, "5", None, 1.5):
        assert "product_id" not in to_item({"name": "x", "product_id": bad})


def test_aggregate_merges_by_name_not_ingredient_id():
    # ICA:s ingredientId kan vara för grovt: krossade tomater → 'tomat' (färska).
    # Sammanslagning sker på namn; id:t följer bara med som reserv.
    r = {"ingredientGroups": [{"ingredients": [
        {"ingredient": "krossade tomater", "quantity": 500, "unit": "g", "ingredientId": 11706},
        {"ingredient": "tomater", "quantity": 200, "unit": "g", "ingredientId": 11706},
        {"ingredient": "tomater", "quantity": 100, "unit": "g", "ingredientId": 11706},
    ]}]}
    assert IcaClient.aggregate_ingredients([r]) == [
        {"name": "krossade tomater", "quantity": 500.0, "unit": "g", "fallback_product_id": 11706},
        {"name": "tomater", "quantity": 300.0, "unit": "g", "fallback_product_id": 11706}]


def test_aggregate_moves_package_word_to_unit():
    r = {"ingredientGroups": [{"ingredients": [
        {"ingredient": "förp majskorn (à 150 g)", "quantity": 1},
        {"ingredient": "förp", "quantity": 1},                     # inget namn efter → orört
        {"ingredient": "port ris", "quantity": 4},                 # portioner: namnet behålls
        {"ingredient": "majs", "quantity": 1, "unit": "g"},        # unit finns → orört
    ]}]}
    assert [(i["name"], i["quantity"], i["unit"]) for i in IcaClient.aggregate_ingredients([r])] == [
        ("majskorn (à 150 g)", 1.0, "förp"), ("förp", 1.0, "st"),
        ("port ris", 4.0, "st"), ("majs", 1.0, "g")]


# ------------------------------------------------------------ verktygen
@pytest.fixture
def fake(tmp_path, monkeypatch):
    c = _Client(tmp_path)
    monkeypatch.setattr(server, "_client", c)
    return c


def test_add_items_reports_unlinked_with_suggestions(fake):
    msg = server.add_items([server.Item(name="mjölk", quantity=1, unit="l"),
                            server.Item(name="krossade tomat", quantity=2, unit="st")])
    rows = fake.synced[0]["createdRows"]
    assert rows[0]["sourceId"] == 11103 and rows[1]["sourceId"] < 0
    assert "Ospecificerad" in msg and "krossade tomat" in msg and "[80178]" in msg
    assert "link_item" in msg  # talar om hur man sorterar


def test_add_items_all_linked_has_no_note(fake):
    msg = server.add_items([server.Item(name="mjölk"), server.Item(name="tomat", product_id=11706)])
    assert "Ospecificerad" not in msg
    assert [r["sourceId"] for r in fake.synced[0]["createdRows"]] == [11103, 11706]


def test_add_items_without_catalog_still_adds(tmp_path, monkeypatch):
    c = _Client(tmp_path, fetch=_Fetch(IcaError("451")))
    monkeypatch.setattr(server, "_client", c)
    msg = server.add_items([server.Item(name="mjölk")])
    assert c.synced[0]["createdRows"][0]["sourceId"] < 0
    assert "kunde inte hämtas" in msg


def test_search_products_tool(fake):
    res = server.search_products("krossade tomater", limit=2)
    assert [r["product_id"] for r in res] == [1002859, 80178]
    assert res[1]["category"] == "Grönsakskonserver"
    assert len(server.search_products("tomat", limit=999)) <= 25


def test_search_products_without_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_client", _Client(tmp_path, fetch=_Fetch(IcaError("451"))))
    with pytest.raises(IcaError):
        server.search_products("mjölk")


def test_add_product_links_by_article_id(fake, monkeypatch):
    monkeypatch.setattr(fake, "get_product", lambda ean: {
        "name": "Gräddfil Arla Ko", "articleId": 10565, "gtin": ean}, raising=False)
    msg = server.add_product_to_shopping_list("7310865004703")
    row = fake.synced[0]["createdRows"][0]
    assert row["productName"] == "Gräddfil Arla Ko" and row["sourceId"] == 10565
    assert "kopplad" in msg


def test_recipe_tool_name_first_then_ingredient_id(fake, monkeypatch):
    recipe = {"id": 1, "title": "Majsgryta", "ingredientGroups": [{"ingredients": [
        {"ingredient": "förp majskorn (à 150 g)", "quantity": 1, "ingredientId": 11014},
        {"ingredient": "krossade tomater (à ca 400 g)", "quantity": 2, "ingredientId": 11706},
        {"ingredient": "fast lök", "quantity": 1, "ingredientId": 10992},
        {"ingredient": "hemlig krydda", "quantity": 1, "unit": "tsk", "ingredientId": 999}]}]}
    monkeypatch.setattr(fake, "get_recipe", lambda rid: recipe, raising=False)
    msg = server.add_recipe_to_shopping_list(1)
    rows = [(r["productName"], r.get("unit"), r["sourceId"]) for r in fake.synced[0]["createdRows"]]
    assert rows[0] == ("majskorn (à 150 g)", "förp", 11014)
    assert rows[1] == ("krossade tomater (à ca 400 g)", "st", 1002859)  # namnet före id 11706
    assert rows[2] == ("fast lök", "st", 10992)  # ingen namnträff → ingredientId, namnet behålls
    assert rows[3][0] == "hemlig krydda" and rows[3][2] < 0  # okänt id → fritext
    assert "3 av 4 kopplade" in msg and "Ospecificerad: hemlig krydda" in msg


def test_row_view_shows_product_id():
    assert server._row_view({"productName": "mjölk", "sourceId": 11103})["product_id"] == 11103
    assert "product_id" not in server._row_view({"productName": "x", "sourceId": -5})


# ------------------------------------------------------------ avdelningar
def test_category_ids_match_ica():
    assert CATEGORY_IDS["Frukt & Grönt"] == 4 and CATEGORY_IDS["Hem & Fritid"] == 11
    assert CATEGORY_IDS["Skafferivaror"] == 9 and ARTICLE_GROUPS[12] == "Ospecificerad"
    assert "Ospecificerad" not in CATEGORY_IDS  # går inte att välja


def test_to_item_category_validation():
    assert to_item({"name": "x", "category": "Mejeri"})["category"] == "Mejeri"
    assert "category" not in to_item({"name": "x", "category": "Mejerier"})


def test_unlinked_item_with_category_is_sorted(fake, monkeypatch):
    catalog = fake.product_catalog()
    monkeypatch.setattr(catalog, "search", lambda *a, **k: pytest.fail("ska inte söka"))
    msg = server.add_items([server.Item(name="swiffer thing", category="Hem & Fritid")])
    row = fake.synced[0]["createdRows"][0]
    assert row["sourceId"] < 0 and row["articleGroupId"] == row["articleGroupIdExtended"] == 11
    assert "Fritext i vald avdelning: swiffer thing (Hem & Fritid)" in msg
    assert "Ospecificerad" not in msg


def test_product_beats_category(fake):
    server.add_items([server.Item(name="mjölk", category="Hem & Fritid")])
    row = fake.synced[0]["createdRows"][0]
    assert row["sourceId"] == 11103 and row["articleGroupId"] == 10


def test_link_item_with_product(fake):
    fake.rows = [{"productName": "fast potatis", "offlineId": "A", "sourceId": -5, "quantity": 900.0},
                 {"productName": "fast potatis", "offlineId": "B", "sourceId": -6},
                 {"productName": "mjölk", "offlineId": "C", "sourceId": 11103}]
    msg = server.link_item("fast potatis", product_id=11706)
    changed = fake.synced[0]["changedRows"]
    assert [r["offlineId"] for r in changed] == ["A", "B"]  # alla rader med namnet
    assert all((r["sourceId"], r["articleGroupId"]) == (11706, 4) for r in changed)
    assert changed[0]["quantity"] == 900.0 and changed[0]["productName"] == "fast potatis"
    assert "Frukt & Grönt" in msg


def test_link_item_with_category(fake):
    fake.rows = [{"productName": "swiffer thing", "offlineId": "A"}]
    msg = server.link_item("swiffer", category="Hem & Fritid")
    r = fake.synced[0]["changedRows"][0]
    assert (r["articleGroupId"], r["articleGroupIdExtended"]) == (11, 11) and "sourceId" not in r
    assert "Hem & Fritid" in msg


def test_link_item_argument_errors(fake):
    fake.rows = [{"productName": "x", "offlineId": "A"}]
    assert "antingen" in server.link_item("x")
    assert "antingen" in server.link_item("x", product_id=1, category="Mejeri")
    with pytest.raises(IcaError):
        server.link_item("x", product_id=424242)
    assert fake.synced == []


def test_row_view_shows_category():
    assert server._row_view({"productName": "m", "articleGroupId": 10})["category"] == "Mejeri"
    assert server._row_view({"productName": "x"})["category"] == "Ospecificerad"
    assert server._row_view({"productName": "x", "articleGroupId": 12})["category"] == "Ospecificerad"
