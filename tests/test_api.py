import os
os.environ["MF_OFFLINE"] = "1"
import tempfile
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["MF_DB_PATH"] = _tmp.name

from datetime import date, timedelta
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def auth(email="a@example.com"):
    r = client.post("/api/auth/register", json={"email": email, "password": "secret123"})
    if r.status_code == 409:
        r = client.post("/api/auth/login", json={"email": email, "password": "secret123"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def add_sip(h, code, months=30, amount=5000):
    start = date.today() - timedelta(days=30 * months)
    for i in range(months):
        d = start + timedelta(days=30 * i)
        r = client.post("/api/transactions", headers=h,
                        json={"scheme_code": code, "txn_date": d.isoformat(), "amount": amount})
        assert r.status_code == 200, r.text


def test_requires_auth():
    assert client.get("/api/portfolio").status_code == 401


def test_bad_login():
    auth("b@example.com")
    r = client.post("/api/auth/login", json={"email": "b@example.com", "password": "wrongpass1"})
    assert r.status_code == 401


def test_search():
    h = auth()
    hits = client.get("/api/funds/search", params={"q": "mid cap"}, headers=h).json()
    assert hits and hits[0]["code"] == "900003"


def test_free_plan_flow_and_upgrade():
    h = auth("c@example.com")
    assert client.get("/api/portfolio", headers=h).json() == {"empty": True}
    add_sip(h, "900002")
    add_sip(h, "900003")
    p = client.get("/api/portfolio", headers=h).json()
    assert p["value"] > 0 and p["xirr"] is not None
    assert p["per_fund_metrics_locked"] is True
    assert all(x["metrics"] is None for x in p["holdings"])
    assert any("regular plan" in i["title"] for i in p["insights"])
    assert client.get("/api/funds/900003/metrics", headers=h).status_code == 402

    client.post("/api/billing/dev-activate", headers=h, json={"plan": "pro"})
    p = client.get("/api/portfolio", headers=h).json()
    assert p["per_fund_metrics_locked"] is False
    assert p["holdings"][0]["metrics"]["sortino"] is not None
    m = client.get("/api/funds/900003/metrics", headers=h).json()["metrics"]
    assert m["beta"] > 1.0  # demo mid cap is high beta


def test_csv_import_with_errors():
    h = auth("d@example.com")
    csv_text = "scheme_code,date,amount\n900001,2024-01-15,10000\n999999,2024-01-15,5000\n900005,notadate,100\n"
    r = client.post("/api/transactions/import-csv", headers=h,
                    files={"file": ("t.csv", csv_text, "text/csv")}).json()
    assert r["added"] == 1 and len(r["errors"]) == 2


def test_future_date_rejected():
    h = auth()
    r = client.post("/api/transactions", headers=h,
                    json={"scheme_code": "900001", "txn_date": (date.today() + timedelta(days=5)).isoformat(), "amount": 100})
    assert r.status_code == 400


def test_nav_source_down_is_friendly_503(monkeypatch):
    import requests
    from app import navs

    def down(code, when):
        raise requests.ReadTimeout("mfapi.in hung")

    monkeypatch.setattr(navs, "nav_on", down)
    r = client.post("/api/transactions", headers=auth("down@example.com"),
                    json={"scheme_code": "900001", "txn_date": "2025-01-06", "amount": 5000})
    assert r.status_code == 503
    assert "temporarily unavailable" in r.json()["detail"]
