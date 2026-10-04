"""
ica_mcp.client — återanvändbar klient mot ICA:s inofficiella API.

Sköter hela auth-flödet (OAuth2/OIDC via Curity @ ims.icagruppen.se, med
personnummer + lösenord utan BankID), token-cache + auto-refresh, samt
inköpslist-operationer mot gatewayen apimgw-pub.ica.se.

>>> Kräver svensk egress-IP (apimgw-pub.ica.se 451:ar annars). <<<

Auth-flödet är portat från LazyTarget/ha-ica-todo. Skriv-API:t (/sync) är
verifierat empiriskt mot ett riktigt konto:
  - enskild lista identifieras av listans offlineId (GUID), inte numeriskt id
  - lägg till rad:  {"createdRows":[{offlineId, productName, sourceId(neg), ...}]}
  - ändra/bocka av: {"changedRows":[<hela raden, muterad>]}
  - ta bort rad:    {"deletedRows":[<radens offlineId-sträng>]}
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import math
import os
import random
import re
import shutil
import threading
import unicodedata
import uuid
from os import urandom
from typing import Literal, get_args
from urllib.parse import urlparse, parse_qs

import platformdirs
import requests

from .products import ARTICLES_PATH, CATEGORY_IDS, ProductCache, ProductCatalog

try:
    import jwt  # PyJWT — valfritt, bara för att läsa användarnamn ur id_token
except ImportError:  # pragma: no cover
    jwt = None

_LOG = logging.getLogger("ica_mcp.client")

# --------------------------------------------------------------------------
# Konstanter (verbatim från LazyTarget/ha-ica-todo)
# --------------------------------------------------------------------------
IMS_BASE = "https://ims.icagruppen.se"
API_BASE = "https://apimgw-pub.ica.se"

TOKEN_ENDPOINT = f"{IMS_BASE}/oauth/v2/token"
AUTHORIZE_ENDPOINT = f"{IMS_BASE}/oauth/v2/authorize"
REGISTER_ENDPOINT = f"{IMS_BASE}/register"
LOGIN_ENDPOINT = f"{IMS_BASE}/authn/authenticate/IcaCustomers"

DCR_CLIENT_ID = "ica-app-dcr-registration"
DCR_CLIENT_SECRET = "uxLHTBvZ-Z2fV-SbrHl1E-tz7vB3jQFrwAdSLlbVMMu1rxDdvJU0s8KGu9d1wLS4"
DCR_SOFTWARE_ID = "dcr-ica-app-template"
REDIRECT_URI = "icacurity://app"
ACR = "urn:se:curity:authentication:html-form:IcaCustomers"

SL_PATH = "sverige/digx/mobile/shoppinglistservice/v1/shoppinglists"
RECIPE_PATH = "sverige/digx/mobile/recipeservice/v1"
STORE_PATH = "sverige/digx/mobile/storeservice/v1"
OFFER_PATH = "sverige/digx/mobile/offerservice/v1"
BONUS_PATH = "sverige/digx/mobile/bonusservice/v1"
PRODUCT_PATH = "sverige/digx/mobile/productservice/v1"

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)

LEGACY_STATE_NAME = ".ica_auth_state.json"


def resolve_state_file() -> str:
    """Var token-cachen ligger. Prioritet:
      1. ICA_STATE_FILE (explicit override)
      2. platformdirs user_state_dir('ica-mcp')/auth_state.json  (NON-roaming:
         %LOCALAPPDATA% på Windows, ~/.local/state på Linux, Application Support
         på macOS — så en långlivad refresh-token inte synkas mellan maskiner)
    """
    override = os.environ.get("ICA_STATE_FILE")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(
        platformdirs.user_state_dir("ica-mcp", appauthor=False), "auth_state.json"
    )


def _ensure_parent_dir(path: str) -> None:
    """makedirs för filens katalog — no-op om path är ett naket filnamn (dirname
    == '', vilket annars ger FileNotFoundError från os.makedirs)."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def _legacy_state_candidates() -> list[str]:
    """Gamla platser där .ica_auth_state.json kan ligga (för engångs-migrering)."""
    here = os.path.dirname(os.path.abspath(__file__))          # ica_mcp/
    return [
        os.path.join(os.getcwd(), LEGACY_STATE_NAME),
        os.path.join(here, LEGACY_STATE_NAME),                 # bredvid modulen
        os.path.join(os.path.dirname(here), LEGACY_STATE_NAME),  # repo-roten (editable install)
    ]


class IcaError(RuntimeError):
    """Generiskt ICA-fel (nätverk, HTTP, validering)."""


class IcaAuthError(IcaError):
    """Inloggning/refresh misslyckades."""


def validate_barcode(ean) -> str:
    """Ta bort mellanslag och kräv 8–14 siffror (EAN-8/UPC/EAN-13/GTIN-14).
    Returnerar den rena koden, annars IcaError."""
    s = re.sub(r"\s+", "", "" if ean is None else str(ean))
    if not (s.isascii() and s.isdigit() and 8 <= len(s) <= 14):
        raise IcaError(f"Ogiltig streckkod {ean!r}: ange 8–14 siffror (EAN/GTIN).")
    return s


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _ts() -> str:
    """ICA:s tidsstämpelformat, t.ex. 2026-07-12T16:40:08Z."""
    return _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_personnummer(value: str | None) -> str | None:
    """Returnera personnumret som 12 siffror, eller ``None``.

    ICA:s authenticate-endpoint kräver sekelsiffror och svarar annars HTTP 400
    utan förklaring — webbinloggningen lägger till dem åt användaren, så vi gör
    detsamma. Skiljetecken tas bort så att ``780101-1234`` fungerar lika bra som
    ``197801011234``.

    Sekel härleds ur tvåsiffrigt år: ett år som ligger i framtiden tolkas som
    1900-talet. Samordningsnummer (dag + 60) påverkas inte.
    """
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) == 12:
        return digits
    if len(digits) == 10:
        yy = int(digits[:2])
        current_yy = dt.date.today().year % 100
        century = "20" if yy <= current_yy else "19"
        return century + digits
    # Låt övriga längder passera oförändrade — servern får avgöra.
    return digits or None


