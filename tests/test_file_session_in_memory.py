"""TELEGRAM_SESSION_FILE_IN_MEMORY loads a file session's auth key into memory so
several server processes can share one login without contending for the SQLite file."""

import os

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession, StringSession

from telegram_mcp import runtime
from telegram_mcp.singleton import session_identity

AUTH_KEY = bytes(range(256))
DC = (2, "149.154.167.51", 443)


@pytest.fixture
def session_file(tmp_path):
    name = str(tmp_path / "telegram_mcp_session")
    sqlite = SQLiteSession(name)
    sqlite.set_dc(*DC)
    sqlite.auth_key = AuthKey(data=AUTH_KEY)
    sqlite.save()
    sqlite.close()
    return name


def _snapshot(name):
    path = f"{name}.session"
    with open(path, "rb") as fh:
        return os.stat(path).st_mtime_ns, fh.read()


def test_auth_key_and_dc_are_copied_without_touching_the_file(session_file):
    before = _snapshot(session_file)

    session = runtime._file_session_in_memory(session_file)

    assert isinstance(session, StringSession)
    assert (session.dc_id, session.server_address, session.port) == DC
    assert session.auth_key.key == AUTH_KEY
    assert _snapshot(session_file) == before


def test_accepts_name_with_session_suffix(session_file):
    session = runtime._file_session_in_memory(f"{session_file}.session")
    assert session.auth_key.key == AUTH_KEY


def test_every_process_gets_the_same_identity(session_file):
    # Same auth key -> same lock identity, so TELEGRAM_SESSION_LOCK=shared lets
    # the processes coexist instead of refusing the second one.
    first = runtime._file_session_in_memory(session_file)
    second = runtime._file_session_in_memory(session_file)

    class _Client:
        def __init__(self, session):
            self.session = session

    assert session_identity(_Client(first)) == session_identity(_Client(second))
    assert session_identity(_Client(first)).startswith("string:")


def test_missing_file_falls_back_to_the_name(tmp_path):
    name = str(tmp_path / "nobody_logged_in")
    assert runtime._file_session_in_memory(name) == name


def test_off_by_default(session_file, monkeypatch):
    monkeypatch.delenv("TELEGRAM_SESSION_FILE_IN_MEMORY", raising=False)
    assert runtime._session_from_name(session_file) == session_file


def test_flag_switches_to_memory(session_file, monkeypatch):
    monkeypatch.setenv("TELEGRAM_SESSION_FILE_IN_MEMORY", "1")
    session = runtime._session_from_name(session_file)
    assert isinstance(session, StringSession)
    assert session.auth_key.key == AUTH_KEY


def test_default_account_uses_memory_session(session_file, monkeypatch):
    for key in list(os.environ):
        if key.startswith(("TELEGRAM_SESSION_STRING", "TELEGRAM_SESSION_NAME_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("TELEGRAM_SESSION_NAME", session_file)
    monkeypatch.setenv("TELEGRAM_SESSION_FILE_IN_MEMORY", "1")
    before = _snapshot(session_file)

    accounts = runtime._discover_accounts()

    session = accounts["default"].session
    assert isinstance(session, StringSession)
    assert session.auth_key.key == AUTH_KEY
    assert _snapshot(session_file) == before
