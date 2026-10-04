"""Gemensamma fixtures: offline-klient med falsk requests-session."""

import json

import pytest
from helpers import FakeResponse, FakeSession

from ica_mcp import client as ica_client
from ica_mcp.client import IcaClient


@pytest.fixture
def make_client(monkeypatch, tmp_path):
    for var in ("ICA_USER", "ICA_PASS"):
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "s.json"
    # explicit state-fil: hoppar över migreringen av en gammal
    # .ica_auth_state.json från cwd/repo-roten (en riktig token får aldrig
    # hamna i testernas tmp-kataloger)
    monkeypatch.setenv("ICA_STATE_FILE", str(path))
    monkeypatch.setattr(ica_client, "_legacy_state_candidates", lambda: [])

    def _make(handler=None, username="197801011234", password="hemligt", state=None):
        if state is not None:
            path.write_text(json.dumps(state), encoding="utf-8")
        c = IcaClient(username=username, password=password, state_file=str(path))
        c.session = FakeSession(handler or (lambda m, u, k: FakeResponse(500)))
        return c

    return _make