# --------------------------------------------------------------------------
# Varor: mängd + enhet
# --------------------------------------------------------------------------
# Enheterna som ICA-appen använder.
Unit = Literal["st", "förp", "kg", "hg", "g", "l", "dl", "cl", "ml", "msk", "tsk", "krm"]
UNITS: tuple[str, ...] = get_args(Unit)


def _to_number(value) -> float | None:
    """2 / 1.5 / '1,5' → float; tomt, 0, negativt, inf/nan eller ogiltigt →
    None (ICA anger "ingen mängd" som 0)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        n = float(str(value).strip().replace(",", ".")) if isinstance(value, str) else float(value)
    except ValueError:
        return None
    if not math.isfinite(n):
        return None
    n = round(n, 3)
    return n if n > 0 else None


# Stavningar i receptdata/fritext → ICA-enhet. Förpackningar som köps hela
# (burk, påse, flaska) räknas som st; paket/ask som förp.
_UNIT_ALIASES = {
    **{u: u for u in UNITS},
    "styck": "st", "stycken": "st", "stk": "st",
    "burk": "st", "burkar": "st", "påse": "st", "påsar": "st",
    "flaska": "st", "flaskor": "st",
    "förpackning": "förp", "förpackningar": "förp", "pkt": "förp", "paket": "förp",
    "ask": "förp", "askar": "förp",
    "kilo": "kg", "kilogram": "kg", "hekto": "hg", "hektogram": "hg",
    "gr": "g", "gram": "g",
    "liter": "l", "deciliter": "dl", "centiliter": "cl", "milliliter": "ml",
    "matsked": "msk", "matskedar": "msk", "tesked": "tsk", "teskedar": "tsk",
    "kryddmått": "krm",
}


def normalize_unit(unit) -> str | None:
    """Enhet → en av UNITS ('liter' → 'l', 'pkt' → 'förp', 'burk' → 'st').
    Tomt/None → None. Okända enheter ('klyftor', 'nypa', 'cm') ger ValueError:
    de går inte att uttrycka i ICA:s enheter utan att mängden ändrar betydelse."""
    if unit is None:
        return None
    u = str(unit).strip().lower().rstrip(".")
    if not u:
        return None
    try:
        return _UNIT_ALIASES[u]
    except KeyError:
        raise ValueError(f"okänd enhet {unit!r}") from None


# ICA:s recept har ibland förpackningen först i ingrediensnamnet i stället
# för i unit-fältet: "förp majskorn (à 150 g)".
_PACKAGE_WORDS = {"förp", "förp.", "förpackning", "förpackningar", "paket"}


def to_item(it) -> dict:
    """Normalisera en vara till {name, quantity, unit}. En dict {name,
    quantity?, unit?, product_id?} eller en sträng (= bara namn; texten tolkas
    aldrig). Enheten följer alltid UNITS; mängd utan enhet blir 'st', enhet
    utan mängd tas bort, och okänd enhet med mängd ger ValueError (se
    normalize_unit). Följer med om de finns: product_id (önskad ICA-produkt),
    fallback_product_id (receptets ingredientId), category (avdelning, se
    products.Category), product (kopplad produkt, se IcaClient.link_products)
    och recipes (receptandelar, se add_recipe_share)."""
    if isinstance(it, str):
        it = {"name": it}
    qty = _to_number(it.get("quantity"))
    unit = (normalize_unit(it.get("unit")) or "st") if qty else None
    out = {"name": str(it.get("name") or "").strip(), "quantity": qty, "unit": unit}
    for key in ("product_id", "fallback_product_id"):
        pid = it.get(key)
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            out[key] = pid
    if it.get("category") in CATEGORY_IDS:
        out["category"] = it["category"]
    if isinstance(it.get("product"), dict):
        out["product"] = it["product"]
    if isinstance(it.get("recipes"), list):
        out["recipes"] = [dict(r) for r in it["recipes"] if isinstance(r, dict) and r.get("id")]
    for flag in ("weak_link", "amount_in_name"):  # se _same_item
        if it.get(flag) is True:
            out[flag] = True
    return out


def recipe_id_of(recipe: dict) -> int | None:
    """Receptets id som int — även om ICA skickar det som sträng ('123')."""
    rid = recipe.get("id")
    if isinstance(rid, bool):
        return None
    if isinstance(rid, int):
        return rid
    if isinstance(rid, str) and rid.strip().isdigit():
        return int(rid)
    return None


def add_recipe_share(item: dict, recipe_id: int, quantity, unit) -> None:
    """Lägg till ett recepts andel av varan i item["recipes"] — ICA:s format
    [{id, quantity, unit?}], som appen visar under "Tillagd från recept" (bild,
    receptnamn, mängd). Samma recept + enhet summeras; ingen mängd = 0.0."""
    shares = item.setdefault("recipes", [])
    q = float(quantity or 0.0)
    for s in shares:
        if s["id"] == recipe_id and s.get("unit") == unit:
            s["quantity"] = round(s["quantity"] + q, 3)
            return
    share = {"id": recipe_id, "quantity": q}
    if unit:
        share["unit"] = unit
    shares.append(share)


def _new_row(it: dict) -> dict:
    """En ny rad för /sync createdRows (se IcaClient.add_rows)."""
    row = {
        "offlineId": str(uuid.uuid4()).upper(),
        "productName": it["name"],
        "sourceId": -random.randint(10**6, 10**9),  # negativ = fri text
        "isStrikedOver": False,
        "recipes": it.get("recipes") or [],
    }
    if it.get("product"):
        apply_product(row, it["product"])
    elif it.get("category"):
        row["articleGroupId"] = row["articleGroupIdExtended"] = CATEGORY_IDS[it["category"]]
    if it.get("quantity") is not None:
        row["quantity"] = float(it["quantity"])
    if it.get("unit"):
        row["unit"] = it["unit"]
    return row


# --------------------------------------------------------------------------
# En rad per vara: slå ihop med det som redan finns på listan
# --------------------------------------------------------------------------
# Enheter som kan räknas om: volym via ml, vikt via g. st/förp räknas aldrig om.
_UNIT_FACTORS = {"ml": ("volym", 1), "krm": ("volym", 1), "tsk": ("volym", 5), "msk": ("volym", 15),
                 "cl": ("volym", 10), "dl": ("volym", 100), "l": ("volym", 1000),
                 "g": ("vikt", 1), "hg": ("vikt", 100), "kg": ("vikt", 1000)}


def merge_quantities(q1, u1, q2, u2) -> tuple | None:
    """Summera två mängder. Samma enhet → summan; samma slags enhet (volym,
    vikt) → summan i den större enheten om den blir exakt med tre decimaler
    (2 msk + 1 dl → 1.3 dl), annars i den mindre (1 kg + 0,5 g → 1000,5 g);
    saknas mängd på ena sidan → den andra. None om enheterna inte går ihop
    (g och st)."""
    if not q2:
        return q1, u1
    if not q1:
        return q2, u2
    if u1 == u2:
        return round(q1 + q2, 3), u1
    f1, f2 = _UNIT_FACTORS.get(u1), _UNIT_FACTORS.get(u2)
    if not f1 or not f2 or f1[0] != f2[0]:
        return None
    (big, fb), (small, fs) = sorted([(u1, f1[1]), (u2, f2[1])], key=lambda x: -x[1])
    total = q1 * f1[1] + q2 * f2[1]
    in_big = round(total / fb, 3)
    if abs(in_big * fb - total) < 1e-9 * max(1.0, total):
        return in_big, big
    return round(total / fs, 3), small


def _norm_name(name: str) -> str:
    return " ".join(unicodedata.normalize("NFC", name).casefold().split())


def _trusted_link(it: dict) -> bool:
    """En koppling som räcker för att slå ihop varor med olika namn. Inte:
    receptets grova ingredientId ('körsbärstomater på burk' → 'tomat'), en
    vara vars mängd står i namnet ('vitlök (3 klyftor)' – mängden skulle
    försvinna i en st-rad) eller en rad vars namn inte motsvarar produkten."""
    return not (it.get("weak_link") or it.get("amount_in_name"))


def _same_item(a: dict, b: dict) -> bool:
    """Samma vara: samma ICA-produkt om båda har en pålitlig koppling (se
    _trusted_link), annars samma namn (skiftläge, mellanslag och
    Unicode-form spelar ingen roll). Två olika produkter med samma namn räknas
    inte som samma vara."""
    pa, pb = (a.get("product") or {}).get("id"), (b.get("product") or {}).get("id")
    if pa and pb and _trusted_link(a) and _trusted_link(b):
        return pa == pb
    return _norm_name(a["name"]) == _norm_name(b["name"])


def _row_as_item(row: dict, catalog=None) -> dict:
    """En befintlig rad i samma form som to_item (för jämförelse/sammanslagning).
    Appen lämnar enheten tom för styckvaror ('4 tomater') — det motsvarar 'st'.
    Med catalog räknas radens produktkoppling som pålitlig bara om radens
    namn matchar samma produkt — en rad som kopplades via receptets
    ingredientId ('körsbärstomater på burk' → tomat) ska inte svälja 'tomater'."""
    q = _to_number(row.get("quantity"))
    try:
        unit = (normalize_unit(row.get("unit")) or "st") if q else None
    except ValueError:  # enhet som appen satt men vi inte känner till
        unit = str(row.get("unit"))
    it = {"name": str(row.get("productName") or ""), "quantity": q, "unit": unit,
          "recipes": [{"id": s["id"], "quantity": _to_number(s.get("quantity")) or 0.0,
                       **({"unit": s["unit"]} if s.get("unit") else {})}
                      for s in row.get("recipes") or [] if isinstance(s, dict) and s.get("id")]}
    src = row.get("sourceId") or 0
    if src > 0:
        it["product"] = {"id": src}
        if catalog is not None and (catalog.match(it["name"]) or {}).get("id") != src:
            it["weak_link"] = True
    return it


def _merge_item(target: dict, it: dict) -> bool:
    """Slå ihop it i target (båda i to_item-form). False om enheterna inte går ihop."""
    qu = merge_quantities(target["quantity"], target["unit"], it["quantity"], it["unit"])
    if qu is None:
        return False
    target["quantity"], target["unit"] = qu
    for s in it.get("recipes") or []:
        add_recipe_share(target, s["id"], s.get("quantity"), s.get("unit"))
    if not target.get("product") and it.get("product"):
        target["product"] = it["product"]  # fritextrad får produktkoppling
    elif not target.get("product") and not target.get("category") and it.get("category"):
        target["category"] = it["category"]
    return True


def plan_additions(rows: list[dict], items: list[dict], catalog=None) -> tuple[list, list, list]:
    """Planera att lägga till items (to_item-form, ev. kopplade) på en lista
    med raderna rows, så att varje vara bara har en rad: en vara som redan
    finns (ej avbockad, se _same_item) och har kompatibel enhet ökar den raden
    i stället för att bli en ny. Dubbletter inom items slås också ihop.
    catalog (ProductCatalog) avgör om befintliga raders produktkoppling är
    pålitlig, se _row_as_item. Returnerar (nya varor, ändrade rader,
    [{name, before, after, items, unsorted}]) — items är de tillagda varornas
    namn, unsorted att raden fortfarande saknar avdelning."""
    cands: list[tuple[dict, dict | None]] = []   # (vy i to_item-form, ev. befintlig rad)
    for r in rows:
        if not r.get("isStrikedOver") and r.get("productName"):
            cands.append((_row_as_item(r, catalog), r))
    new: list[dict] = []
    touched: dict[str, tuple[dict, dict, str, list]] = {}  # offlineId → (rad, vy, före, varor)
    for it in items:
        for view, row in cands:
            if not _same_item(view, it):
                continue
            before = format_item(view)
            if not _merge_item(view, it):
                continue
            if row is not None:
                touched.setdefault(row["offlineId"], (row, view, before, []))[3].append(it["name"])
            break
        else:
            new.append(it)
            cands.append((it, None))
    changed, merges = [], []
    for original, view, before, names in touched.values():
        row = dict(original)
        if view["quantity"]:
            row["quantity"] = float(view["quantity"])
            if not (row.get("unit") is None and view["unit"] == "st"):
                row["unit"] = view["unit"]
        row["recipes"] = view.get("recipes") or []
        p = view.get("product")
        if p and p.get("parentId") and (row.get("sourceId") or 0) <= 0:
            row["sourceId"] = p["id"]
            row["articleGroupId"] = p["parentId"]
            row["articleGroupIdExtended"] = p.get("parentIdExtended") or p["parentId"]
        elif view.get("category") and row.get("articleGroupId") in (None, 12):
            row["articleGroupId"] = row["articleGroupIdExtended"] = CATEGORY_IDS[view["category"]]
        if row != {**original, "recipes": original.get("recipes") or []}:
            changed.append(row)  # oförändrad rad (t.ex. samma streckkod igen) skrivs inte
        unsorted = (row.get("sourceId") or 0) <= 0 and row.get("articleGroupId") in (None, 12)
        merges.append({"name": row["productName"], "before": before, "after": format_item(view),
                       "items": names, "unsorted": unsorted})
    return new, changed, merges


def format_quantity(q: float) -> str:
    """1.5 → '1,5', 2.0 → '2' (svensk decimalkomma)."""
    return f"{q:g}".replace(".", ",")


def format_item(it: dict) -> str:
    """{name: 'grädde', quantity: 2.0, unit: 'dl'} → '2 dl grädde'."""
    if not it.get("quantity"):
        return it["name"]
    return " ".join(p for p in (format_quantity(it["quantity"]), it.get("unit"), it["name"]) if p)


def apply_product(row: dict, p: dict) -> None:
    """Koppla en rad till ICA-produkten p (som när man väljer ett förslag i
    appen). Avdelningen sätts bara om produkten har en — aldrig null."""
    row["sourceId"] = p["id"]
    if p.get("parentId"):
        row["articleGroupId"] = p["parentId"]
        row["articleGroupIdExtended"] = p.get("parentIdExtended") or p["parentId"]


class IcaClient:
    """Trådsäker(ish) klient. En instans per konto."""

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        state_file: str | None = None,
        user_agent: str | None = USER_AGENT,
        early_refresh_seconds: int = 60,
    ) -> None:
        self.username = normalize_personnummer(username or os.environ.get("ICA_USER"))
        self.password = password or os.environ.get("ICA_PASS")
        self.state_file = state_file or resolve_state_file()
        self.early_refresh = early_refresh_seconds
        self.session = requests.Session()
        if user_agent:
            self.session.headers["User-Agent"] = user_agent
        self._lock = threading.RLock()
        self._state = self._load_state()

    # ----------------------------------------------------------------- state
    def _migrate_legacy_state(self) -> None:
        """Kopiera (aldrig flytta) en gammal .ica_auth_state.json till den nya
        platsen första gången, så att en uppgradering inte tappar inloggningen."""
        if os.environ.get("ICA_STATE_FILE"):
            return  # explicit override — rör inte
        for legacy in _legacy_state_candidates():
            if legacy == self.state_file or not os.path.isfile(legacy):
                continue
            try:
                _ensure_parent_dir(self.state_file)
                shutil.copyfile(legacy, self.state_file)
                try:
                    os.chmod(self.state_file, 0o600)
                except OSError:
                    pass
                _LOG.info("Migrerade auth-state från %s → %s", legacy, self.state_file)
            except OSError as e:
                _LOG.warning("Kunde inte migrera %s: %s", legacy, e)
            return

    def _load_state(self) -> dict:
        if not os.path.exists(self.state_file):
            self._migrate_legacy_state()
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                _LOG.warning("Kunde inte läsa %s, börjar om.", self.state_file)
        return {}

    def _save_state(self) -> None:
        try:
            _ensure_parent_dir(self.state_file)
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
            os.chmod(self.state_file, 0o600)  # nära no-op på Windows (bara read-only-biten)
        except OSError as e:
            _LOG.warning("Kunde inte spara auth-state: %s", e)

    # ------------------------------------------------------------ auth-steg
    @staticmethod
    def _qs(location: str, key: str) -> str:
        vals = parse_qs(urlparse(location).query).get(key)
        if vals:
            return vals[0]
        m = re.search(rf"[?&]{re.escape(key)}=([^&\s]+)", location)
        if not m:
            raise IcaAuthError(f"Hittade inte '{key}' i redirect: {location!r}")
        return m.group(1)

    @staticmethod
    def _hidden(html: str, name: str) -> str:
        for pat in (
            rf'name="{name}"[^>]*value="([^"]*)"',
            rf'value="([^"]*)"[^>]*name="{name}"',
        ):
            m = re.search(pat, html)
            if m:
                return m.group(1)
        raise IcaAuthError(
            f"Hittade inte dolt fält '{name}' i login-svaret — troligen fel "
            "personnummer/lösenord (eller kontot kräver BankID)."
        )

    @staticmethod
    def _pkce() -> tuple[str, str]:
        verifier = re.sub("[^a-zA-Z0-9]+", "", base64.urlsafe_b64encode(urandom(40)).decode())
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).decode().replace("=", "")
        return challenge, verifier

    def _full_login(self) -> None:
        if not self.username or not self.password:
            raise IcaAuthError(
                "Inte inloggad. Kör `ica-mcp login` i en terminal (eller sätt "
                "ICA_USER/ICA_PASS i serverns miljö) och försök igen."
            )
        _LOG.info("Full inloggning …")
        # 1. bootstrap-token
        r = self.session.post(TOKEN_ENDPOINT, data={
            "client_id": DCR_CLIENT_ID, "client_secret": DCR_CLIENT_SECRET,
            "grant_type": "client_credentials", "scope": "dcr", "response_type": "token",
        }, timeout=30)
        r.raise_for_status()
        bootstrap = r.json()["access_token"]
        # 2. dynamisk klientregistrering
        r = self.session.post(REGISTER_ENDPOINT, json={"software_id": DCR_SOFTWARE_ID},
                              headers={"Authorization": f"Bearer {bootstrap}"}, timeout=30)
        r.raise_for_status()
        client = r.json()
        # 3. authorize (PKCE)
        challenge, verifier = self._pkce()
        r = self.session.get(AUTHORIZE_ENDPOINT, params={
            "client_id": client["client_id"], "scope": client["scope"],
            "redirect_uri": REDIRECT_URI, "response_type": "code",
            "code_challenge": challenge, "code_challenge_method": "S256",
            "prompt": "login", "acr": ACR,
        }, allow_redirects=False, timeout=30)
        r.raise_for_status()
        state = self._qs(r.headers["Location"], "state")
        self.session.get(r.headers["Location"], timeout=30).raise_for_status()
        # 4. posta uppgifter
        r = self.session.post(LOGIN_ENDPOINT,
                              data={"userName": self.username, "password": self.password}, timeout=30)
        r.raise_for_status()
        sso_token = self._hidden(r.text, "token")
        # 5. hämta kod + byt mot token
        r = self.session.post(AUTHORIZE_ENDPOINT,
                              params={"client_id": client["client_id"], "forceAuthN": "true", "acr": ACR},
                              data={"token": sso_token, "state": state},
                              allow_redirects=False, timeout=30)
        r.raise_for_status()
        code = self._qs(r.headers["Location"], "code")
        r = self.session.post(TOKEN_ENDPOINT, data={
            "code": code, "client_id": client["client_id"], "client_secret": client["client_secret"],
            "grant_type": "authorization_code", "scope": client["scope"],
            "response_type": "token", "code_verifier": verifier, "redirect_uri": REDIRECT_URI,
        }, timeout=30)
        r.raise_for_status()
        self._state = {"client": client, "token": self._stamp(r.json())}
        self._save_state()

    def _refresh(self) -> None:
        client = self._state["client"]
        rt = self._state["token"]["refresh_token"]
        basic = base64.b64encode(f"{client['client_id']}:{client['client_secret']}".encode()).decode()
        r = self.session.post(TOKEN_ENDPOINT,
                              data={"grant_type": "refresh_token", "refresh_token": rt},
                              headers={"Authorization": f"Basic {basic}"}, timeout=30)
        r.raise_for_status()
        self._state["token"] = self._stamp({**self._state["token"], **r.json()})
        self._save_state()

    @staticmethod
    def _stamp(token: dict) -> dict:
        token = dict(token)
        token["expiry"] = (_now_utc() + dt.timedelta(seconds=token.get("expires_in", 900))).isoformat()
        return token

    def _token_valid(self, token: dict) -> bool:
        exp = token.get("expiry")
        if not exp or not token.get("access_token"):
            return False
        try:
            return dt.datetime.fromisoformat(exp) > _now_utc() + dt.timedelta(seconds=self.early_refresh)
        except ValueError:
            return False

    def _access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            tok = self._state.get("token")
            if not force_refresh and tok and self._token_valid(tok):
                return tok["access_token"]
            # försök refresh
            if tok and tok.get("refresh_token") and self._state.get("client"):
                try:
                    self._refresh()
                    return self._state["token"]["access_token"]
                except requests.HTTPError as e:
                    _LOG.info("Refresh misslyckades (%s) — gör full inloggning.",
                              e.response.status_code if e.response is not None else "?")
            self._full_login()
            return self._state["token"]["access_token"]

    # -------------------------------------------------------------- HTTP
    def _api(self, method: str, path: str, json_body=None, _retry=True,
             allow_404: bool = False) -> requests.Response:
        url = f"{API_BASE}/{path}"
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        data = None
        if json_body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            data = json.dumps(json_body)
        r = self.session.request(method, url, headers=headers, data=data, timeout=30)
        if r.status_code == 401 and _retry:
            self._access_token(force_refresh=True)
            return self._api(method, path, json_body, _retry=False, allow_404=allow_404)
        if r.status_code == 451:
            raise IcaError("HTTP 451 från ICA-gatewayen — icke-svensk IP (geo-block). "
                           "Kör servern från svensk egress.")
        if r.status_code == 404 and allow_404:
            return r
        if not r.ok:
            raise IcaError(f"ICA {method} {path} → HTTP {r.status_code}: {(r.text or '')[:300]}")
        return r

    # ---------------------------------------------------- auth (publikt)
    def authenticate(self, force: bool = False) -> None:
        """Se till att vi har en giltig session. force=True tvingar full
        inloggning (kräver username/password); annars används cache/refresh och
        full inloggning bara som sista utväg. Används av `ica-mcp login`."""
        with self._lock:
            if force:
                self._full_login()
            else:
                self._access_token()

    def token_status(self) -> dict:
        """Offline-status om den cachade sessionen — gör INGEN nätverksanrop
        och triggar ingen inloggning. Används av `ica-mcp status`."""
        tok = self._state.get("token") or {}
        return {
            "state_file": self.state_file,
            "exists": os.path.exists(self.state_file),
            "access_valid": self._token_valid(tok),
            "has_refresh": bool(tok.get("refresh_token")),
            "expiry": tok.get("expiry"),
        }

    # ---------------------------------------------------- konto/whoami
    def whoami(self) -> str | None:
        self._access_token()
        idt = self._state.get("token", {}).get("id_token")
        if jwt and idt:
            try:
                c = jwt.decode(idt, options={"verify_signature": False})
                return f"{c.get('given_name','')} {c.get('family_name','')}".strip() or None
            except Exception:  # noqa: BLE001
                return None
        return None

    # ---------------------------------------------------- inköpslistor (raw)
    def get_lists(self) -> list[dict]:
        return self._api("GET", SL_PATH).json().get("shoppingLists", [])

    def get_list_raw(self, offline_id: str) -> dict:
        return self._api("GET", f"{SL_PATH}/{offline_id}").json()

    def create_list(self, title: str, comment: str = "", store_id: int = 0) -> dict:
        """Skapa en lista. store_id (sortingStore) kopplar listan till en butik;
        utan butik visar ICA-appen inga kategorier. Se store_id_for."""
        offline_id = str(uuid.uuid4()).upper()
        body = {"offlineId": offline_id, "title": title, "commentText": comment,
                "sortingStore": int(store_id or 0), "rows": [], "latestChange": _ts()}
        self._api("POST", SL_PATH, body)
        return self.get_list_raw(offline_id)

    def delete_list(self, offline_id: str) -> None:
        self._api("DELETE", f"{SL_PATH}/{offline_id}")

    def _sync(self, offline_id: str, payload: dict) -> dict:
        return self._api("POST", f"{SL_PATH}/{offline_id}/sync", payload).json()

    def add_rows(self, offline_id: str, items: list) -> dict:
        """Lägg till varor: dicts {name, quantity?, unit?, product?, category?}
        (se to_item). En vara med product (från link_products) kopplas till
        ICA-produkten som när man väljer ett förslag i appen; annars läggs den
        till som fritext, i avdelningen category om den finns (annars hamnar
        den under Ospecificerad). recipes blir radens "Tillagd från recept".
        Skapar alltid nya rader — se add_or_merge för en rad per vara."""
        rows = [_new_row(it) for it in map(to_item, items) if it["name"]]
        if not rows:
            raise IcaError("Inga varor att lägga till.")
        return self._sync(offline_id, {"createdRows": rows})

    def add_or_merge(self, offline_id: str, items: list, merge: bool = True) -> dict:
        """Lägg till varor så att varje vara bara har en rad på listan: en vara
        som redan finns (se plan_additions) ökar den raden, övriga blir nya
        rader. merge=False: alltid nya rader. Allt skrivs i ett /sync-anrop
        (verifierat mot ICA: createdRows och changedRows går i samma anrop).
        Listan läses direkt före skrivningen, men ändras en rad i appen i just
        det fönstret skrivs den ändå över med vår version. Returnerar
        {created, merged}."""
        items = [it for it in map(to_item, items) if it["name"]]
        if not items:
            raise IcaError("Inga varor att lägga till.")
        if not merge:
            self._sync(offline_id, {"createdRows": [_new_row(it) for it in items]})
            return {"created": items, "merged": []}
        # bara en redan laddad katalog (link_products) — ingen ny hämtning här
        cache = getattr(self, "_products", None)
        catalog = cache.get() if cache is not None else None
        rows = self.get_list_raw(offline_id).get("rows", [])
        new, changed, merges = plan_additions(rows, items, catalog)
        payload: dict = {}
        if not new and not changed:
            return {"created": [], "merged": merges}  # inget att skriva
        if new:
            payload["createdRows"] = [_new_row(it) for it in new]
        if changed:
            for r in changed:
                r["latestChange"] = _ts()
            payload["changedRows"] = changed
        self._sync(offline_id, payload)
        return {"created": new, "merged": merges}

    def change_rows(self, offline_id: str, rows: list[dict]) -> dict:
        for r in rows:
            r["latestChange"] = _ts()
        return self._sync(offline_id, {"changedRows": rows})

    def delete_rows(self, offline_id: str, row_offline_ids: list[str]) -> dict:
        return self._sync(offline_id, {"deletedRows": list(row_offline_ids)})

    def set_list_store(self, offline_id: str, store_id: int) -> dict:
        """Byt butik (sortingStore) på en befintlig lista; varorna påverkas inte.
        Verifierat mot ICA: /sync med changedShoppingListProperties."""
        return self._sync(offline_id, {"changedShoppingListProperties": {
            "sortingStore": int(store_id), "latestChange": _ts()}})

    # ---------------------------------------------------- produktregister
    def _fetch_articles(self) -> list[dict]:
        return self._api("GET", ARTICLES_PATH).json().get("articles", [])

    def product_catalog(self) -> ProductCatalog | None:
        """ICA:s produktregister, cachat i minnet och på disk (se products.py).
        None om det inte kan hämtas och ingen cache finns."""
        if getattr(self, "_products", None) is None:
            self._products = ProductCache(self._fetch_articles)
        return self._products.get()

    def link_products(self, items: list[dict], suggestions: int = 3) -> dict:
        """Koppla varor (från to_item) till ICA-produkter, på plats, i ordningen:
        product_id → exakt namn eller pluralnamn (se ProductCatalog.match) →
        fallback_product_id (receptets ingredientId). Namnet behålls. Ingen
        gissning — omatchade varor returneras, med förslag om de saknar
        category (annars hamnar de under Ospecificerad).
        Returnerar {available, linked, unlinked: [{name, reason?, category?,
        suggestions}], explicit: [{name, product}], fallback: [{name, product}]}
        — explicit/fallback är kopplingar via product_id resp. ingredientId,
        som ska redovisas eftersom namnet inte styrkte dem."""
        catalog = self.product_catalog()
        if catalog is None:
            return {"available": False, "linked": 0, "unlinked": [], "explicit": [], "fallback": []}
        linked, unlinked, explicit, via_fallback = 0, [], [], []
        for it in items:
            pid = it.pop("product_id", None)
            fallback = it.pop("fallback_product_id", None)
            p = catalog.get(pid) if pid else None
            if p:
                explicit.append({"name": it["name"], "product": p})
            else:
                p = catalog.match(it["name"])
            if not p and fallback and (p := catalog.get(fallback)):
                via_fallback.append({"name": it["name"], "product": p})
                it["weak_link"] = True  # grov koppling: sorterar, men slår aldrig ihop
            if p:
                it["product"] = p
                linked += 1
                continue
            want = suggestions if suggestions > 0 and not it.get("category") else 0
            hits = catalog.search(it["name"], want) if want else []
            entry = {"name": it["name"], "suggestions": [catalog.summary(a) for a in hits]}
            if it.get("category"):
                entry["category"] = it["category"]
            if pid:
                entry["reason"] = f"okänt produkt-id {pid}"
            unlinked.append(entry)
        return {"available": True, "linked": linked, "unlinked": unlinked,
                "explicit": explicit, "fallback": via_fallback}

    # ---------------------------------------------------- resolvers
    def resolve_list(self, ref: str | int | None = None, exact: bool = False) -> dict:
        """Hitta en lista via titel, numeriskt id eller offlineId. None = primär.
        exact=True (för destruktiva anrop): kräver icke-tomt ref och exakt träff
        på id, offlineId eller hel titel – ingen primärlista, ingen delmatchning."""
        lists = self.get_lists()
        if not lists:
            raise IcaError("Du har inga inköpslistor.")
        if exact:
            s = "" if ref is None else str(ref).strip()
            if not s:
                raise IcaError("Ange listans exakta namn (tomt namn tillåts inte här).")
            low = s.lower()
            # Samla träffar på id, offlineId OCH titel: om t.ex. en listas id
            # råkar vara en annan listas titel ska vi vägra, inte gissa.
            hits = [L for L in lists if str(L.get("id")) == s
                    or L.get("offlineId", "").lower() == low
                    or L.get("title", "").strip().lower() == low]
            if len(hits) > 1:
                raise IcaError(f"Flera listor matchar exakt {ref!r}: {[L['title'] for L in hits]}")
            if hits:
                return hits[0]
            raise IcaError(f"Ingen lista heter exakt {ref!r}. Dina listor: {[L['title'] for L in lists]}")
        if ref is None or str(ref).strip() == "":
            return lists[0]  # ICA returnerar primärlistan ("Handla") först
        s = str(ref).strip()
        for L in lists:
            if str(L.get("id")) == s or L.get("offlineId", "").upper() == s.upper():
                return L
        low = s.lower()
        exact = [L for L in lists if L.get("title", "").lower() == low]
        if exact:
            return exact[0]
        sub = [L for L in lists if low in L.get("title", "").lower()]
        if len(sub) == 1:
            return sub[0]
        if len(sub) > 1:
            raise IcaError(f"Flera listor matchar {ref!r}: {[L['title'] for L in sub]}")
        raise IcaError(f"Ingen lista matchar {ref!r}. Dina listor: {[L['title'] for L in lists]}")

    def resolve_or_create_list(self, name: str, store_ref=None) -> dict:
        """Hitta en lista med exakt titel, annars skapa en ny med det namnet,
        kopplad till butiken store_ref (se store_id_for)."""
        low = name.strip().lower()
        for L in self.get_lists():
            if L.get("title", "").lower() == low:
                return L
        return self.create_list(name, store_id=self.store_id_for(store_ref))

    @staticmethod
    def match_rows(list_obj: dict, item: str, unstruck_only: bool = False) -> list[dict]:
        rows = list_obj.get("rows", [])
        if unstruck_only:
            rows = [r for r in rows if not r.get("isStrikedOver")]
        low = str(item).strip().lower()
        exact = [r for r in rows if r.get("productName", "").lower() == low]
        if exact:
            return exact
        by_id = [r for r in rows if r.get("offlineId", "").upper() == str(item).upper()]
        if by_id:
            return by_id
        return [r for r in rows if low in r.get("productName", "").lower()]

    # ==================================================================
    # Fas 2: recept, butiker, erbjudanden, bonus
    # ==================================================================
    # ---------------------------------------------------- recept
    def get_recipe(self, recipe_id) -> dict:
        """Full receptdetalj (titel, tid, portioner, ingredientGroups, steg)."""
        return self._api("GET", f"{RECIPE_PATH}/recipes/{recipe_id}?api-version=2.0").json()

    def get_saved_recipe_refs(self) -> list[dict]:
        """Favoritmarkerade recept som referenser: [{recipeId, createdDate}]."""
        return self._api("GET", f"{RECIPE_PATH}/favorites").json().get("favorites", [])

    def get_random_recipes(self, count: int = 3) -> list[dict]:
        """Random-endpointen ger ETT recept per anrop (numberofrecipes ignoreras),
        så vi anropar den `count` gånger och dedupar."""
        out: list[dict] = []
        seen: set = set()
        for _ in range(max(1, count)):
            data = self._api("GET", f"{RECIPE_PATH}/recipes/random?numberofrecipes=1").json()
            rec = (data[0] if isinstance(data, list) and data
                   else data if isinstance(data, dict) and data.get("id") else None)
            if rec and rec.get("id") not in seen:
                seen.add(rec.get("id"))
                out.append(rec)
        return out

    @staticmethod
    def recipe_ingredient_texts(recipe: dict) -> list[str]:
        """Platt lista av ingrediensrader ('8 dl mjölk', '4 ägg') ur ett recept."""
        out = []
        for grp in recipe.get("ingredientGroups", []):
            for ing in grp.get("ingredients", []):
                t = (ing.get("text") or ing.get("ingredient") or "").strip()
                if t:
                    out.append(t)
        return out

    @staticmethod
    def recipe_summary(recipe: dict) -> dict:
        return {
            "id": recipe.get("id"),
            "title": recipe.get("title"),
            "cookingTime": recipe.get("cookingTime"),
            "difficulty": recipe.get("difficulty"),
            "ingredientCount": recipe.get("ingredientCount"),
            "rating": recipe.get("averageRating"),
            "portions": (recipe.get("details") or {}).get("portions"),
        }

    @staticmethod
    def aggregate_ingredients(recipes: list[dict]) -> list[dict]:
        """Slå ihop ingredienser från ett eller flera recept till varor
        {name, quantity, unit} (se to_item). Samma namn summeras, med
        omräkning mellan enheter av samma slag (2 msk + 1 dl olja → 1.3 dl,
        se merge_quantities); enheter som inte går ihop (g och st) blir
        separata varor. Varor utan mängd (salt) tas med en gång. Ordningen bevaras. Varje
        recepts andel sparas i recipes (se add_recipe_share), så att appen
        visar varför varan finns på listan.

        Mängder i enheter som saknar ICA-motsvarighet (3 klyftor vitlök)
        summeras per enhet men behålls i namnet – 'vitlök (3 klyftor)' – i
        stället för att bli '3 st'; deras receptandel får då ingen mängd.
        Saknas ingrediensnamn används receptraden som den är, utan separat
        mängd (den står redan i texten).

        ingredientId följer med som fallback_product_id: det används bara om
        namnet inte matchar en produkt, och aldrig för sammanslagning — det
        pekar ibland på en för grov produkt ('krossade tomater' → 'tomat')."""
        items: list[dict] = []
        raw_groups: dict[tuple, dict] = {}  # (namn, okänd enhet) → vara
        for r in recipes:
            rid = recipe_id_of(r)
            is_rid = rid is not None
            for grp in r.get("ingredientGroups", []):
                for ing in grp.get("ingredients", []):
                    name = (ing.get("ingredient") or "").strip()
                    if not name:
                        text = (ing.get("text") or "").strip()
                        if text:
                            it = to_item({"name": text,
                                          "fallback_product_id": ing.get("ingredientId")})
                            if is_rid:
                                add_recipe_share(it, rid, None, None)
                            items.append(it)
                        continue
                    unit = ing.get("unit")
                    first, _, rest = name.partition(" ")
                    if not unit and rest and first.lower() in _PACKAGE_WORDS:
                        unit, name = "förp", rest.strip()  # "förp majskorn (à 150 g)"
                    fallback = ing.get("ingredientId")
                    try:
                        it = to_item({"name": name, "quantity": ing.get("quantity"), "unit": unit,
                                      "fallback_product_id": fallback})
                    except ValueError:
                        # okänd enhet: summera per enhet, mängden hamnar i namnet nedan
                        raw = str(unit).strip()
                        g = raw_groups.get((name.lower(), raw.lower()))
                        if g is None:
                            g = to_item({"name": name, "fallback_product_id": fallback})
                            g["_raw"] = [raw, 0.0]
                            g["amount_in_name"] = True
                            raw_groups[(name.lower(), raw.lower())] = g
                            items.append(g)
                        g["_raw"][1] = round(g["_raw"][1] + (_to_number(ing.get("quantity")) or 0), 3)
                        if is_rid:
                            add_recipe_share(g, rid, None, None)
                        continue
                    if is_rid:
                        add_recipe_share(it, rid, it["quantity"], it["unit"])
                    items.append(it)
        for g in raw_groups.values():
            raw, q = g.pop("_raw")
            g["name"] = f"{g['name']} ({format_quantity(q)} {raw})"
        return plan_additions([], items)[0]

    # ---------------------------------------------------- butiker
    def get_favorite_store_ids(self) -> list[int]:
        return self._api("GET", f"{STORE_PATH}/favorites").json().get("favoriteStores", [])

    def get_store(self, store_id) -> dict:
        return self._api("GET", f"{STORE_PATH}/stores/{store_id}").json()

    def get_favorite_stores(self) -> list[dict]:
        """[{id, name, city}] — favoritbutiker med upplösta namn."""
        out = []
        for sid in self.get_favorite_store_ids():
            try:
                s = self.get_store(sid)
                out.append({"id": s.get("id", sid), "name": s.get("marketingName"),
                            "city": (s.get("address") or {}).get("city")})
            except IcaError:
                out.append({"id": sid, "name": None, "city": None})
        return out

    def store_id_for(self, ref=None) -> int:
        """Butiks-id för en ny lista: favoritbutiken ref (id eller namn), annars
        den primära (första favoriten). 0 = ingen butik (inga favoritbutiker,
        eller standardbutiken kunde inte hämtas — då skapas listan ändå)."""
        if ref is None or str(ref).strip() == "":
            try:
                ids = self.get_favorite_store_ids()
            except IcaError as e:
                _LOG.warning("Kunde inte hämta favoritbutik, skapar listan utan butik: %s", e)
                return 0
            return int(ids[0]) if ids else 0
        return int(self.resolve_store(ref)["id"])

    def resolve_store(self, ref=None) -> dict:
        """Hitta en favoritbutik via id eller namn. None = primär (första favoriten)."""
        stores = self.get_favorite_stores()
        if not stores:
            raise IcaError("Du har inga favoritbutiker i ICA-appen.")
        if ref is None or str(ref).strip() == "":
            return stores[0]
        s = str(ref).strip()
        for st in stores:
            if str(st["id"]) == s:
                return st
        low = s.lower()
        m = [st for st in stores if st.get("name") and low in st["name"].lower()]
        if len(m) == 1:
            return m[0]
        if len(m) > 1:
            raise IcaError(f"Flera butiker matchar {ref!r}: {[st['name'] for st in m]}")
        raise IcaError(f"Ingen favoritbutik matchar {ref!r}. "
                       f"Dina butiker: {[st['name'] for st in stores]}")

    # ---------------------------------------------------- erbjudanden
    def get_store_offers(self, store_id) -> list[dict]:
        return self._api("GET", f"{OFFER_PATH}/offersdiscounts/{store_id}").json().get("offers", [])

    @staticmethod
    def format_offer(o: dict) -> dict:
        pm = o.get("parsedMechanics") or {}
        deal = " ".join(v for v in (pm.get("value1"), pm.get("value2"),
                                    pm.get("value3"), pm.get("value4")) if v).strip()
        sign = pm.get("unitSign") or ""
        if deal and sign:
            deal = f"{deal}{sign}"
        return {
            "name": o.get("name"),
            "brand": o.get("brand"),
            "package": o.get("packageInformation"),
            "deal": deal or None,
            "category": (o.get("category") or {}).get("articleGroupName"),
            "personal": bool(o.get("isPersonal")),
            "requiresCard": bool(o.get("requiresLoyaltyCard")),
            "validTo": o.get("validTo"),
        }

    # ---------------------------------------------------- bonus
    def get_bonus(self) -> dict:
        return self._api("GET", f"{BONUS_PATH}/bonus/current").json()

    # ---------------------------------------------------- produkt (streckkod)
    def get_product(self, ean) -> dict | None:
        """Produktinfo för en EAN/GTIN, eller None om koden inte finns (404).
        Ogiltig streckkod ger IcaError utan nätverksanrop."""
        ean = validate_barcode(ean)
        r = self._api("GET", f"{PRODUCT_PATH}/product/{ean}", allow_404=True)
        return None if r.status_code == 404 else r.json()
