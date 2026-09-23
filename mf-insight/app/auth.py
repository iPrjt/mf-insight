"""Minimal email + password auth with opaque session tokens (stdlib only)."""
import hashlib
import hmac
import secrets

from fastapi import Header, HTTPException

from .db import connect

ITERATIONS = 310_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt, digest = stored.split("$", 1)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS)
    return hmac.compare_digest(dk.hex(), digest)


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with connect() as c:
        c.execute("INSERT INTO sessions(token, user_id) VALUES (?, ?)", (token, user_id))
    return token


def current_user(authorization: str = Header(default="")) -> dict:
    """FastAPI dependency: resolves 'Authorization: Bearer <token>' to a user row."""
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Sign in to continue.")
    with connect() as c:
        row = c.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,)).fetchone()
    if not row:
        raise HTTPException(401, "Your session has expired. Sign in again.")
    return dict(row)
