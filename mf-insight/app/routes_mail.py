"""
Automatic statement import:
  * Gmail connection (read-only, searches only statement emails)
  * Personal forwarding address (no inbox access at all)
Both feed statements.process_automatic, which uses the saved statement password.
"""
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from . import navs, statements
from .auth import current_user
from .db import connect
from .mail.gmail import FakeGmailClient, GmailAuthError, GmailClient, STATEMENT_SENDERS
from .secrets_box import decrypt, encrypt

router = APIRouter(prefix="/api")
INBOUND_DOMAIN = os.environ.get("INBOUND_DOMAIN", "import.localhost")
INBOUND_SECRET = os.environ.get("INBOUND_SECRET", "")
STATE_TTL = timedelta(minutes=10)


def gmail() -> GmailClient:
    return FakeGmailClient() if navs.OFFLINE else GmailClient()


def _settings(user_id: int) -> dict:
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO mail_settings(user_id, import_token) VALUES (?, ?)",
                  (user_id, secrets.token_hex(5)))
        return dict(c.execute("SELECT * FROM mail_settings WHERE user_id=?", (user_id,)).fetchone())


# ------------------------------------------------------------ status & password
@router.get("/mail")
def mail_status(user=Depends(current_user)):
    s = _settings(user["id"])
    with connect() as c:
        recent = [dict(r) for r in c.execute(
            "SELECT channel, subject, status, detail, processed_at FROM mail_imports WHERE user_id=? "
            "ORDER BY processed_at DESC LIMIT 8", (user["id"],))]
    return {
        "password_saved": bool(s["pdf_password_enc"]),
        "gmail": {"available": gmail().configured, "connected": bool(s["gmail_refresh_enc"]),
                  "email": s["gmail_email"], "status": s["gmail_status"], "last_checked": s["gmail_last_checked"]},
        "forwarding": {"address": f"{s['import_token']}@{INBOUND_DOMAIN}",
                       "senders": STATEMENT_SENDERS, "confirmation": s["forward_confirmation"]},
        "recent": recent,
    }


class PasswordIn(BaseModel):
    password: str
    consent: bool


@router.put("/mail/password")
def save_password(body: PasswordIn, user=Depends(current_user)):
    if not body.consent:
        raise HTTPException(400, "Tick the box to allow MF Insight to store your statement password.")
    if not body.password.strip():
        raise HTTPException(400, "Enter the password.")
    _settings(user["id"])
    with connect() as c:
        c.execute("UPDATE mail_settings SET pdf_password_enc=? WHERE user_id=?", (encrypt(body.password.strip()), user["id"]))
        # retry statements that were waiting for a password
        c.execute("DELETE FROM mail_imports WHERE user_id=? AND status='needs_password'", (user["id"],))
    return {"password_saved": True}


@router.delete("/mail/password")
def delete_password(user=Depends(current_user)):
    with connect() as c:
        c.execute("UPDATE mail_settings SET pdf_password_enc=NULL WHERE user_id=?", (user["id"],))
    return {"password_saved": False}


# ------------------------------------------------------------ Gmail
@router.get("/mail/gmail/login")
def gmail_login(user=Depends(current_user)):
    g = gmail()
    if not g.configured:
        raise HTTPException(503, "Gmail isn't set up on this server. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.")
    state = secrets.token_urlsafe(24)
    with connect() as c:
        c.execute("INSERT INTO oauth_states(state, user_id, broker) VALUES (?,?,'gmail')", (state, user["id"]))
    return {"url": g.login_url(state)}


@router.get("/mail/gmail/callback")
def gmail_callback(code: str = "", state: str = "", error: str = ""):
    with connect() as c:
        row = c.execute("SELECT * FROM oauth_states WHERE state=? AND broker='gmail'", (state,)).fetchone()
        c.execute("DELETE FROM oauth_states WHERE state=?", (state,))
    if not row or datetime.now(timezone.utc).replace(tzinfo=None) - datetime.fromisoformat(row["created_at"]) > STATE_TTL:
        return RedirectResponse("/?broker_error=The+Gmail+link+expired.+Try+connecting+again.")
    if error or not code:
        return RedirectResponse("/?broker_error=Gmail+access+wasn%27t+granted.")
    g = gmail()
    try:
        access, refresh = g.exchange_code(code)
        email = g.profile_email(access)
    except GmailAuthError as e:
        return RedirectResponse("/?broker_error=" + str(e).replace(" ", "+"))
    _settings(row["user_id"])
    with connect() as c:
        c.execute("UPDATE mail_settings SET gmail_refresh_enc=?, gmail_email=?, gmail_status='connected' WHERE user_id=?",
                  (encrypt(refresh), email, row["user_id"]))
    check_gmail(row["user_id"])
    return RedirectResponse("/?connected=gmail")


