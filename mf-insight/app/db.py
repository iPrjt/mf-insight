"""SQLite for the MVP. Schema is Postgres-compatible enough to migrate later."""
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("MF_DB_PATH", "mf_insight.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT 'free',
    plan_valid_until TEXT,
    ai_questions_used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    scheme_code TEXT NOT NULL,
    txn_date TEXT NOT NULL,
    amount REAL NOT NULL,
    units REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual'
);
CREATE INDEX IF NOT EXISTS idx_txn_user ON transactions(user_id);
CREATE TABLE IF NOT EXISTS broker_connections (
    user_id INTEGER NOT NULL REFERENCES users(id),
    broker TEXT NOT NULL,
    access_token_enc TEXT,
    status TEXT NOT NULL DEFAULT 'connected',   -- connected | expired
    connected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_synced_at TEXT,
    PRIMARY KEY (user_id, broker)
);
CREATE TABLE IF NOT EXISTS mail_settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    pdf_password_enc TEXT,              -- statement password, encrypted, saved with consent
    import_token TEXT UNIQUE,           -- local part of the personal forwarding address
    forward_confirmation TEXT,          -- Gmail's forwarding-confirmation message, shown to the user
    gmail_refresh_enc TEXT,
    gmail_email TEXT,
    gmail_status TEXT,                  -- connected | expired
    gmail_last_checked TEXT
);
CREATE TABLE IF NOT EXISTS mail_imports (
    user_id INTEGER NOT NULL REFERENCES users(id),
    message_id TEXT NOT NULL,
    channel TEXT NOT NULL,              -- gmail | forward
    subject TEXT,
    status TEXT NOT NULL,               -- imported | needs_password | failed
    detail TEXT,
    processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, message_id)
);
CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    broker TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def init_db() -> None:
    with connect() as c:
        c.executescript(SCHEMA)


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
