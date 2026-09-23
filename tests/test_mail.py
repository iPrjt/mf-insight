import os, tempfile
os.environ["MF_OFFLINE"] = "1"
os.environ["MF_DB_PATH"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name

import base64
from fastapi.testclient import TestClient
from app.main import app
from app import cas, routes_mail
from app.mail.gmail import GmailClient, FakeGmailClient

client = TestClient(app)


def auth(email):
    r = client.post("/api/auth/register", json={"email": email, "password": "secret123"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def connect_gmail(h):
    url = client.get("/api/mail/gmail/login", headers=h).json()["url"]
    r = client.get(url, follow_redirects=False)
    assert r.headers["location"] == "/?connected=gmail"


def funds(h):
    p = client.get("/api/portfolio", headers=h).json()
    return {} if p.get("empty") else {x["scheme_code"]: x for x in p["holdings"]}


def test_gmail_without_password_waits_then_imports_after_saving():
    h = auth("g1@example.com")
    connect_gmail(h)
    st = client.get("/api/mail", headers=h).json()
    assert st["gmail"]["connected"] and st["gmail"]["email"] == "you@gmail.com"
    assert st["recent"][0]["status"] == "needs_password"
    assert funds(h) == {}

    assert client.put("/api/mail/password", headers=h, json={"password": "x", "consent": False}).status_code == 400
    client.put("/api/mail/password", headers=h, json={"password": cas.DEMO_PASSWORD, "consent": True})
    r = client.post("/api/mail/gmail/check", headers=h).json()
    assert r["imported"] == 1
    f = funds(h)
    assert set(f) == {"900002", "900005"} and f["900002"]["xirr"] is not None

    # checking again doesn't re-import
    assert client.post("/api/mail/gmail/check", headers=h).json()["found"] == 0


def test_wrong_password_reported():
    h = auth("g2@example.com")
    client.put("/api/mail/password", headers=h, json={"password": "WRONG", "consent": True})
    connect_gmail(h)
    assert client.get("/api/mail", headers=h).json()["recent"][0]["status"] == "needs_password"


def test_gmail_revoked_marks_expired(monkeypatch):
    h = auth("g3@example.com")
    connect_gmail(h)
    monkeypatch.setattr(FakeGmailClient, "access_token", lambda self, t: (_ for _ in ()).throw(routes_mail.GmailAuthError("x")))
    assert client.post("/api/mail/gmail/check", headers=h).status_code == 409
    g = client.get("/api/mail", headers=h).json()["gmail"]
    assert g["status"] == "expired" and not g["connected"]


def test_forwarding_flow():
    h = auth("f1@example.com")
    client.put("/api/mail/password", headers=h, json={"password": cas.DEMO_PASSWORD, "consent": True})
    addr = client.get("/api/mail", headers=h).json()["forwarding"]["address"]

    # Gmail's confirmation email is captured and shown
    client.post("/api/inbound/email", data={"recipient": addr, "sender": "forwarding-noreply@google.com",
                "subject": "Gmail Forwarding Confirmation", "body-plain": "Confirmation code: 123456789"})
    assert "123456789" in client.get("/api/mail", headers=h).json()["forwarding"]["confirmation"]

    # a random sender is ignored
    r = client.post("/api/inbound/email", data={"recipient": addr, "sender": "spam@evil.com"},
                    files={"attachment-1": ("x.pdf", FakeGmailClient.DEMO_PDF, "application/pdf")}).json()
    assert r["ignored"]

    # a statement from CAMS is imported
    r = client.post("/api/inbound/email", data={"recipient": addr, "sender": "donotreply@camsonline.com",
                    "subject": "CAS", "Message-Id": "<abc@cams>"},
                    files={"attachment-1": ("cas.pdf", FakeGmailClient.DEMO_PDF, "application/pdf")}).json()
    assert r["processed"] == ["imported"]
    assert set(funds(h)) == {"900002", "900005"}


def test_unknown_recipient_ignored():
    r = client.post("/api/inbound/email", data={"recipient": "nobody@example.com", "sender": "a@camsonline.com"}).json()
    assert r["ignored"]


def test_real_gmail_client_parsing():
    pdf = b"%PDF-1.4 test"
    enc = base64.urlsafe_b64encode(pdf).decode().rstrip("=")
    payload = {"headers": [{"name": "Subject", "value": "Your CAS"}], "parts": [
        {"filename": "", "body": {"data": "aGk"}},
        {"filename": "", "parts": [{"filename": "CAS_Aug.PDF", "body": {"attachmentId": "att1"}}]}]}
    class Resp:
        def __init__(s, b): s._b, s.status_code = b, 200
        def json(s): return s._b
        def raise_for_status(s): pass
    class Sess:
        def get(s, url, params=None, headers=None, timeout=None):
            if url.endswith("/messages"):
                assert "camsonline.com" in params["q"]
                return Resp({"messages": [{"id": "m1"}, {"id": "seen"}]})
            if url.endswith("/attachments/att1"): return Resp({"data": enc})
            return Resp({"payload": payload})
    g = GmailClient(Sess())
    mails = g.find_statements("tok", {"seen"})
    assert len(mails) == 1 and mails[0].pdfs == [pdf] and mails[0].subject == "Your CAS"
    assert "gmail.readonly" in GmailClient().login_url("s") and "access_type=offline" in GmailClient().login_url("s")


def test_missing_cas_library_is_not_reported_as_bad_pdf(monkeypatch):
    import pytest
    from app import cas, statements

    def no_lib(fileobj, password):
        raise ModuleNotFoundError("No module named 'casparser'")

    monkeypatch.setattr(cas, "parse_pdf", no_lib)
    with pytest.raises(ImportError):
        statements.process_pdf(1, b"%PDF-1.4", "pw")
