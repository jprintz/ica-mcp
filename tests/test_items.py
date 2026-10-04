"""Varor med mängd och enhet: normalisering, verktygets schema och radbygget
mot ICA:s /sync (nätfritt)."""

import asyncio

import pytest
from pydantic import ValidationError

from ica_mcp.client import UNITS, IcaClient, IcaError, format_item, normalize_unit, to_item
from ica_mcp.server import Item, mcp


def _it(name, quantity=None, unit=None):
    return {"name": name, "quantity": quantity, "unit": unit}


# ------------------------------------------------------------ normalize_unit
@pytest.mark.parametrize("unit, expected", [
    (None, None), ("", None), ("dl", "dl"), ("DL", "dl"), ("st.", "st"),
    ("förp", "förp"), ("burk", "st"), ("port", "st"),
])
def test_normalize_unit(unit, expected):
    assert normalize_unit(unit) == expected


def test_units_are_ica_standard():
    assert UNITS == ("st", "förp", "kg", "hg", "g", "l", "dl", "cl", "ml", "msk", "tsk", "krm")


# ------------------------------------------------------------ to_item / format
def test_to_item_normalizes():
    assert to_item({"name": " grädde ", "quantity": 2, "unit": "dl"}) == _it("grädde", 2.0, "dl")
    assert to_item({"name": "tomater", "quantity": "2", "unit": "burk"}) == _it("tomater", 2.0, "st")
    assert to_item({"name": "mjölk", "quantity": 1.5}) == _it("mjölk", 1.5, "st")  # mängd utan enhet
    assert to_item({"name": "mjölk", "unit": "dl"}) == _it("mjölk")  # enhet utan mängd
    assert to_item({"name": "salt", "quantity": 0.0}) == _it("salt")  # ICA:s "ingen mängd"


def test_to_item_never_parses_text():
    # namn som ser ut som mängd lämnas orörda (t.ex. produktnamn)
    assert to_item("7up lime") == _it("7up lime")
    assert to_item({"name": "2 dl grädde"}) == _it("2 dl grädde")


def test_format_item():
    assert format_item(_it("grädde", 2.0, "dl")) == "2 dl grädde"
    assert format_item(_it("potatis", 1.5, "kg")) == "1.5 kg potatis"
    assert format_item(_it("salt")) == "salt"


# ------------------------------------------------------------ Item / schema
def test_item_rejects_non_standard_unit():
    with pytest.raises(ValidationError):
        Item(name="tomater", quantity=2, unit="burk")
    assert Item(name="tomater", quantity=2, unit="st").unit == "st"


def test_add_items_schema_is_structured_with_unit_enum():
    tool = next(t for t in asyncio.run(mcp.list_tools()) if t.name == "add_items")
    schema = tool.inputSchema
    item = schema["$defs"]["Item"]
    assert schema["properties"]["items"]["items"] == {"$ref": "#/$defs/Item"}  # inga fria strängar
    unit_enum = next(s["enum"] for s in item["properties"]["unit"]["anyOf"] if "enum" in s)
    assert tuple(unit_enum) == UNITS


# ------------------------------------------------------------ add_rows
class _Recorder(IcaClient):
    def __init__(self):  # ingen auth/state behövs
        self.payloads = []

    def _sync(self, offline_id, payload):
        self.payloads.append(payload)
        return {}


def test_add_rows_sends_quantity_and_unit():
    c = _Recorder()
    c.add_rows("LIST", [{"name": "grädde", "quantity": 2, "unit": "dl"},
                        {"name": "ägg", "quantity": 6}, {"name": "salt"}])
    rows = c.payloads[0]["createdRows"]
    assert [(r["productName"], r.get("quantity"), r.get("unit")) for r in rows] == [
        ("grädde", 2.0, "dl"), ("ägg", 6.0, "st"), ("salt", None, None)]
    assert all(r["sourceId"] < 0 and r["isStrikedOver"] is False for r in rows)


def test_add_rows_rejects_empty():
    with pytest.raises(IcaError):
        _Recorder().add_rows("LIST", [{"name": ""}, {"name": "  "}])
