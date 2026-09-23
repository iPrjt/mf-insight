"""
One pipeline for every way a statement arrives: manual upload, Gmail,
or the forwarding address.

Imports are ADDITIVE and de-duplicated: a monthly statement that only
covers one month adds that month's transactions without touching older
history, and importing the same statement twice changes nothing.
Broker snapshot estimates (Zerodha opening/sync rows) for a fund are
removed once real statement history for that fund arrives.
"""
import io

from fastapi import HTTPException

from . import cas, navs
from .db import connect
from .secrets_box import decrypt


class StatementError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message   # code: password | unreadable | empty


def import_rows(user_id: int, rows: list[dict]) -> dict:
    codes = {r["scheme_code"] for r in rows}
    unknown = sorted(c for c in codes if c not in navs.scheme_master())
    rows = [r for r in rows if r["scheme_code"] not in unknown]
    added = 0
    with connect() as c:
        for code in codes - set(unknown):
            c.execute("DELETE FROM transactions WHERE user_id=? AND scheme_code=? "
                      "AND (source LIKE '%opening' OR source LIKE '%sync')", (user_id, code))
        for r in rows:
            dup = c.execute("SELECT 1 FROM transactions WHERE user_id=? AND scheme_code=? AND txn_date=? "
                            "AND source='cas' AND ABS(units-?) < 0.0005",
                            (user_id, r["scheme_code"], r["txn_date"].isoformat(), r["units"])).fetchone()
            if dup:
                continue
            c.execute("INSERT INTO transactions(user_id,scheme_code,txn_date,amount,units,source) "
                      "VALUES (?,?,?,?,?,'cas')",
                      (user_id, r["scheme_code"], r["txn_date"].isoformat(), r["amount"], r["units"]))
            added += 1
    return {"funds": len(codes) - len(unknown), "transactions": added,
            "already_imported": len(rows) - added,
            "skipped": [f"Scheme code {u} isn't in the AMFI list" for u in unknown]}


def process_pdf(user_id: int, pdf: bytes, password: str) -> dict:
    try:
        data = cas.parse_pdf(io.BytesIO(pdf), password)
    except Exception as e:
        msg = str(e).lower()
        if "password" in msg or "decrypt" in msg:
            raise StatementError("password", "The PDF password is incorrect. It's usually your PAN in capital letters.")
        raise StatementError("unreadable", "This PDF couldn't be read as a CAS.")
    rows, skipped = cas.extract_transactions(data)
    if not rows:
        raise StatementError("empty", "No mutual fund transactions were found in this statement.")
    result = import_rows(user_id, rows)
    result["skipped"] += skipped
    return result


def saved_password(user_id: int) -> str | None:
    with connect() as c:
        row = c.execute("SELECT pdf_password_enc FROM mail_settings WHERE user_id=?", (user_id,)).fetchone()
    return decrypt(row["pdf_password_enc"]) if row and row["pdf_password_enc"] else None


def process_automatic(user_id: int, pdf: bytes, channel: str, message_id: str, subject: str) -> str:
    """Import a statement that arrived by Gmail or forwarding. Records the outcome; returns status."""
    with connect() as c:
        if c.execute("SELECT 1 FROM mail_imports WHERE user_id=? AND message_id=? AND status='imported'",
                     (user_id, message_id)).fetchone():
            return "duplicate"
    pwd = saved_password(user_id)
    if not pwd:
        status, detail = "needs_password", "Save your statement password so it can be opened."
    else:
        try:
            r = process_pdf(user_id, pdf, pwd)
            status, detail = "imported", f"{r['transactions']} new transactions across {r['funds']} funds"
        except StatementError as e:
            status, detail = ("needs_password" if e.code == "password" else "failed"), e.message
    with connect() as c:
        c.execute("""INSERT INTO mail_imports(user_id, message_id, channel, subject, status, detail)
                     VALUES (?,?,?,?,?,?)
                     ON CONFLICT(user_id, message_id) DO UPDATE SET status=excluded.status,
                     detail=excluded.detail, processed_at=CURRENT_TIMESTAMP""",
                  (user_id, message_id, channel, subject[:200], status, detail))
    return status


def to_http(e: StatementError) -> HTTPException:
    return HTTPException(400, e.message)
