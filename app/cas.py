"""
CAS (Consolidated Account Statement) import via the open-source `casparser`
library. Works for funds bought on ANY platform, including Groww, whose
official API does not expose mutual fund holdings.

Download a *detailed* CAS from CAMS, KFintech or MF Central. The PDF
password is the one chosen when requesting it (PAN for NSDL/CDSL). Manual uploads use it once; automatic imports
use the encrypted copy the user chose to save (see statements.py).
"""
from datetime import date

UNIT_TXN_TYPES = {"PURCHASE", "PURCHASE_SIP", "REDEMPTION", "SWITCH_IN", "SWITCH_IN_MERGER",
                  "SWITCH_OUT", "SWITCH_OUT_MERGER", "DIVIDEND_REINVEST", "GIFT_IN", "GIFT_OUT", "REVERSAL"}


DEMO_PASSWORD = "ABCDE1234F"


def _demo_cas() -> dict:
    """Statement used in MF_OFFLINE mode: 2 years of SIPs in two demo funds."""
    from datetime import timedelta
    from . import navs
    start = date.today() - timedelta(days=730)
    schemes = []
    for code, amt in (("900002", 3000), ("900005", 2000)):
        txns = []
        for i in range(24):
            d = start + timedelta(days=30 * i)
            nav = navs.nav_on(code, d)
            txns.append({"date": d, "type": "PURCHASE_SIP", "amount": amt, "units": round(amt / nav, 3), "nav": nav})
        schemes.append({"scheme": navs.meta(code)["name"], "amfi": code, "transactions": txns})
    return {"folios": [{"schemes": schemes}]}


def parse_pdf(fileobj, password: str) -> dict:
    from . import navs
    if navs.OFFLINE:
        head = fileobj.read(16); fileobj.seek(0)
        if head.startswith(b"%DEMO-CAS%"):
            if password != DEMO_PASSWORD:
                raise ValueError("Incorrect password")
            return _demo_cas()
    import casparser
    data = casparser.read_cas_pdf(fileobj, password)
    return data.model_dump() if hasattr(data, "model_dump") else data


def extract_transactions(cas: dict) -> tuple[list[dict], list[str]]:
    """Flatten folios -> schemes -> transactions into our transaction rows."""
    rows, skipped = [], []
    for folio in cas.get("folios", []):
        for s in folio.get("schemes", []):
            code = s.get("amfi")
            if not code:
                skipped.append(f"{s.get('scheme')}: no AMFI code in statement")
                continue
            for t in s.get("transactions", []):
                ttype = str(t.get("type", "")).split(".")[-1].upper()
                if ttype not in UNIT_TXN_TYPES or not t.get("units"):
                    continue  # stamp duty, STT, dividend payouts: no unit change
                d = t["date"] if isinstance(t["date"], date) else date.fromisoformat(str(t["date"]))
                units = float(t["units"])
                amount = float(t["amount"]) if t.get("amount") is not None else units * float(t.get("nav") or 0)
                rows.append({"scheme_code": str(code), "txn_date": d, "units": units,
                             "amount": abs(amount) if units > 0 else -abs(amount)})
    return rows, skipped
