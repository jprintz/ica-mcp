"""
ica_mcp.products — ICA:s produktregister (shoppinglistservice/v1/articles).

Registret har ~6 900 generiska produkter ('mjölk', 'krossad tomat' …). När man
väljer ett förslag i ICA-appen kopplas raden till en sådan produkt: radens
sourceId blir produktens id (fritext har negativt sourceId). Här finns exakt
matchning, sökning (så att LLM:en själv kan hitta rätt produkt) och en cache i
minnet + på disk — registret är ~3 MB och ändras sällan.

Verifierat mot ICA (2026-10-04):
  - fritext som inte matchar en produkt hamnar under Ospecificerad i appen; en
    befintlig rad kan kopplas i efterhand eller få en avdelning (changedRows)
  - appen själv använder produkter med status 2; övriga statusar är mest nyare
    dubbletter, men ~50 namn finns bara med annan status
  - samma namn kan finnas flera gånger; lägst id är den äldre basprodukten
  - streckkodsuppslagens articleId och receptens ingredientId är id:n i
    registret; ingredientId är ibland för grovt ('krossade tomater' → 'tomat',
    färska) och används därför bara när namnet inte matchar
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import logging
import os
import tempfile
import re
import threading
import unicodedata
from typing import Callable, Literal, get_args

import platformdirs

_LOG = logging.getLogger("ica_mcp.products")

ARTICLES_PATH = "sverige/digx/mobile/shoppinglistservice/v1/articles"

# ICA:s avdelningar (artikelgrupper; GET .../articles/articlegroups, oförändrade
# sedan 2015). Avdelningen styr var varan hamnar i appen: fritext som inte
# matchar en produkt får 12 = Ospecificerad, om man inte anger en själv.
Category = Literal["Bröd, kex och bageri", "Frukt & Grönt", "Djupfryst", "Färskvaror",
                   "Hälsa & Skönhet", "Kassa", "Skafferivaror", "Mejeri", "Hem & Fritid"]
CATEGORY_IDS: dict[str, int] = dict(zip(get_args(Category), (3, 4, 5, 6, 7, 8, 9, 10, 11)))
UNSPECIFIED = 12
ARTICLE_GROUPS: dict[int, str] = {**{v: k for k, v in CATEGORY_IDS.items()},
                                  UNSPECIFIED: "Ospecificerad"}
CACHE_TTL = dt.timedelta(hours=24)
RETRY_AFTER_FAILURE = dt.timedelta(minutes=30)
ACTIVE = 2  # status på produkterna som ICA-appen själv använder
_CACHE_VERSION = 1
_FIELDS = ("id", "name", "pluralName", "parentId", "parentIdExtended", "status")
# Butiksformatens kategorinamn ('Grönsakskonserver' …) — mer beskrivande än
# artikelgruppen och ifyllt för ~97 % av produkterna.
_CATEGORY_FIELDS = ("maxiFormatCategoryName", "supermarketFormatCategoryName",
                    "kvantumFormatCategoryName", "naraFormatCategoryName")


def resolve_cache_file() -> str:
    """Var produktregistret cachas: $ICA_CACHE_DIR/products.json, annars i
    platformdirs user_cache_dir('ica-mcp')."""
    d = os.environ.get("ICA_CACHE_DIR") or platformdirs.user_cache_dir("ica-mcp", appauthor=False)
    return os.path.join(os.path.abspath(os.path.expanduser(d)), "products.json")


_PARENS = re.compile(r"\([^)]*\)")


def _norm(s) -> str:
    """Jämförelsenyckel: NFC, gemener, enkla mellanslag."""
    return " ".join(unicodedata.normalize("NFC", str(s or "")).lower().split())


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def trim_article(a: dict) -> dict:
    """Behåll bara fälten vi använder (håller disk-cachen liten)."""
    out = {k: a.get(k) for k in _FIELDS}
    out["category"] = next((a[f] for f in _CATEGORY_FIELDS if a.get(f)), None)
    return out


def _stem_match(a: str, b: str) -> bool:
    """Grov böjningsmatchning: 'tomater'~'tomat', 'krossade'~'krossad'."""
    if a == b:
        return True
    n = min(len(a), len(b))
    if n < 3:
        return False
    common = len(os.path.commonprefix([a, b]))
    return common >= min(5, n)


class ProductCatalog:
    """Produktregistret i minnet: uppslag på id, exakt namn och sökning."""

    def __init__(self, articles: list[dict]):
        self.by_id: dict[int, dict] = {}
        self.by_name: dict[str, dict] = {}
        self.by_plural: dict[str, dict] = {}
        # Bästa produkt först, så att setdefault väljer den vid dubbletter.
        for a in sorted(articles, key=self._rank):
            if not isinstance(a.get("id"), int) or not a.get("name"):
                continue
            self.by_id[a["id"]] = a
            self.by_name.setdefault(_norm(a["name"]), a)
            if a.get("pluralName"):
                self.by_plural.setdefault(_norm(a["pluralName"]), a)
        # (nyckel, produkt, är_plural) — exakt namnträff ska slå exakt pluralträff
        self._keys = ([(k, a, False) for k, a in self.by_name.items()]
                      + [(k, a, True) for k, a in self.by_plural.items()])

    def __len__(self) -> int:
        return len(self.by_id)

    @staticmethod
    def _rank(a: dict) -> tuple:
        """Status 2 (som appen använder) före övriga, sedan lägst id."""
        return (a.get("status") != ACTIVE, a.get("id") or 0)

    def get(self, product_id) -> dict | None:
        try:
            return self.by_id.get(int(product_id))
        except (TypeError, ValueError):
            return None

    def match(self, name) -> dict | None:
        """Exakt matchning (skiftlägesokänslig) på namn, annars pluralnamn —
        även med anteckningar inom parentes borttagna ('krossade tomater (à ca
        400 g)'). Ingen gissning utöver det."""
        for k in dict.fromkeys((_norm(name), _norm(_PARENS.sub(" ", str(name or ""))))):
            if k and (k in self.by_name or k in self.by_plural):
                return self.by_name.get(k) or self.by_plural.get(k)
        return None

    def search(self, query, limit: int = 10) -> list[dict]:
        """Rangordnade produkter för en fritextfråga: exakt träff, början av
        namnet, delsträng ('tomat' → 'körsbärstomat'), namnet som ord i frågan
        ('gul lök' → 'lök'), böjningsform ('krossade tomater' → 'krossad
        tomat'), sist stavningslikhet."""
        q = _norm(query)
        if not q:
            return []
        qtoks = q.split()
        sm = difflib.SequenceMatcher()
        sm.set_seq2(q)  # cachas — bara set_seq1 per nyckel
        best: dict[int, tuple[float, dict]] = {}
        for key, a, plural in self._keys:
            s = self._score(q, qtoks, key, sm) - (0.5 if plural else 0)
            if s and s > best.get(a["id"], (0.0,))[0]:
                best[a["id"]] = (s, a)
        ranked = sorted(best.values(), key=lambda x: (-x[0], self._rank(x[1])))
        return [a for _, a in ranked[:max(1, int(limit))]]

    @staticmethod
    def _score(q: str, qtoks: list[str], key: str, sm: difflib.SequenceMatcher) -> float:
        # Band med 10–20 poängs avstånd; inom bandet vinner kortare/närmare namn.
        if key == q:
            return 100.0
        if key.startswith(q):
            return 80 - len(key) / 100
        if q in key:
            return 70 - len(key) / 100
        if f" {key} " in f" {q} ":
            return 60 - (len(q) - len(key)) / 100
        ktoks = key.split()
        if all(any(_stem_match(t, k) for k in ktoks) for t in qtoks):
            return 50 - abs(len(q) - len(key)) / 100
        sm.set_seq1(key)
        if sm.real_quick_ratio() < 0.6 or sm.quick_ratio() < 0.6:
            return 0.0
        r = sm.ratio()
        return 40 * r if r >= 0.6 else 0.0

    @staticmethod
    def summary(a: dict) -> dict:
        """Kompakt vy för LLM:en."""
        d = {"product_id": a["id"], "name": a["name"]}
        if a.get("pluralName"):
            d["plural"] = a["pluralName"]
        if a.get("category"):
            d["category"] = a["category"]
        return d


