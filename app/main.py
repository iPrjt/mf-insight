"""
MF Insight API.

Run:  uvicorn app.main:app --reload        (live data)
      MF_OFFLINE=1 uvicorn app.main:app    (demo funds, no network)
Open: http://127.0.0.1:8000
"""
import csv
import io
import math
from dataclasses import asdict, is_dataclass
from datetime import date, timedelta
from pathlib import Path

import requests
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from mf_insight import metrics as M
from mf_insight.portfolio import Portfolio, Transaction

from . import navs, plans
from .auth import create_session, current_user, hash_password, verify_password
from .db import connect, init_db
from .routes_brokers import router as broker_router
from .routes_mail import router as mail_router

app = FastAPI(title="MF Insight")
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.include_router(broker_router)
app.include_router(mail_router)
init_db()


@app.exception_handler(requests.RequestException)
def nav_source_unavailable(request, exc):
    # AMFI / mfapi.in didn't answer even after retries; don't show a bare 500.
    return JSONResponse(status_code=503, content={
        "detail": "Fund prices are temporarily unavailable. Please try again in a minute."})


# ------------------------------------------------------------ helpers
def clean(obj):
    """Make dataclasses / NaN / dates JSON-safe."""
    if is_dataclass(obj):
        obj = asdict(obj)
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, date):
        return obj.isoformat()
    return obj


def load_portfolio(user_id: int) -> Portfolio | None:
    with connect() as c:
        rows = c.execute("SELECT * FROM transactions WHERE user_id = ? ORDER BY txn_date",
                         (user_id,)).fetchall()
    if not rows:
        return None
    txns = [Transaction(r["scheme_code"], date.fromisoformat(r["txn_date"]), r["amount"], r["units"])
            for r in rows]
    codes = {t.scheme_code for t in txns}
    return Portfolio(txns, {c: navs.nav_history(c) for c in codes}, {c: navs.meta(c) for c in codes})


def save_txn(user_id: int, code: str, txn_date: date, amount: float,
             units: float | None, source: str) -> dict:
    if code not in navs.scheme_master():
        raise HTTPException(400, f"Scheme code {code} was not found. Search for the fund and pick it from the list.")
    if txn_date > date.today():
        raise HTTPException(400, "Transaction date can't be in the future.")
    if amount == 0:
        raise HTTPException(400, "Amount can't be zero.")
    if units is None:
        try:
            units = amount / navs.nav_on(code, txn_date)
        except ValueError as e:
            raise HTTPException(400, str(e))
    with connect() as c:
        cur = c.execute(
            "INSERT INTO transactions(user_id, scheme_code, txn_date, amount, units, source) VALUES (?,?,?,?,?,?)",
            (user_id, code, txn_date.isoformat(), amount, units, source))
    return {"id": cur.lastrowid, "scheme_code": code, "txn_date": txn_date.isoformat(),
            "amount": amount, "units": units}


# ------------------------------------------------------------ auth
class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=8, max_length=200)


@app.post("/api/auth/register")
def register(body: Credentials):
    email = body.email.strip().lower()
    with connect() as c:
        if c.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise HTTPException(409, "An account with this email already exists. Sign in instead.")
        cur = c.execute("INSERT INTO users(email, password_hash) VALUES (?, ?)",
                        (email, hash_password(body.password)))
    return {"token": create_session(cur.lastrowid)}


@app.post("/api/auth/login")
def login(body: Credentials):
    with connect() as c:
        row = c.execute("SELECT * FROM users WHERE email = ?", (body.email.strip().lower(),)).fetchone()
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(401, "Email or password is incorrect.")
    return {"token": create_session(row["id"])}


@app.get("/api/me")
def me(user=Depends(current_user)):
    plan = plans.effective_plan(user)
    return {"email": user["email"], "plan": plan, "plan_valid_until": user["plan_valid_until"],
            "entitlements": plans.PLANS[plan], "plans": plans.PLANS}


# ------------------------------------------------------------ billing (stub)
class PlanChange(BaseModel):
    plan: str


@app.post("/api/billing/dev-activate")
def dev_activate(body: PlanChange, user=Depends(current_user)):
    """
    DEVELOPMENT ONLY. Replace with Razorpay/Cashfree: create a subscription,
    and set plan + plan_valid_until ONLY from a verified payment webhook.
    """
    if body.plan not in plans.PLANS:
        raise HTTPException(400, "Unknown plan.")
    valid = None if body.plan == "free" else (date.today() + timedelta(days=30)).isoformat()
    with connect() as c:
        c.execute("UPDATE users SET plan = ?, plan_valid_until = ? WHERE id = ?",
                  (body.plan, valid, user["id"]))
    return {"plan": body.plan, "plan_valid_until": valid}


# ------------------------------------------------------------ funds
@app.get("/api/funds/search")
def fund_search(q: str, user=Depends(current_user)):
    if len(q.strip()) < 2:
        return []
    return navs.search(q)


@app.get("/api/funds/{code}/metrics")
def fund_metrics(code: str, years: float = 3.0, user=Depends(current_user)):
    plans.require(user, "per_fund_metrics")
    try:
        nav = navs.nav_history(code)
    except KeyError:
        raise HTTPException(404, "No NAV history found for this fund.")
    m = M.compute_fund_metrics(nav, navs.benchmark(), years=years)
    return {"fund": navs.meta(code), "metrics": clean(m)}


