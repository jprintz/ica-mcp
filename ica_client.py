"""
ica_client.py — återanvändbar klient mot ICA:s inofficiella API.

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
import os
import random
import re
import threading
import uuid
from os import urandom
from urllib.parse import urlparse, parse_qs

import requests

try:
    import jwt  # PyJWT — valfritt, bara för att läsa användarnamn ur id_token
except ImportError:  # pragma: no cover
    jwt = None

_LOG = logging.getLogger("ica_client")

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

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)

DEFAULT_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".ica_auth_state.json"
)


class IcaError(RuntimeError):
    """Generiskt ICA-fel (nätverk, HTTP, validering)."""


class IcaAuthError(IcaError):
    """Inloggning/refresh misslyckades."""


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _ts() -> str:
    """ICA:s tidsstämpelformat, t.ex. 2026-07-12T16:40:08Z."""
    return _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")


class IcaClient:
    """Trådsäker(ish) klient. En instans per konto."""

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        state_file: str = DEFAULT_STATE_FILE,
        user_agent: str | None = USER_AGENT,
        early_refresh_seconds: int = 60,
    ) -> None:
        self.username = username or os.environ.get("ICA_USER")
        self.password = password or os.environ.get("ICA_PASS")
        self.state_file = state_file
        self.early_refresh = early_refresh_seconds
        self.session = requests.Session()
        if user_agent:
            self.session.headers["User-Agent"] = user_agent
        self._lock = threading.RLock()
        self._state = self._load_state()

    # ----------------------------------------------------------------- state
    def _load_state(self) -> dict:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                _LOG.warning("Kunde inte läsa %s, börjar om.", self.state_file)
        return {}

    def _save_state(self) -> None:
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
            os.chmod(self.state_file, 0o600)
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
                "Saknar inloggningsuppgifter (sätt ICA_USER/ICA_PASS) och "
                "refresh gick inte att använda."
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
    def _api(self, method: str, path: str, json_body=None, _retry=True) -> requests.Response:
        url = f"{API_BASE}/{path}"
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        data = None
        if json_body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            data = json.dumps(json_body)
        r = self.session.request(method, url, headers=headers, data=data, timeout=30)
        if r.status_code == 401 and _retry:
            self._access_token(force_refresh=True)
            return self._api(method, path, json_body, _retry=False)
        if r.status_code == 451:
            raise IcaError("HTTP 451 från ICA-gatewayen — icke-svensk IP (geo-block). "
                           "Kör servern från svensk egress.")
        if not r.ok:
            raise IcaError(f"ICA {method} {path} → HTTP {r.status_code}: {(r.text or '')[:300]}")
        return r

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

    def create_list(self, title: str, comment: str = "") -> dict:
        offline_id = str(uuid.uuid4()).upper()
        body = {"offlineId": offline_id, "title": title, "commentText": comment,
                "sortingStore": 0, "rows": [], "latestChange": _ts()}
        self._api("POST", SL_PATH, body)
        return self.get_list_raw(offline_id)

    def delete_list(self, offline_id: str) -> None:
        self._api("DELETE", f"{SL_PATH}/{offline_id}")

    def _sync(self, offline_id: str, payload: dict) -> dict:
        return self._api("POST", f"{SL_PATH}/{offline_id}/sync", payload).json()

    def add_rows(self, offline_id: str, items: list) -> dict:
        rows = []
        for it in items:
            if isinstance(it, str):
                it = {"name": it}
            row = {
                "offlineId": str(uuid.uuid4()).upper(),
                "productName": it["name"],
                "sourceId": -random.randint(10**6, 10**9),  # negativ = fri text
                "isStrikedOver": False,
                "recipes": [],
            }
            if it.get("quantity") is not None:
                row["quantity"] = float(it["quantity"])
            if it.get("unit"):
                row["unit"] = it["unit"]
            rows.append(row)
        return self._sync(offline_id, {"createdRows": rows})

    def change_rows(self, offline_id: str, rows: list[dict]) -> dict:
        for r in rows:
            r["latestChange"] = _ts()
        return self._sync(offline_id, {"changedRows": rows})

    def delete_rows(self, offline_id: str, row_offline_ids: list[str]) -> dict:
        return self._sync(offline_id, {"deletedRows": list(row_offline_ids)})

    # ---------------------------------------------------- resolvers
    def resolve_list(self, ref: str | int | None = None) -> dict:
        """Hitta en lista via titel, numeriskt id eller offlineId. None = primär."""
        lists = self.get_lists()
        if not lists:
            raise IcaError("Du har inga inköpslistor.")
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
