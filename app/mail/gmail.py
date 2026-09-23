"""
Gmail client for finding statement emails (read-only).

Scope: gmail.readonly, a Google "restricted" scope. In Testing mode it
works for up to 100 test users you add in Google Cloud Console; a public
launch needs Google's app verification and a yearly security assessment.

Env: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
     GOOGLE_REDIRECT_URL (default http://127.0.0.1:8000/api/mail/gmail/callback)
"""
import base64
import os
import re
from dataclasses import dataclass
from urllib.parse import urlencode

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

# Senders of consolidated account statements. Kept as a list so it can be
# tuned after checking real emails.
STATEMENT_SENDERS = ["camsonline.com", "kfintech.com", "mfcentral.com", "nsdl.co.in", "cdslindia.com", "cdslindia.co.in"]


# The same senders also email transaction receipts ("Redemption Transaction
# confirmation") with PDFs; only statement subjects are imported.
STATEMENT_SUBJECT = re.compile(r"consolidated account statement|\be-?cas\b|\bcas\b", re.I)


def is_statement_subject(subject: str) -> bool:
    return bool(STATEMENT_SUBJECT.search(subject or ""))


def statement_query(newer_than: str = "2y") -> str:
    senders = " OR ".join(f"from:{d}" for d in STATEMENT_SENDERS)
    return (f'has:attachment filename:pdf newer_than:{newer_than} '
            f'({senders} OR subject:"Consolidated Account Statement") '
            f'(subject:"Consolidated Account Statement" OR subject:CAS OR subject:eCAS)')


@dataclass
class StatementMail:
    message_id: str
    subject: str
    pdfs: list[bytes]


class GmailAuthError(Exception):
    """Refresh token revoked or expired; the user must reconnect."""


class GmailClient:
    def __init__(self, session=None):
        self.client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
        self.client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
        self.redirect = os.environ.get("GOOGLE_REDIRECT_URL", "http://127.0.0.1:8000/api/mail/gmail/callback")
        self.http = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def login_url(self, state: str) -> str:
        return AUTH_URL + "?" + urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect, "response_type": "code",
            "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
            "include_granted_scopes": "true"})

    def exchange_code(self, code: str) -> tuple[str, str]:
        """Returns (access_token, refresh_token)."""
        r = self.http.post(TOKEN_URL, timeout=20, data={
            "code": code, "client_id": self.client_id, "client_secret": self.client_secret,
            "redirect_uri": self.redirect, "grant_type": "authorization_code"})
        body = r.json()
        if r.status_code != 200 or "refresh_token" not in body:
            raise GmailAuthError(body.get("error_description", "Google didn't grant access."))
        return body["access_token"], body["refresh_token"]

    def access_token(self, refresh_token: str) -> str:
        r = self.http.post(TOKEN_URL, timeout=20, data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": refresh_token, "grant_type": "refresh_token"})
        body = r.json()
        if r.status_code != 200:
            raise GmailAuthError(body.get("error_description", "Gmail access was revoked."))
        return body["access_token"]

    def revoke(self, refresh_token: str) -> None:
        try:
            self.http.post(REVOKE_URL, params={"token": refresh_token}, timeout=10)
        except Exception:
            pass  # best effort; we delete our copy regardless

    def _get(self, token: str, path: str, **params) -> dict:
        r = self.http.get(f"{API}{path}", params=params, timeout=30,
                          headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 401:
            raise GmailAuthError("Gmail access was revoked.")
        r.raise_for_status()
        return r.json()

    def profile_email(self, token: str) -> str:
        return self._get(token, "/profile").get("emailAddress", "")

    def find_statements(self, token: str, skip_ids: set[str], limit: int = 20) -> list[StatementMail]:
        listing = self._get(token, "/messages", q=statement_query(), maxResults=limit)
        out = []
        for m in listing.get("messages", []):
            if m["id"] in skip_ids:
                continue
            msg = self._get(token, f"/messages/{m['id']}", format="full")
            headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
            if not is_statement_subject(headers.get("subject", "")):
                continue
            pdfs = [self._attachment(token, m["id"], p) for p in _pdf_parts(msg["payload"])]
            if pdfs:
                out.append(StatementMail(m["id"], headers.get("subject", ""), pdfs))
        return out

    def _attachment(self, token: str, msg_id: str, part: dict) -> bytes:
        body = part.get("body", {})
        data = body.get("data") or self._get(token, f"/messages/{msg_id}/attachments/{body['attachmentId']}")["data"]
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _pdf_parts(payload: dict) -> list[dict]:
    found, stack = [], [payload]
    while stack:
        p = stack.pop()
        stack.extend(p.get("parts", []))
        if (p.get("filename") or "").lower().endswith(".pdf") and (p.get("body", {}).get("attachmentId") or p.get("body", {}).get("data")):
            found.append(p)
    return found


class FakeGmailClient(GmailClient):
    """Offline stand-in: one mailbox containing one demo statement."""
    DEMO_PDF = b"%DEMO-CAS%"

    def __init__(self):
        super().__init__()
        self.client_id, self.client_secret = "demo", "demo"

    def login_url(self, state: str) -> str:
        return f"/api/mail/gmail/callback?code=demo&state={state}"

    def exchange_code(self, code: str):
        if code != "demo":
            raise GmailAuthError("Google didn't grant access.")
        return "demo_access", "demo_refresh"

    def access_token(self, refresh_token: str) -> str:
        if refresh_token != "demo_refresh":
            raise GmailAuthError("Gmail access was revoked.")
        return "demo_access"

    def revoke(self, refresh_token: str) -> None:
        pass

    def profile_email(self, token: str) -> str:
        return "you@gmail.com"

    def find_statements(self, token: str, skip_ids: set[str], limit: int = 20):
        mails = [StatementMail("demo-msg-1", "Consolidated Account Statement - demo", [self.DEMO_PDF])]
        return [m for m in mails if m.message_id not in skip_ids]