# ------------------------------------------------------------ transactions
class TxnIn(BaseModel):
    scheme_code: str
    txn_date: date
    amount: float = Field(description="+ for purchase/SIP, - for redemption (rupees)")
    units: float | None = Field(default=None, description="Leave empty to derive from that day's NAV")


@app.post("/api/transactions")
def add_txn(body: TxnIn, user=Depends(current_user)):
    return save_txn(user["id"], body.scheme_code, body.txn_date, body.amount, body.units, "manual")


@app.get("/api/transactions")
def list_txns(user=Depends(current_user)):
    with connect() as c:
        rows = c.execute("SELECT id, scheme_code, txn_date, amount, units, source FROM transactions "
                         "WHERE user_id = ? ORDER BY txn_date DESC", (user["id"],)).fetchall()
    return [dict(r) | {"name": navs.meta(r["scheme_code"])["name"]} for r in rows]


@app.delete("/api/transactions/{txn_id}")
def delete_txn(txn_id: int, user=Depends(current_user)):
    with connect() as c:
        cur = c.execute("DELETE FROM transactions WHERE id = ? AND user_id = ?", (txn_id, user["id"]))
    if cur.rowcount == 0:
        raise HTTPException(404, "Transaction not found.")
    return {"deleted": txn_id}


@app.post("/api/transactions/import-csv")
async def import_csv(file: UploadFile = File(...), user=Depends(current_user)):
    """CSV columns: scheme_code,date(YYYY-MM-DD),amount[,units]. Interim until the CAS parser lands."""
    text = (await file.read()).decode("utf-8-sig")
    added, errors = 0, []
    for i, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        try:
            units = float(row["units"]) if row.get("units") else None
            save_txn(user["id"], row["scheme_code"].strip(), date.fromisoformat(row["date"].strip()),
                     float(row["amount"]), units, "csv")
            added += 1
        except HTTPException as e:
            errors.append(f"Line {i}: {e.detail}")
        except (KeyError, ValueError) as e:
            errors.append(f"Line {i}: couldn't read this row ({e}).")
    return {"added": added, "errors": errors}


# ------------------------------------------------------------ portfolio
@app.get("/api/portfolio")
def portfolio(user=Depends(current_user)):
    p = load_portfolio(user["id"])
    if p is None:
        return {"empty": True}
    ent = plans.entitlements(user)
    today = date.today()
    with connect() as c:
        src = c.execute("SELECT scheme_code, SUM(CASE WHEN source LIKE '%opening' THEN 1 ELSE 0 END) AS est "
                        "FROM transactions WHERE user_id=? GROUP BY scheme_code", (user["id"],)).fetchall()
    # Funds added from a broker snapshot have no purchase dates yet, so XIRR isn't meaningful.
    estimated = {r["scheme_code"] for r in src if r["est"]}
    real_txns = [t for t in p.transactions if t.scheme_code not in estimated]
    holdings = p.holdings()
    # Funds fully sold still count for lifetime returns, but not for "your ₹X portfolio".
    held = {h.scheme_code for h in holdings}
    current_txns = [t for t in real_txns if t.scheme_code in held]
    exited_txns = [t for t in real_txns if t.scheme_code not in held]
    weights = p.weights()
    total = sum(h.value for h in holdings)
    invested = sum(h.invested for h in holdings)

    fund_metrics = {}
    if ent["per_fund_metrics"]:
        bench = navs.benchmark()
        for h in holdings:
            try:
                fund_metrics[h.scheme_code] = M.compute_fund_metrics(p.navs[h.scheme_code], bench)
            except Exception:
                pass  # too little history for this fund

    try:
        port_m = clean(p.portfolio_metrics(navs.benchmark()))
    except Exception:
        port_m = None

    return clean({
        "empty": False,
        "value": total,
        "invested": invested,
        "gain": total - invested,
        "xirr": (M.xirr([(t.txn_date, -t.amount) for t in current_txns] +
                        [(today, sum(h.value for h in holdings if h.scheme_code not in estimated))])
                 if current_txns else None),
        "xirr_partial": bool(estimated) and bool(current_txns),
        "exited_funds": len({t.scheme_code for t in exited_txns}),
        "exited_gain": -sum(t.amount for t in exited_txns),
        "lifetime_xirr": (M.xirr([(t.txn_date, -t.amount) for t in real_txns] +
                                 [(today, sum(h.value for h in holdings if h.scheme_code not in estimated))])
                          if exited_txns else None),
        "lifetime_since": min(t.txn_date for t in real_txns).year if real_txns else None,
        "portfolio_metrics": port_m,
        "per_fund_metrics_locked": not ent["per_fund_metrics"],
        "holdings": [{
            "scheme_code": h.scheme_code, "name": h.name, "category": h.category,
            "units": h.units, "invested": h.invested, "value": h.value,
            "weight": weights[h.scheme_code],
            "xirr": None if h.scheme_code in estimated else p.scheme_xirr(h.scheme_code, today),
            "needs_history": h.scheme_code in estimated,
            "is_direct": h.is_direct,
            "metrics": fund_metrics.get(h.scheme_code),
        } for h in sorted(holdings, key=lambda x: -x.value)],
        "insights": p.health_checks(fund_metrics),
    })


# ------------------------------------------------------------ frontend
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
