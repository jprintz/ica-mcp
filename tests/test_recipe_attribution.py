"""Receptandelar ("Tillagd från recept" i appen): ICA:s recipes-fält på raden,
[{id, quantity, unit?}] — samma format som appen själv skriver."""

from ica_mcp import server
from ica_mcp.client import IcaClient, add_recipe_share, to_item


def _recipe(rid, *ingredients):
    return {"id": rid, "title": f"Recept {rid}", "ingredientGroups": [{"ingredients": list(ingredients)}]}


def test_shares_per_recipe_when_aggregating():
    r1 = _recipe(1, {"ingredient": "krossade tomater", "quantity": 1, "unit": "st"},
                 {"ingredient": "salt", "quantity": 0.0})
    r2 = _recipe(2, {"ingredient": "krossade tomater", "quantity": 2, "unit": "st"},
                 {"ingredient": "salt"})
    tomater, salt = IcaClient.aggregate_ingredients([r1, r2])
    assert tomater["quantity"] == 3.0
    assert tomater["recipes"] == [{"id": 1, "quantity": 1.0, "unit": "st"},
                                  {"id": 2, "quantity": 2.0, "unit": "st"}]
    # ingen mängd: som appen skriver det, quantity 0.0 och ingen enhet
    assert salt["recipes"] == [{"id": 1, "quantity": 0.0}, {"id": 2, "quantity": 0.0}]


def test_same_ingredient_twice_in_one_recipe_is_one_share():
    r = _recipe(7, {"ingredient": "smör", "quantity": 2, "unit": "msk"},
                {"ingredient": "smör", "quantity": 1, "unit": "msk"})
    (smor,) = IcaClient.aggregate_ingredients([r])
    assert smor["recipes"] == [{"id": 7, "quantity": 3.0, "unit": "msk"}]


def test_unit_mismatch_gives_separate_items_with_own_shares():
    r = _recipe(3, {"ingredient": "smör", "quantity": 50, "unit": "g"},
                {"ingredient": "smör", "quantity": 1, "unit": "msk"})
    a, b = IcaClient.aggregate_ingredients([r])
    assert a["recipes"] == [{"id": 3, "quantity": 50.0, "unit": "g"}]
    assert b["recipes"] == [{"id": 3, "quantity": 1.0, "unit": "msk"}]


def test_recipe_without_id_has_no_shares():
    (it,) = IcaClient.aggregate_ingredients([{"ingredientGroups": [{"ingredients": [
        {"ingredient": "mjölk", "quantity": 1, "unit": "l"}]}]}])
    assert "recipes" not in it


def test_add_recipe_share_and_to_item():
    it = {"name": "x"}
    add_recipe_share(it, 5, 2, "dl")
    add_recipe_share(it, 5, 1, "dl")
    add_recipe_share(it, 5, None, None)
    assert it["recipes"] == [{"id": 5, "quantity": 3.0, "unit": "dl"}, {"id": 5, "quantity": 0.0}]
    assert to_item({"name": "x", "recipes": [{"id": 5, "quantity": 1.0}, {"quantity": 2}, "skräp"]})[
        "recipes"] == [{"id": 5, "quantity": 1.0}]


class _Recorder(IcaClient):
    def __init__(self, recipes):  # ingen auth; produktregistret otillgängligt
        self.recipes, self.synced = recipes, []
        self.lists = [{"title": "Handla", "offlineId": "H"}]

    def product_catalog(self):
        return None

    def get_lists(self):
        return self.lists

    def get_recipe(self, rid):
        return self.recipes[rid]

    def get_list_raw(self, offline_id):
        return {"title": "Handla", "offlineId": offline_id, "rows": []}

    def _sync(self, offline_id, payload):
        self.synced.append(payload)
        return {}


def test_recipe_tools_write_recipes_field(monkeypatch):
    c = _Recorder({1: _recipe(1, {"ingredient": "vispgrädde", "quantity": 2, "unit": "dl"}),
                   2: _recipe(2, {"ingredient": "vispgrädde", "quantity": 1, "unit": "dl"})})
    monkeypatch.setattr(server, "_client", c)
    server.add_recipe_to_shopping_list(1)
    (row,) = c.synced[0]["createdRows"]
    assert row["recipes"] == [{"id": 1, "quantity": 2.0, "unit": "dl"}]
    server.add_recipes_to_shopping_list([1, 2])
    (row,) = c.synced[1]["createdRows"]
    assert row["quantity"] == 3.0 and [s["id"] for s in row["recipes"]] == [1, 2]


def test_plain_items_have_empty_recipes(monkeypatch):
    c = _Recorder({})
    monkeypatch.setattr(server, "_client", c)
    server.add_items([server.Item(name="mjölk")])
    assert c.synced[0]["createdRows"][0]["recipes"] == []
