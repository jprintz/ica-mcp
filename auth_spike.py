#!/usr/bin/env python3
"""
ICA auth-spike — verifierar att vi kan logga in på ICA:s nya OAuth-flöde
(Curity @ ims.icagruppen.se) med personnummer + lösenord (utan BankID) och
hämta dina inköpslistor från gatewayen (apimgw-pub.ica.se).

Detta är en fristående port av auth-flödet i LazyTarget/ha-ica-todo, utan
Home Assistant-beroenden. Syftet är att bevisa premissen innan vi bygger
MCP-servern — och att fånga den FAKTISKA JSON-formen på inköpslistorna.

>>> MÅSTE KÖRAS FRÅN EN SVENSK IP <<<
apimgw-pub.ica.se svarar 451 (Unavailable For Legal Reasons) till icke-svenska
IP:n. Kör alltså detta på din hemmamaskin / homelab, inte på en US/DE-VPS.

Användning:
    pip install -r requirements.txt         # requests (PyJWT valfritt)
    export ICA_USER=ÅÅMMDDXXXX               # ditt personnummer
    export ICA_PASS='ditt-lösenord'          # (eller skriv in interaktivt)
    python auth_spike.py                     # -v för debug, --fresh för ny inloggning

Vad den gör:
  1. Hämtar bootstrap-token (client_credentials, scope=dcr)
  2. Dynamisk klientregistrering (POST /register) -> per-install client_id/secret
  3. PKCE authorize -> login-formulär
  4. POSTar personnummer + lösenord -> SSO-token
  5. Byter kod mot access_token / refresh_token
  6. GET /shoppinglists och skriver ut rå JSON (så vi lär oss schemat)

Auth-state (registrerad klient + token) cachas i .ica_auth_state.json så att
återkörningar kan testa refresh-vägen istället för full inloggning.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
import hashlib
import json
import logging
import os
import re
import sys
from os import urandom
from urllib.parse import urlparse, parse_qs

import requests

try:
    import jwt  # PyJWT — bara för att läsa ut namnet ur id_token (valfritt)
except ImportError:  # pragma: no cover
    jwt = None

_LOG = logging.getLogger("ica_spike")

# --------------------------------------------------------------------------
# Konstanter (verbatim från LazyTarget/ha-ica-todo const.py)
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

SHOPPINGLISTS_ENDPOINT = (
    f"{API_BASE}/sverige/digx/mobile/shoppinglistservice/v1/shoppinglists"
)

# LazyTarget använder bare requests default-UA (och det funkar). Vi sätter en
# neutral browser-UA för att minska risken att F5/Akamai flaggar python-requests
# som bot. Om inloggning strular: prova att sätta USER_AGENT = None.
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ica_auth_state.json")


class IcaAuthError(RuntimeError):
    pass


class IcaAuth:
    def __init__(self, username: str, password: str, session: requests.Session | None = None):
        self.username = username
        self.password = password
        self.session = session or requests.Session()
        if USER_AGENT:
            self.session.headers["User-Agent"] = USER_AGENT

    # ---- små hjälpare ----------------------------------------------------
    @staticmethod
    def _qs(location: str, key: str) -> str:
        """Robust extraktion av en query-param ur en (ev. custom-scheme) URL."""
        parsed = urlparse(location)
        vals = parse_qs(parsed.query).get(key)
        if vals:
            return vals[0]
        m = re.search(rf"[?&]{re.escape(key)}=([^&\s]+)", location)
        if not m:
            raise IcaAuthError(f"Hittade inte '{key}' i redirect: {location!r}")
        return m.group(1)

    @staticmethod
    def _hidden(html: str, name: str) -> str:
        """Plocka ut ett dolt <input>-värde oavsett attribut-ordning."""
        for pat in (
            rf'name="{name}"[^>]*value="([^"]*)"',
            rf'value="([^"]*)"[^>]*name="{name}"',
        ):
            m = re.search(pat, html)
            if m:
                return m.group(1)
        raise IcaAuthError(
            f"Hittade inte dolt fält '{name}' i login-svaret. "
            "Troligen fel personnummer/lösenord (eller så kräver kontot BankID)."
        )

    @staticmethod
    def _pkce() -> tuple[str, str]:
        verifier = base64.urlsafe_b64encode(urandom(40)).decode("utf-8")
        verifier = re.sub("[^a-zA-Z0-9]+", "", verifier)
        digest = hashlib.sha256(verifier.encode("utf-8")).digest()
        challenge = base64.urlsafe_b64encode(digest).decode("utf-8").replace("=", "")
        return challenge, verifier

    def _post(self, url, **kw):
        kw.setdefault("timeout", 30)
        r = self.session.post(url, **kw)
        _LOG.debug("[POST] %s -> %s", url, r.status_code)
        return r

    def _get(self, url, **kw):
        kw.setdefault("timeout", 30)
        r = self.session.get(url, **kw)
        _LOG.debug("[GET] %s -> %s", url, r.status_code)
        return r

    # ---- OAuth-stegen ----------------------------------------------------
    def bootstrap_token(self) -> str:
        r = self._post(
            TOKEN_ENDPOINT,
            data={
                "client_id": DCR_CLIENT_ID,
                "client_secret": DCR_CLIENT_SECRET,
                "grant_type": "client_credentials",
                "scope": "dcr",
                "response_type": "token",
            },
        )
        r.raise_for_status()
        return r.json()["access_token"]

    def register_client(self, bootstrap: str) -> dict:
        r = self._post(
            REGISTER_ENDPOINT,
            json={"software_id": DCR_SOFTWARE_ID},
            headers={"Authorization": f"Bearer {bootstrap}"},
        )
        r.raise_for_status()
        client = r.json()
        _LOG.debug("Registrerad klient: client_id=%s scope=%s",
                   client.get("client_id"), client.get("scope"))
        return client

    def authorize(self, client: dict, challenge: str) -> str:
        r = self._get(
            AUTHORIZE_ENDPOINT,
            params={
                "client_id": client["client_id"],
                "scope": client["scope"],
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "prompt": "login",
                "acr": ACR,
            },
            allow_redirects=False,
        )
        r.raise_for_status()
        location = r.headers.get("Location")
        if not location:
            raise IcaAuthError("Förväntade en Location-redirect från /authorize")
        state = self._qs(location, "state")
        # GET redirecten -> renderar login-formuläret och sätter cookies
        self._get(location).raise_for_status()
        return state

    def submit_credentials(self, state: str) -> str:
        r = self._post(
            LOGIN_ENDPOINT,
            data={"userName": self.username, "password": self.password},
        )
        r.raise_for_status()
        server_state = self._hidden(r.text, "state")
        token = self._hidden(r.text, "token")
        if server_state != state:
            _LOG.warning("State skiljer sig! klient=%s server=%s", state, server_state)
        return token

    def exchange_code(self, client: dict, state: str, sso_token: str, verifier: str) -> dict:
        r = self._post(
            AUTHORIZE_ENDPOINT,
            params={"client_id": client["client_id"], "forceAuthN": "true", "acr": ACR},
            data={"token": sso_token, "state": state},
            allow_redirects=False,
        )
        r.raise_for_status()
        location = r.headers.get("Location")
        if not location:
            raise IcaAuthError("Förväntade en Location-redirect med ?code=")
        code = self._qs(location, "code")

        r = self._post(
            TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "grant_type": "authorization_code",
                "scope": client["scope"],
                "response_type": "token",
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            },
        )
        r.raise_for_status()
        return r.json()

    def refresh(self, client: dict, refresh_token: str) -> dict:
        basic = base64.b64encode(
            f"{client['client_id']}:{client['client_secret']}".encode()
        ).decode()
        r = self._post(
            TOKEN_ENDPOINT,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            headers={"Authorization": f"Basic {basic}"},
        )
        r.raise_for_status()
        return r.json()

    # ---- full inloggning -------------------------------------------------
    def full_login(self) -> dict:
        _LOG.info("Steg 1/5: bootstrap-token …")
        bootstrap = self.bootstrap_token()
        _LOG.info("Steg 2/5: registrerar klient (DCR) …")
        client = self.register_client(bootstrap)
        challenge, verifier = self._pkce()
        _LOG.info("Steg 3/5: authorize (PKCE) …")
        state = self.authorize(client, challenge)
        _LOG.info("Steg 4/5: postar inloggningsuppgifter …")
        sso_token = self.submit_credentials(state)
        _LOG.info("Steg 5/5: byter kod mot access_token …")
        token = self.exchange_code(client, state, sso_token, verifier)
        return {"client": client, "token": _with_expiry(token)}


def _with_expiry(token: dict) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    token = dict(token)
    token["expiry"] = (now + dt.timedelta(seconds=token.get("expires_in", 2592000))).isoformat()
    return token


def _load_state() -> dict | None:
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(STATE_FILE, 0o600)
    except OSError:
        pass


def _expired(token: dict) -> bool:
    exp = token.get("expiry")
    if not exp:
        return True
    try:
        return dt.datetime.fromisoformat(exp) <= dt.datetime.now(dt.timezone.utc)
    except ValueError:
        return True


def get_auth_state(username: str, password: str, fresh: bool = False) -> dict:
    """Returnerar {client, token}, med cache + refresh när möjligt."""
    auth = IcaAuth(username, password)
    state = None if fresh else _load_state()

    if state and state.get("token") and not _expired(state["token"]):
        _LOG.info("Använder cachat, giltigt access_token.")
        return state

    if state and state.get("client") and state.get("token", {}).get("refresh_token"):
        _LOG.info("Access_token utgånget — provar refresh …")
        try:
            new = auth.refresh(state["client"], state["token"]["refresh_token"])
            state["token"] = _with_expiry({**state["token"], **new})
            _save_state(state)
            return state
        except requests.HTTPError as e:
            _LOG.info("Refresh misslyckades (%s) — gör full inloggning.",
                      e.response.status_code if e.response else "?")

    state = auth.full_login()
    _save_state(state)
    return state


def fetch_shopping_lists(access_token: str) -> tuple[int, object]:
    r = requests.get(
        SHOPPINGLISTS_ENDPOINT,
        headers={"Authorization": f"Bearer {access_token}", "User-Agent": USER_AGENT or "python-requests"},
        timeout=30,
    )
    body: object
    try:
        body = r.json()
    except ValueError:
        body = r.text
    return r.status_code, body


def _diagnose_http_error(e: requests.HTTPError) -> None:
    resp = e.response
    if resp is None:
        print(f"\n✖ Nätverksfel: {e}", file=sys.stderr)
        return
    print(f"\n✖ HTTP {resp.status_code} mot {resp.url}", file=sys.stderr)
    if resp.status_code == 451:
        print("  → 451 = geo-block. Du kör inte från en svensk IP. Kör detta hemma / "
              "på homelab, inte på en utländsk VPS.", file=sys.stderr)
    elif resp.status_code in (401, 403):
        print("  → Auth nekad. Kolla personnummer/lösenord, eller bot-block (F5/Akamai).",
              file=sys.stderr)
    snippet = (resp.text or "")[:500]
    if snippet:
        print(f"  Svarskropp (500 tecken): {snippet}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="ICA auth-spike")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug-loggning")
    ap.add_argument("--fresh", action="store_true", help="ignorera cache, tvinga full inloggning")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # Dämpa requests/urllib3-brus om inte -v
    if not args.verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    username = os.environ.get("ICA_USER") or input("Personnummer (ÅÅMMDDXXXX): ").strip()
    password = os.environ.get("ICA_PASS") or getpass.getpass("Lösenord: ")
    if not username or not password:
        print("Saknar personnummer eller lösenord.", file=sys.stderr)
        return 2

    try:
        state = get_auth_state(username, password, fresh=args.fresh)
    except requests.HTTPError as e:
        _diagnose_http_error(e)
        return 1
    except IcaAuthError as e:
        print(f"\n✖ {e}", file=sys.stderr)
        return 1

    token = state["token"]
    who = ""
    if jwt and token.get("id_token"):
        try:
            claims = jwt.decode(token["id_token"], options={"verify_signature": False})
            who = f" ({claims.get('given_name', '')} {claims.get('family_name', '')})".rstrip()
        except Exception:  # noqa: BLE001
            pass
    print(f"\n✅ Inloggad{who}. access_token utgår {token.get('expiry')}.")
    print(f"   Har refresh_token: {'ja' if token.get('refresh_token') else 'nej'}")

    print("\n→ Hämtar inköpslistor …")
    try:
        status, body = fetch_shopping_lists(token["access_token"])
    except requests.RequestException as e:
        print(f"✖ Nätverksfel vid /shoppinglists: {e}", file=sys.stderr)
        return 1

    if status == 451:
        print("✖ 451 geo-block på /shoppinglists — kör från svensk IP.", file=sys.stderr)
        return 1
    if status != 200:
        print(f"✖ HTTP {status} på /shoppinglists:", file=sys.stderr)
        print(json.dumps(body, ensure_ascii=False, indent=2)[:2000], file=sys.stderr)
        return 1

    print(f"✅ HTTP 200. Rå JSON (så vi lär oss schemat för MCP-bygget):\n")
    print(json.dumps(body, ensure_ascii=False, indent=2))

    # Snabb sammanfattning oavsett exakt schema
    lists = body.get("shoppingLists") or body.get("ShoppingLists") if isinstance(body, dict) else None
    if isinstance(lists, list):
        print(f"\n📋 Hittade {len(lists)} lista/listor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
