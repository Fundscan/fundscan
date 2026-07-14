"""
Tests for magic-link auth flow and session cookies.
Uses an in-memory SQLite DB (DB_PATH=:memory: via temp file).
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Point at a temp DB before importing anything that touches the DB
@pytest.fixture(autouse=True, scope="module")
def tmp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    os.environ["DB_PATH"] = db_path
    os.environ.setdefault("SECRET_KEY", "test-secret-key-32-bytes-padding!")
    os.environ.setdefault("BASE_URL", "http://localhost:8000")

    # Lazy import AFTER env is set
    import fundscan.db as db_mod
    import fundscan.auth as auth_mod
    db_mod.DB_PATH = Path(db_path)
    auth_mod.BASE_URL = "http://localhost:8000"
    db_mod.init_db(Path(db_path))

    yield db_path

    os.unlink(db_path)


def _auth():
    import fundscan.auth as auth_mod
    return auth_mod


# ---------------------------------------------------------------------------
# User creation
# ---------------------------------------------------------------------------

def test_get_or_create_user_new():
    auth = _auth()
    user = auth.get_or_create_user("new@example.com")
    assert user["email"] == "new@example.com"
    assert user["tier"] == "free"


def test_get_or_create_user_idempotent():
    auth = _auth()
    u1 = auth.get_or_create_user("same@example.com")
    u2 = auth.get_or_create_user("same@example.com")
    assert u1["id"] == u2["id"]


def test_set_user_tier():
    auth = _auth()
    user = auth.get_or_create_user("tier@example.com")
    auth.set_user_tier("tier@example.com", "pro")
    updated = auth.get_user_by_id(user["id"])
    assert updated["tier"] == "pro"


def test_set_user_tier_invalid():
    auth = _auth()
    with pytest.raises(AssertionError):
        auth.set_user_tier("x@example.com", "admin")


# ---------------------------------------------------------------------------
# Magic tokens
# ---------------------------------------------------------------------------

def test_create_and_consume_token():
    auth = _auth()
    auth.get_or_create_user("magic@example.com")
    token = auth.create_magic_token("magic@example.com")
    assert len(token) > 20
    email = auth.consume_magic_token(token)
    assert email == "magic@example.com"


def test_token_single_use():
    auth = _auth()
    auth.get_or_create_user("once@example.com")
    token = auth.create_magic_token("once@example.com")
    assert auth.consume_magic_token(token) == "once@example.com"
    assert auth.consume_magic_token(token) is None  # already used


def test_invalid_token_returns_none():
    auth = _auth()
    assert auth.consume_magic_token("not-a-real-token") is None


def test_expired_token_returns_none():
    """Simulate an expired token by back-dating its expires_at."""
    auth = _auth()
    import fundscan.db as db_mod
    auth.get_or_create_user("expired@example.com")
    token = auth.create_magic_token("expired@example.com")

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with db_mod.get_conn() as conn:
        conn.execute(
            "UPDATE magic_tokens SET expires_at = ? WHERE token = ?",
            (past, token),
        )

    assert auth.consume_magic_token(token) is None


# ---------------------------------------------------------------------------
# Session cookies
# ---------------------------------------------------------------------------

def test_session_cookie_roundtrip():
    auth = _auth()
    user = auth.get_or_create_user("session@example.com")
    cookie = auth.make_session_cookie(user["id"])
    uid = auth.decode_session_cookie(cookie)
    assert uid == user["id"]


def test_tampered_cookie_returns_none():
    auth = _auth()
    assert auth.decode_session_cookie("garbage.cookie.value") is None


def test_wrong_key_returns_none():
    """Cookie signed with a different key should be rejected."""
    from itsdangerous import URLSafeTimedSerializer
    other_signer = URLSafeTimedSerializer("different-key", salt="session")
    bad_cookie = other_signer.dumps({"uid": 999})
    auth = _auth()
    assert auth.decode_session_cookie(bad_cookie) is None
