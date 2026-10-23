"""API key entered on the Settings page: stored safely, never returned, only changeable from this site."""

import os
import stat

import keyring
import pytest
from fastapi.testclient import TestClient
from keyring.backend import KeyringBackend

from app import secrets_store
from app.chat import model as chat_model
from app.chat.sandbox import _clean_env
from app.config import settings

KEY = "sk-ant-api03-" + "x" * 40 + "AbCd"


class MemoryKeyring(KeyringBackend):
    """Stand-in for the macOS Keychain, used only where a test installs it."""
    priority = -1  # never chosen automatically by keyring's backend discovery

    def __init__(self):
        super().__init__()
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(chat_model, "verify_key", lambda k: (True, "The key works."))
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_save_then_status_never_returns_the_key(client, tmp_path):
    assert client.get("/api/settings").json()["anthropic"]["configured"] is False
    r = client.put("/api/settings/anthropic-key", json={"key": KEY})
    assert r.status_code == 200, r.text
    st = r.json()["anthropic"]
    assert st["configured"] and st["masked"] == "sk-ant-…AbCd" and st["source"] in ("file", "keychain")
    for resp in (r, client.get("/api/settings"), client.get("/api/chat/status")):
        assert KEY not in resp.text
    if st["source"] == "file":  # no keychain on this machine: private file
        f = tmp_path / "secrets" / "anthropic_api_key"
        assert stat.S_IMODE(f.stat().st_mode) == 0o600 and stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert client.delete("/api/settings/anthropic-key").json()["anthropic"]["configured"] is False


def test_other_websites_cannot_change_the_key(client):
    r = client.put("/api/settings/anthropic-key", json={"key": KEY}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.put("/api/settings/anthropic-key", json={"key": KEY}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = client.delete("/api/settings/anthropic-key", headers={"Origin": "http://localhost:9999"})
    assert r.status_code == 403
    ok = client.put("/api/settings/anthropic-key", json={"key": KEY},
                    headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 200


def test_bad_keys_are_rejected_and_not_saved(client, monkeypatch):
    assert client.put("/api/settings/anthropic-key", json={"key": "hello"}).status_code == 422
    monkeypatch.setattr(chat_model, "verify_key", lambda k: (False, "Anthropic did not accept this key."))
    r = client.put("/api/settings/anthropic-key", json={"key": KEY})
    assert r.status_code == 422 and "did not accept" in r.json()["detail"]
    assert client.get("/api/settings").json()["anthropic"]["configured"] is False


def test_environment_key_takes_precedence(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    st = client.get("/api/settings").json()["anthropic"]
    assert st["source"] == "environment" and st["can_change"] is False and KEY not in str(st)
    assert client.put("/api/settings/anthropic-key", json={"key": KEY}).status_code == 409


def test_keychain_is_used_when_available(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    previous = keyring.get_keyring()
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    try:
        assert secrets_store.save_key(KEY) == "keychain"
        assert kr.store[(secrets_store.SERVICE, secrets_store.ACCOUNT)] == KEY
        assert not (tmp_path / "secrets" / "anthropic_api_key").exists()
        assert secrets_store.get_key() == (KEY, "keychain")
        # the chat client gets the saved key directly; it is not placed in the environment
        c = chat_model.ClaudeChat()
        assert c.client.api_key == KEY and "ANTHROPIC_API_KEY" not in os.environ
        secrets_store.delete_key()
        assert secrets_store.get_key() == (None, None)
    finally:
        keyring.set_keyring(previous)


def test_assistant_code_gets_no_keychain_and_no_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    env = _clean_env(tmp_path)
    assert env["PYTHON_KEYRING_BACKEND"] == "keyring.backends.fail.Keyring"
    assert KEY not in env.values()
