"""
Encrypts broker access tokens at rest (Fernet / AES-128-CBC + HMAC).
Set TOKEN_KEY in production (generate with:
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
In development a key file is created next to the database.
"""
import os
from pathlib import Path

from cryptography.fernet import Fernet


def _key() -> bytes:
    env = os.environ.get("TOKEN_KEY")
    if env:
        return env.encode()
    path = Path(os.environ.get("MF_DB_PATH", "mf_insight.db")).with_suffix(".key")
    if not path.exists():
        path.write_bytes(Fernet.generate_key())
    return path.read_bytes()


def encrypt(value: str) -> str:
    return Fernet(_key()).encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return Fernet(_key()).decrypt(value.encode()).decode()
