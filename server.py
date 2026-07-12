#!/usr/bin/env python3
"""
ICA MCP-server (stdio) — låter agenter läsa och redigera dina ICA-inköpslistor.

Verktyg:
  list_shopping_lists   – alla dina listor + hur många varor kvar/avbockade
  view_shopping_list    – innehållet i en lista
  add_items             – lägg till varor (fri text) på en lista
  check_off / uncheck   – bocka av / ångra en vara
  remove_item           – ta bort en vara helt
  create_shopping_list  – skapa ny lista
  delete_shopping_list  – radera en lista
  clear_checked         – ta bort alla avbockade varor

Auth sköts av ica_client (OAuth via ims.icagruppen.se, token cachas i
.ica_auth_state.json). Kräver svensk egress-IP (annars HTTP 451).

Kör:  .venv/bin/python server.py       (stdio — startas normalt av Claude Code)
"""

from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP

from ica_client import IcaClient, IcaError

# Logga till stderr (stdout är reserverat för MCP-protokollet!)
logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(levelname)s %(name)s: %(message)s")
_LOG = logging.getLogger("ica_mcp")

mcp = FastMCP("ICA")
_client: IcaClient | None = None


def client() -> IcaClient:
    global _client
    if _client is None:
        _client = IcaClient()
    return _client


# ------------------------------------------------------------------ helpers
def _row_view(row: dict) -> dict:
    v = {"name": row.get("productName"), "checked": bool(row.get("isStrikedOver"))}
    if row.get("quantity") is not None:
        v["quantity"] = row["quantity"]
    if row.get("unit"):
        v["unit"] = row["unit"]
    return v


def _resolve_rows(list_obj: dict, item: str, unstruck_only: bool = False) -> list[dict]:
    """Returnera rader för EN vara. Fel om flera olika varor matchar."""
    matches = IcaClient.match_rows(list_obj, item, unstruck_only=unstruck_only)
    if not matches:
        avail = sorted({r.get("productName", "") for r in list_obj.get("rows", [])})
        raise IcaError(f"Ingen vara matchar {item!r} på '{list_obj.get('title')}'. "
                       f"Varor: {avail}")
    names = {r.get("productName", "").lower() for r in matches}
    if len(names) > 1:
        raise IcaError(f"Flera olika varor matchar {item!r}: "
                       f"{sorted({r.get('productName') for r in matches})}. Var mer specifik.")
    return matches


# -------------------------------------------------------------------- tools
@mcp.tool()
def list_shopping_lists() -> list[dict]:
    """Lista alla dina ICA-inköpslistor med antal varor kvar och avbockade.
    Den första listan är din primära ('Handla') och används som standard när
    inget listnamn anges i andra verktyg."""
    out = []
    for L in client().get_lists():
        rows = L.get("rows", [])
        remaining = sum(1 for r in rows if not r.get("isStrikedOver"))
        out.append({
            "name": L.get("title"),
            "remaining": remaining,
            "checked": len(rows) - remaining,
            "total": len(rows),
        })
    return out


@mcp.tool()
def view_shopping_list(list_name: str | None = None) -> dict:
    """Visa innehållet i en inköpslista. list_name matchas mot listans titel
    (utelämna för den primära listan). Returnerar varor uppdelat i kvar/avbockade."""
    L = client().resolve_list(list_name)
    fresh = client().get_list_raw(L["offlineId"])
    rows = fresh.get("rows", [])
    remaining = [_row_view(r) for r in rows if not r.get("isStrikedOver")]
    checked = [_row_view(r) for r in rows if r.get("isStrikedOver")]
    return {"list": fresh.get("title"), "remaining": remaining, "checked": checked,
            "summary": f"{len(remaining)} kvar, {len(checked)} avbockade"}


@mcp.tool()
def add_items(items: list[str], list_name: str | None = None) -> str:
    """Lägg till en eller flera varor (fri text, t.ex. 'mjölk', '2 kg potatis')
    på en inköpslista. Utelämna list_name för den primära listan."""
    if not items:
        return "Inga varor angivna."
    L = client().resolve_list(list_name)
    client().add_rows(L["offlineId"], list(items))
    return f"La till {len(items)} vara/varor på '{L.get('title')}': {', '.join(items)}"


@mcp.tool()
def check_off(item: str, list_name: str | None = None) -> str:
    """Bocka av en vara (markera som köpt/klar) på en lista."""
    c = client()
    L = c.resolve_list(list_name)
    fresh = c.get_list_raw(L["offlineId"])
    rows = _resolve_rows(fresh, item, unstruck_only=True)
    for r in rows:
        r["isStrikedOver"] = True
    c.change_rows(L["offlineId"], rows)
    return f"Bockade av '{rows[0].get('productName')}' på '{fresh.get('title')}'."


@mcp.tool()
def uncheck(item: str, list_name: str | None = None) -> str:
    """Ångra avbockning av en vara (markera som ej köpt igen)."""
    c = client()
    L = c.resolve_list(list_name)
    fresh = c.get_list_raw(L["offlineId"])
    rows = _resolve_rows(fresh, item)
    for r in rows:
        r["isStrikedOver"] = False
    c.change_rows(L["offlineId"], rows)
    return f"Ångrade avbockning av '{rows[0].get('productName')}' på '{fresh.get('title')}'."


@mcp.tool()
def remove_item(item: str, list_name: str | None = None) -> str:
    """Ta bort en vara helt från en lista (inte samma som att bocka av)."""
    c = client()
    L = c.resolve_list(list_name)
    fresh = c.get_list_raw(L["offlineId"])
    rows = _resolve_rows(fresh, item)
    c.delete_rows(L["offlineId"], [r["offlineId"] for r in rows])
    return f"Tog bort '{rows[0].get('productName')}' från '{fresh.get('title')}'."


@mcp.tool()
def clear_checked(list_name: str | None = None) -> str:
    """Ta bort alla avbockade varor från en lista (rensa upp efter handling)."""
    c = client()
    L = c.resolve_list(list_name)
    fresh = c.get_list_raw(L["offlineId"])
    struck = [r["offlineId"] for r in fresh.get("rows", []) if r.get("isStrikedOver")]
    if not struck:
        return f"Inga avbockade varor att rensa på '{fresh.get('title')}'."
    c.delete_rows(L["offlineId"], struck)
    return f"Rensade {len(struck)} avbockade varor från '{fresh.get('title')}'."


@mcp.tool()
def create_shopping_list(title: str) -> str:
    """Skapa en ny inköpslista med angiven titel."""
    L = client().create_list(title)
    return f"Skapade listan '{L.get('title')}'."


@mcp.tool()
def delete_shopping_list(list_name: str) -> str:
    """Radera en hel inköpslista (kräver att du anger listnamnet explicit)."""
    L = client().resolve_list(list_name)
    title = L.get("title")
    client().delete_list(L["offlineId"])
    return f"Raderade listan '{title}'."


if __name__ == "__main__":
    mcp.run()
