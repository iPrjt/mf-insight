"""Broker connections (Zerodha) and statement import (Groww and everything else)."""
import secrets
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse

from . import navs, statements
from .auth import current_user
from .brokers.zerodha import BrokerAuthExpired, FakeKiteClient, KiteClient
from .db import connect
from .secrets_box import decrypt, encrypt
from .sync import reconcile

router = APIRouter(prefix="/api")
STATE_TTL = timedelta(minutes=10)


def kite() -> KiteClient:
    return FakeKiteClient() if navs.OFFLINE else KiteClient()


# ------------------------------------------------------------ status
@router.get("/brokers")
def brokers(user=Depends(current_user)):
    with connect() as c:
        rows = {r["broker"]: dict(r) for r in c.execute(
            "SELECT broker, status, connected_at, last_synced_at FROM broker_connections WHERE user_id=?",
            (user["id"],))}
    z = rows.get("zerodha")
    return {
        "zerodha": {"available": kite().configured, "connected": bool(z),
                    "status": z["status"] if z else None, "last_synced_at": z["last_synced_at"] if z else None},
        "groww": {"available": False, "import_via": "cas",
                  "reason": "Groww's official API covers stocks and F&O, not mutual funds. "
                            "Import your CAS statement to bring in Groww funds."},
    }


# ------------------------------------------------------------ zerodha
@router.get("/brokers/zerodha/login")
def zerodha_login(user=Depends(current_user)):
    client = kite()
    if not client.configured:
        raise HTTPException(503, "Zerodha isn't set up on this server. Add KITE_API_KEY and KITE_API_SECRET.")
    state = secrets.token_urlsafe(24)
    with connect() as c:
        c.execute("INSERT INTO oauth_states(state, user_id, broker) VALUES (?,?,?)", (state, user["id"], "zerodha"))
    return {"url": client.login_url(state)}


@router.get("/brokers/zerodha/callback")
def zerodha_callback(request_token: str = "", status: str = "", state: str = ""):
    """Zerodha redirects the browser here; the user is identified by `state`, not a bearer token."""
    with connect() as c:
        row = c.execute("SELECT * FROM oauth_states WHERE state=? AND broker='zerodha'", (state,)).fetchone()
        c.execute("DELETE FROM oauth_states WHERE state=?", (state,))
    if not row or datetime.now(timezone.utc).replace(tzinfo=None) - datetime.fromisoformat(row["created_at"]) > STATE_TTL:
        return RedirectResponse("/?broker_error=The+Zerodha+login+link+expired.+Try+connecting+again.")
    if status != "success" or not request_token:
        return RedirectResponse("/?broker_error=Zerodha+login+was+cancelled.")
    client = kite()
    try:
        token = client.exchange_token(request_token)
    except BrokerAuthExpired as e:
        return RedirectResponse(f"/?broker_error={str(e).replace(' ', '+')}")
    with connect() as c:
        c.execute("""INSERT INTO broker_connections(user_id, broker, access_token_enc, status)
                     VALUES (?, 'zerodha', ?, 'connected')
                     ON CONFLICT(user_id, broker) DO UPDATE SET access_token_enc=excluded.access_token_enc,
                     status='connected', connected_at=CURRENT_TIMESTAMP""", (row["user_id"], encrypt(token)))
    try:
        _sync_zerodha(row["user_id"])
    except HTTPException:
        pass
    return RedirectResponse("/?connected=zerodha")


def _sync_zerodha(user_id: int) -> dict:
    with connect() as c:
        row = c.execute("SELECT * FROM broker_connections WHERE user_id=? AND broker='zerodha'", (user_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Zerodha isn't connected.")
    if row["status"] != "connected" or not row["access_token_enc"]:
        raise HTTPException(409, "Your Zerodha session has expired. Reconnect Zerodha to sync.")
    try:
        holdings = kite().mf_holdings(decrypt(row["access_token_enc"]))
    except BrokerAuthExpired:
        with connect() as c:
            c.execute("UPDATE broker_connections SET status='expired', access_token_enc=NULL "
                      "WHERE user_id=? AND broker='zerodha'", (user_id,))
        raise HTTPException(409, "Your Zerodha session has expired. Reconnect Zerodha to sync.")
    return asdict(reconcile(user_id, "zerodha", holdings))


@router.post("/brokers/zerodha/sync")
def zerodha_sync(user=Depends(current_user)):
    return _sync_zerodha(user["id"])


@router.delete("/brokers/zerodha")
def zerodha_disconnect(user=Depends(current_user)):
    with connect() as c:
        c.execute("DELETE FROM broker_connections WHERE user_id=? AND broker='zerodha'", (user["id"],))
    return {"disconnected": "zerodha"}


# ------------------------------------------------------------ CAS upload (Groww + all platforms)
@router.post("/import/cas")
async def import_cas(file: UploadFile = File(...), password: str = Form(...), user=Depends(current_user)):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Upload the CAS as a PDF file.")
    try:
        return statements.process_pdf(user["id"], await file.read(), password)  # password not stored
    except statements.StatementError as e:
        raise statements.to_http(e)
