"""
Reconciles holdings reported by a broker with the user's transactions.

Brokers give current units, not full history, so:
  * First sync of a fund with no history -> one "opening balance" record
    (units x average price), dated today. Value and gain are exact; XIRR is
    not meaningful until a statement (CAS) import supplies real dates.
  * Later syncs -> the unit difference is recorded as a purchase/redemption
    on the sync date at that day's NAV. Syncing often keeps this accurate.
"""
from dataclasses import dataclass, field
from datetime import date

from . import navs
from .brokers.zerodha import BrokerHolding
from .db import connect


@dataclass
class SyncResult:
    matched: int = 0
    opened: int = 0
    adjusted: int = 0
    unmatched: list[str] = field(default_factory=list)


def reconcile(user_id: int, broker: str, holdings: list[BrokerHolding], today: date | None = None) -> SyncResult:
    today = today or date.today()
    res = SyncResult()
    with connect() as c:
        for h in holdings:
            code = navs.code_for_isin(h.isin)
            if not code:
                res.unmatched.append(f"{h.fund_name or h.isin} ({h.isin})")
                continue
            res.matched += 1
            row = c.execute("SELECT COALESCE(SUM(units),0) AS u, COUNT(*) AS n FROM transactions "
                            "WHERE user_id=? AND scheme_code=?", (user_id, code)).fetchone()
            if row["n"] == 0:
                c.execute("INSERT INTO transactions(user_id,scheme_code,txn_date,amount,units,source) "
                          "VALUES (?,?,?,?,?,?)",
                          (user_id, code, today.isoformat(), h.units * h.average_price, h.units, f"{broker}_opening"))
                res.opened += 1
                continue
            diff = h.units - row["u"]
            if abs(diff) > 0.001:
                nav = navs.nav_on(code, today)
                c.execute("INSERT INTO transactions(user_id,scheme_code,txn_date,amount,units,source) "
                          "VALUES (?,?,?,?,?,?)",
                          (user_id, code, today.isoformat(), diff * nav, diff, f"{broker}_sync"))
                res.adjusted += 1
        c.execute("UPDATE broker_connections SET last_synced_at=CURRENT_TIMESTAMP WHERE user_id=? AND broker=?",
                  (user_id, broker))
    return res