def check_gmail(user_id: int) -> dict:
    s = _settings(user_id)
    if not s["gmail_refresh_enc"]:
        raise HTTPException(404, "Gmail isn't connected.")
    g = gmail()
    with connect() as c:
        done = {r["message_id"].rsplit(":", 1)[0] for r in c.execute(
            "SELECT message_id FROM mail_imports WHERE user_id=? AND status IN ('imported','failed')", (user_id,))}
    try:
        token = g.access_token(decrypt(s["gmail_refresh_enc"]))
        mails = g.find_statements(token, done)
    except GmailAuthError:
        with connect() as c:
            c.execute("UPDATE mail_settings SET gmail_status='expired', gmail_refresh_enc=NULL WHERE user_id=?", (user_id,))
        raise HTTPException(409, "Gmail access was removed. Connect Gmail again to keep importing statements.")
    counts = {"found": len(mails), "imported": 0, "needs_password": 0, "failed": 0}
    for m in mails:
        # a statement email can carry more than one PDF; the message counts once
        results = [statements.process_automatic(user_id, pdf, "gmail", f"{m.message_id}:{i}", m.subject)
                   for i, pdf in enumerate(m.pdfs)]
        for st in results:
            if st in counts:
                counts[st] += 1
    with connect() as c:
        c.execute("UPDATE mail_settings SET gmail_last_checked=CURRENT_TIMESTAMP WHERE user_id=?", (user_id,))
    return counts


@router.post("/mail/gmail/check")
def gmail_check(user=Depends(current_user)):
    return check_gmail(user["id"])


@router.delete("/mail/gmail")
def gmail_disconnect(user=Depends(current_user)):
    s = _settings(user["id"])
    if s["gmail_refresh_enc"]:
        gmail().revoke(decrypt(s["gmail_refresh_enc"]))
    with connect() as c:
        c.execute("UPDATE mail_settings SET gmail_refresh_enc=NULL, gmail_email=NULL, gmail_status=NULL WHERE user_id=?",
                  (user["id"],))
    return {"disconnected": "gmail"}


# ------------------------------------------------------------ Forwarding address (inbound webhook)
CONFIRM_RE = re.compile(r"confirmation code:\s*(\d+)", re.I)


def _sender_ok(sender: str) -> bool:
    domain = sender.rsplit("@", 1)[-1].strip(" >").lower()
    return any(domain == d or domain.endswith("." + d) for d in STATEMENT_SENDERS)


@router.post("/inbound/email")
async def inbound_email(request: Request):
    """
    Webhook for an inbound-email service (Mailgun Routes, SendGrid Inbound Parse,
    Postmark, Cloudflare Email Workers posting multipart). Configure it to POST
    here with header X-Inbound-Secret: $INBOUND_SECRET.
    """
    if not INBOUND_SECRET and not navs.OFFLINE:
        raise HTTPException(503, "Inbound email isn't configured.")
    if INBOUND_SECRET and not secrets.compare_digest(request.headers.get("x-inbound-secret", ""), INBOUND_SECRET):
        raise HTTPException(403, "Bad secret.")
    form = await request.form()
    recipient = str(form.get("recipient") or form.get("to") or "").lower()
    sender = str(form.get("sender") or form.get("from") or "").lower()
    subject = str(form.get("subject") or "")
    text = str(form.get("body-plain") or form.get("text") or "")
    msg_id = str(form.get("Message-Id") or form.get("message-id") or secrets.token_hex(8))

    m = re.search(r"([a-f0-9]{10})@" + re.escape(INBOUND_DOMAIN.lower()), recipient)
    if not m:
        return {"ignored": "unknown recipient"}
    with connect() as c:
        row = c.execute("SELECT user_id FROM mail_settings WHERE import_token=?", (m.group(1),)).fetchone()
    if not row:
        return {"ignored": "unknown recipient"}
    user_id = row["user_id"]

    # Gmail sends a confirmation code to a new forwarding address; show it in the app.
    if "forwarding-noreply@google.com" in sender:
        code = CONFIRM_RE.search(text)
        note = f"Gmail confirmation code: {code.group(1)}" if code else "Gmail sent a forwarding confirmation. Open it in Gmail to approve."
        with connect() as c:
            c.execute("UPDATE mail_settings SET forward_confirmation=? WHERE user_id=?", (note, user_id))
        return {"handled": "forwarding_confirmation"}

    if not _sender_ok(sender):
        return {"ignored": "sender is not a statement provider"}
    pdfs = [await v.read() for v in form.values()
            if hasattr(v, "filename") and (v.filename or "").lower().endswith(".pdf")]
    results = [statements.process_automatic(user_id, pdf, "forward", f"fwd:{msg_id}:{i}", subject)
               for i, pdf in enumerate(pdfs)]
    return {"processed": results}
