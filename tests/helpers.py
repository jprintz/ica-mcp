"""Delade testhjälpare (importeras som `helpers`; tests/ ligger på sys.path via
pytest-inställningen pythonpath, så det fungerar i alla import-lägen)."""

import datetime as dt
import json

import requests


class FakeResponse:
    """Minimal ersättare för requests.Response."""

    def __init__(self, status=200, json_data=None, text="", headers=None):
        self.status_code = status
        self._json = json_data
        self.text = text if json_data is None else json.dumps(json_data)
        self.headers = headers or {}

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._json

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    """Spelar in alla anrop och svarar via ``handler(method, url, kwargs)``."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls.append((method.upper(), url, kwargs))
        return self.handler(method.upper(), url, kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


def valid_state(access="ACC", refresh="REF", seconds=3600):
    exp = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat()
    return {
        "client": {"client_id": "cid", "client_secret": "csec", "scope": "sc"},
        "token": {"access_token": access, "refresh_token": refresh, "expiry": exp},
    }