class ProductCache:
    """Registret cachat i minnet (serverns livstid) och på disk (CACHE_TTL),
    så att en ny session oftast slipper hämta det. Misslyckas hämtningen
    används en äldre cache om den finns, annars None; nytt försök tidigast
    efter RETRY_AFTER_FAILURE."""

    def __init__(self, fetch: Callable[[], list[dict]], path: str | None = None,
                 ttl: dt.timedelta = CACHE_TTL):
        self._fetch = fetch
        self.path = path or resolve_cache_file()
        self.ttl = ttl
        self._catalog: ProductCatalog | None = None
        self._fetched: dt.datetime | None = None
        self._next_try: dt.datetime | None = None
        self._lock = threading.Lock()

    def get(self) -> ProductCatalog | None:
        with self._lock:
            now = _now()
            if self._catalog is None:
                self._load_disk()
            if self._catalog is not None and now - self._fetched < self.ttl:
                return self._catalog
            if self._next_try is not None and now < self._next_try:
                return self._catalog  # gammal cache eller None
            try:
                articles = [trim_article(a) for a in self._fetch()]
                if not articles:
                    raise ValueError("tomt produktregister")
            except Exception as e:  # noqa: BLE001 — nätverk, 451, auth, ogiltigt svar
                _LOG.warning("Kunde inte hämta produktregistret: %s", e)
                self._next_try = now + RETRY_AFTER_FAILURE
                return self._catalog
            self._catalog, self._fetched, self._next_try = ProductCatalog(articles), now, None
            self._save_disk(articles, now)
            return self._catalog

    def _load_disk(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version") != _CACHE_VERSION:
                return
            fetched = dt.datetime.fromisoformat(data["fetched"])
            catalog = ProductCatalog(data["articles"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return  # saknas eller trasig — hämtas på nytt
        if len(catalog):
            self._catalog, self._fetched = catalog, fetched

    def _save_disk(self, articles: list[dict], fetched: dt.datetime) -> None:
        """Atomisk skrivning (temp-fil + os.replace); fel loggas bara."""
        d = os.path.dirname(self.path)
        tmp = None
        try:
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".products.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": _CACHE_VERSION, "fetched": fetched.isoformat(),
                           "articles": articles}, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            _LOG.warning("Kunde inte spara produktregistret: %s", e)
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
