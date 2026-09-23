"""
NAV provider used by the API.

MF_OFFLINE=1  -> built-in demo schemes with simulated NAVs (no network;
                 used by tests and for UI work).
otherwise     -> AMFI scheme master + mfapi.in NAV history (cached).

BENCHMARK_CODE -> scheme code of a Nifty 50 index fund used as the
                  benchmark proxy until licensed index data is added.
"""
import os
from functools import lru_cache

import numpy as np
import pandas as pd

OFFLINE = os.environ.get("MF_OFFLINE") == "1"
BENCHMARK_CODE = os.environ.get("BENCHMARK_CODE", "")

DEMO_SCHEMES = {
    "900001": ("Demo Flexi Cap Fund - Direct Plan - Growth", "Equity Scheme - Flexi Cap Fund", 0.9, 0.03, 0.05),
    "900002": ("Demo Flexi Cap Fund - Regular Plan - Growth", "Equity Scheme - Flexi Cap Fund", 0.9, 0.02, 0.05),
    "900003": ("Demo Mid Cap Opportunities - Direct Plan - Growth", "Equity Scheme - Mid Cap Fund", 1.25, 0.02, 0.09),
    "900004": ("Demo Small Cap Fund - Direct Plan - Growth", "Equity Scheme - Small Cap Fund", 1.4, 0.01, 0.12),
    "900005": ("Demo Multi Asset Allocation - Direct Plan - Growth", "Hybrid Scheme - Multi Asset Allocation", 0.5, 0.04, 0.03),
    "900006": ("Demo Nifty 50 Index Fund - Direct Plan - Growth", "Other Scheme - Index Funds", 1.0, -0.002, 0.004),
}
_DAYS = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=252 * 7)
_MARKET = np.random.default_rng(33).normal(0.12 / 252, 0.14 / 252 ** 0.5, len(_DAYS))


def _demo_nav(code: str) -> pd.Series:
    _, _, beta, alpha, idio = DEMO_SCHEMES[code]
    noise = np.random.default_rng(int(code)).normal(0, idio / 252 ** 0.5, len(_DAYS))
    return pd.Series(10 * np.exp(np.cumsum(alpha / 252 + beta * _MARKET + noise)), index=_DAYS)


@lru_cache(maxsize=1)
def scheme_master() -> dict[str, dict]:
    if OFFLINE:
        return {c: {"code": c, "name": v[0], "category": v[1], "isins": [f"INFDEMO0000{c[-1]}"]}
                for c, v in DEMO_SCHEMES.items()}
    from mf_insight.nav_source import AmfiLatest
    out = {}
    for s in AmfiLatest().fetch():
        cat = s.category.split("(", 1)[-1].rstrip(")") if "(" in s.category else s.category
        isins = [i for i in (s.isin_growth, s.isin_reinvest) if i and i != "-"]
        out[s.code] = {"code": s.code, "name": s.name, "category": cat, "amc": s.amc, "isins": isins}
    return out


@lru_cache(maxsize=1)
def _isin_index() -> dict[str, str]:
    return {isin: code for code, m in scheme_master().items() for isin in m.get("isins", [])}


def code_for_isin(isin: str) -> str | None:
    """Brokers identify funds by ISIN; AMFI NAVs are keyed by scheme code."""
    return _isin_index().get((isin or "").strip().upper())


def search(q: str, limit: int = 20) -> list[dict]:
    words = q.lower().split()
    hits = [s for s in scheme_master().values() if all(w in s["name"].lower() for w in words)]
    return hits[:limit]


def meta(code: str) -> dict:
    return scheme_master().get(code, {"code": code, "name": code, "category": ""})


@lru_cache(maxsize=512)
def nav_history(code: str) -> pd.Series:
    if OFFLINE:
        if code not in DEMO_SCHEMES:
            raise KeyError(code)
        return _demo_nav(code)
    from mf_insight.nav_source import MfapiHistory
    nav, _ = MfapiHistory().fetch(code)
    if nav.empty:
        raise KeyError(code)
    return nav


def benchmark() -> pd.Series | None:
    code = "900006" if OFFLINE else BENCHMARK_CODE
    try:
        return nav_history(code) if code else None
    except Exception:
        return None


def nav_on(code: str, when) -> float:
    """NAV on a date (or the last one before it, for holidays)."""
    val = nav_history(code).asof(pd.Timestamp(when))
    if pd.isna(val):
        raise ValueError("No NAV available on or before that date for this fund.")
    return float(val)
