import os, tempfile
os.environ["MF_OFFLINE"] = "1"
os.environ["MF_DB_PATH"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name

from datetime import date, timedelta
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import cas, routes_brokers, statements
from app.brokers.zerodha import FakeKiteClient, BrokerHolding, KiteClient

client = TestClient(app)


def auth(email):
    r = client.post("/api/auth/register", json={"email": email, "password": "secret123"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def connect_zerodha(h):
    url = client.get("/api/brokers/zerodha/login", headers=h).json()["url"]
    r = client.get(url, follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/?connected=zerodha"


def test_zerodha_connect_creates_opening_balances():
    h = auth("z1@example.com")
    connect_zerodha(h)
    st = client.get("/api/brokers", headers=h).json()
    assert st["zerodha"]["connected"] and st["zerodha"]["last_synced_at"]
    assert st["groww"]["import_via"] == "cas"
    p = client.get("/api/portfolio", headers=h).json()
    assert len(p["holdings"]) == 2
    assert all(x["needs_history"] and x["xirr"] is None for x in p["holdings"])
    assert p["xirr"] is None
    assert p["invested"] == pytest.approx(1200 * 19.5 + 800 * 16.0)


def test_second_sync_records_unit_changes(monkeypatch):
    h = auth("z2@example.com")
    connect_zerodha(h)
    monkeypatch.setattr(FakeKiteClient, "holdings", [
        BrokerHolding("INFDEMO00001", "Flexi", "Z1", 1300.0, 49.0),   # bought 100 units
        BrokerHolding("INFDEMO00005", "Multi", "Z1", 800.0, 16.0)])
    r = client.post("/api/brokers/zerodha/sync", headers=h).json()
    assert r["adjusted"] == 1 and r["opened"] == 0
    units = {x["scheme_code"]: x["units"] for x in client.get("/api/portfolio", headers=h).json()["holdings"]}
    assert units["900001"] == pytest.approx(1300)


def test_unknown_isin_reported(monkeypatch):
    h = auth("z3@example.com")
    monkeypatch.setattr(FakeKiteClient, "holdings", [BrokerHolding("INF000NOPE00", "Mystery", "Z", 5, 10)])
    connect_zerodha(h)
    r = client.post("/api/brokers/zerodha/sync", headers=h).json()
    assert r["unmatched"] and r["matched"] == 0


def test_expired_session_marks_connection(monkeypatch):
    h = auth("z4@example.com")
    connect_zerodha(h)
    monkeypatch.setattr(FakeKiteClient, "mf_holdings",
                        lambda self, t: (_ for _ in ()).throw(routes_brokers.BrokerAuthExpired("x")))
    assert client.post("/api/brokers/zerodha/sync", headers=h).status_code == 409
    assert client.get("/api/brokers", headers=h).json()["zerodha"]["status"] == "expired"


def test_bad_state_rejected():
    r = client.get("/api/brokers/zerodha/callback?request_token=demo&status=success&state=forged", follow_redirects=False)
    assert "broker_error" in r.headers["location"]


def test_cas_import_replaces_broker_estimates():
    h = auth("cas@example.com")
    connect_zerodha(h)
    start = date.today() - timedelta(days=700)
    fake_cas = {"folios": [{"schemes": [{"scheme": "Demo Flexi", "amfi": "900001", "transactions": [
        {"date": start + timedelta(days=30 * i), "type": "PURCHASE_SIP", "amount": 5000, "units": 100, "nav": 50}
        for i in range(12)] + [
        {"date": start, "type": "STAMP_DUTY_TAX", "amount": 0.25, "units": None, "nav": None},
        {"date": start + timedelta(days=400), "type": "REDEMPTION", "amount": -6000, "units": -100, "nav": 60}]}]}]}
    rows, skipped = cas.extract_transactions(fake_cas)
    assert len(rows) == 13 and not skipped
    statements.import_rows(_uid(h), rows)
    again = statements.import_rows(_uid(h), rows)
    assert again["transactions"] == 0 and again["already_imported"] == 13
    p = client.get("/api/portfolio", headers=h).json()
    flexi = next(x for x in p["holdings"] if x["scheme_code"] == "900001")
    assert flexi["units"] == pytest.approx(1100) and not flexi["needs_history"] and flexi["xirr"] is not None
    assert p["xirr_partial"] is True   # multi-asset still only has a broker snapshot


def _uid(h):
    from app.db import connect
    tok = h["Authorization"].split()[1]
    with connect() as c:
        return c.execute("SELECT user_id FROM sessions WHERE token=?", (tok,)).fetchone()[0]


def test_real_kite_client_request_shape():
    calls = {}
    class Resp:
        def __init__(s, code, body): s.status_code, s._b = code, body
        def json(s): return s._b
    class Sess:
        def post(s, url, **kw): calls["post"] = (url, kw); return Resp(200, {"status": "success", "data": {"access_token": "AT"}})
        def get(s, url, **kw):
            calls["get"] = (url, kw)
            return Resp(200, {"status": "success", "data": [
                {"tradingsymbol": "INF879O01027", "fund": "X", "folio": "1", "quantity": 10.5, "average_price": 50.0},
                {"tradingsymbol": "INF000000000", "fund": "Y", "folio": "2", "quantity": 0, "average_price": 1.0}]})
    k = KiteClient("key", "sec", Sess())
    assert "api_key=key" in k.login_url("abc") and "state%3Dabc" in k.login_url("abc")
    assert k.exchange_token("rt") == "AT"
    import hashlib
    assert calls["post"][1]["data"]["checksum"] == hashlib.sha256(b"keyrtsec").hexdigest()
    hs = k.mf_holdings("AT")
    assert calls["get"][1]["headers"]["Authorization"] == "token key:AT"
    assert len(hs) == 1 and hs[0].isin == "INF879O01027"


def test_two_identical_sips_on_same_day_both_kept():
    h = auth("twosip@example.com")
    d = date.today() - timedelta(days=200)
    rows = [{"scheme_code": "900003", "txn_date": d, "units": 42.241, "amount": 500},
            {"scheme_code": "900003", "txn_date": d, "units": 42.241, "amount": 500}]
    first = statements.import_rows(_uid(h), rows)
    again = statements.import_rows(_uid(h), rows)
    assert first["transactions"] == 2
    assert again["transactions"] == 0 and again["already_imported"] == 2


def test_headline_xirr_excludes_fully_sold_funds():
    h = auth("exited@example.com")
    start = date.today() - timedelta(days=900)
    rows = [{"scheme_code": "900004", "txn_date": start, "units": 100, "amount": 1000},
            {"scheme_code": "900004", "txn_date": start + timedelta(days=365), "units": -100, "amount": -3000},
            {"scheme_code": "900005", "txn_date": start + timedelta(days=500), "units": 100, "amount": 1000}]
    statements.import_rows(_uid(h), rows)
    p = client.get("/api/portfolio", headers=h).json()
    assert [x["scheme_code"] for x in p["holdings"]] == ["900005"]
    assert p["xirr"] == pytest.approx(p["holdings"][0]["xirr"], abs=1e-6)
    assert p["exited_funds"] == 1 and p["exited_gain"] == pytest.approx(2000)
    assert p["lifetime_xirr"] > p["xirr"]
