"""Tester för auth-state-filen: rättigheter, atomisk skrivning, delning mellan processer."""

import datetime as dt
import json
import os
import sys

import pytest

from ica_mcp import client as c
from ica_mcp.client import IcaAuthError, IcaClient


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("ICA_USER", "ICA_PASS", "ICA_STATE_FILE"):
        monkeypatch.delenv(k, raising=False)


def _mk(tmp_path, name="state.json"):
    return IcaClient(username=None, password=None, state_file=str(tmp_path / name))


def _tok(access, refresh="r1", minutes=30):
    exp = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).isoformat()
    return {"access_token": access, "refresh_token": refresh, "expiry": exp}


def _no_http(*a, **k):
    pytest.fail("inget HTTP-anrop förväntades")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-rättigheter")
def test_file_mode_is_0600(tmp_path):
    cl = _mk(tmp_path)
    cl._state = {"token": _tok("a")}
    cl._save_state()
    assert os.stat(cl.state_file).st_mode & 0o777 == 0o600


@pytest.mark.parametrize("target", ["json.dump", "os.replace"])
def test_failed_write_keeps_old_file_and_no_temp(tmp_path, monkeypatch, target):
    cl = _mk(tmp_path)
    cl._state = {"token": _tok("old")}
    cl._save_state()
    before = (tmp_path / "state.json").read_text(encoding="utf-8")
    cl._state = {"token": _tok("new")}

    def boom(*a, **k):
        raise OSError("disk full")

    mod, attr = target.split(".")
    with monkeypatch.context() as m:
        m.setattr(getattr(c, mod), attr, boom)
        cl._save_state()  # ska logga, inte kasta
    assert (tmp_path / "state.json").read_text(encoding="utf-8") == before
    assert json.loads(before)["token"]["access_token"] == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_no_temp_files_after_success(tmp_path):
    cl = _mk(tmp_path)
    cl._state = {"token": _tok("a")}
    cl._save_state()
    cl._save_state()
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_adopts_newer_valid_token_from_other_process(tmp_path, monkeypatch):
    client = {"client_id": "x", "client_secret": "y"}
    a = _mk(tmp_path)
    a._state = {"client": client, "token": _tok("old", minutes=-5)}
    a._save_state()
    b = _mk(tmp_path)  # "annan process" delar samma fil
    b._state = {"client": client, "token": _tok("new", "r2")}
    b._save_state()
    monkeypatch.setattr(a.session, "post", _no_http)
    monkeypatch.setattr(a.session, "get", _no_http)
    assert a._access_token() == "new"
    assert a._state["token"]["refresh_token"] == "r2"


def test_force_refresh_adopts_other_process_token(tmp_path, monkeypatch):
    client = {"client_id": "x", "client_secret": "y"}
    a = _mk(tmp_path)
    a._state = {"client": client, "token": _tok("rejected")}
    a._save_state()
    b = _mk(tmp_path)
    b._state["token"] = _tok("fresh", "r2")
    b._save_state()
    monkeypatch.setattr(a.session, "post", _no_http)
    assert a._access_token(force_refresh=True) == "fresh"


def test_adopts_rotated_refresh_token_before_refreshing(tmp_path, monkeypatch):
    client = {"client_id": "x", "client_secret": "y"}
    a = _mk(tmp_path)
    a._state = {"client": client, "token": _tok("old", "r1", minutes=-5)}
    # filen har en roterad refresh-token men en utgången access-token
    (tmp_path / "state.json").write_text(
        json.dumps({"client": client, "token": _tok("old2", "r2", minutes=-5)}))
    sent = {}

    class R:
        def raise_for_status(self): pass
        def json(self): return {"access_token": "z", "expires_in": 900}

    def post(url, data=None, **k):
        sent.update(data)
        return R()

    monkeypatch.setattr(a.session, "post", post)
    assert a._access_token() == "z"
    assert sent["refresh_token"] == "r2"


def test_missing_location_raises_auth_error(tmp_path, monkeypatch):
    cl = IcaClient(username="197801011234", password="pw", state_file=str(tmp_path / "s.json"))

    class R:
        status_code = 200
        headers = {}
        text = ""
        def raise_for_status(self): pass
        def json(self): return {"access_token": "b", "client_id": "cid", "client_secret": "cs", "scope": "s"}

    monkeypatch.setattr(cl.session, "post", lambda *a, **k: R())
    monkeypatch.setattr(cl.session, "get", lambda *a, **k: R())
    with pytest.raises(IcaAuthError, match="redirect"):
        cl._full_login()


def _idt(sub):
    import jwt
    return jwt.encode({"sub": sub}, "test-nyckel-som-är-lång-nog-för-hs256", algorithm="HS256")


def test_same_account_is_adopted_silently(tmp_path, monkeypatch, caplog):
    client = {"client_id": "x", "client_secret": "y"}
    a = _mk(tmp_path)
    a._state = {"client": client, "token": {**_tok("old", minutes=-5), "id_token": _idt("A")}}
    a._save_state()
    b = _mk(tmp_path)
    b._state = {"client": client, "token": {**_tok("new", "r2"), "id_token": _idt("A")}}
    b._save_state()
    monkeypatch.setattr(a.session, "post", _no_http)
    with caplog.at_level("WARNING", logger="ica_mcp.client"):
        assert a._access_token() == "new"
    assert "annat ICA-konto" not in caplog.text


def test_other_account_wins_with_warning(tmp_path, monkeypatch, caplog):
    # Den senaste inloggningen vinner (annars skrivs den över vid nästa
    # refresh), men bytet ska synas i loggen.
    a = _mk(tmp_path)
    a._state = {"client": {"client_id": "a"}, "token": {**_tok("tokA", minutes=-5), "id_token": _idt("A")}}
    a._save_state()
    b = _mk(tmp_path)
    b._state = {"client": {"client_id": "b"}, "token": {**_tok("tokB", "rB"), "id_token": _idt("B")}}
    b._save_state()
    monkeypatch.setattr(a.session, "post", _no_http)
    with caplog.at_level("WARNING", logger="ica_mcp.client"):
        assert a._access_token() == "tokB"
    assert "annat ICA-konto" in caplog.text
    assert IcaClient._account_of(a._state) == "B"


def test_account_of_without_id_token():
    assert IcaClient._account_of({}) is None
    assert IcaClient._account_of({"token": {"id_token": "inte-en-jwt"}}) is None
