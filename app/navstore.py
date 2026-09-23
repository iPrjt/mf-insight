"""
Our own NAV history, so dashboards read from the database instead of
calling mfapi.in on every page load (it hangs at times and lags AMFI).

nav_history is filled two ways:
* backfill - a scheme's full history from mfapi.in, the first time anyone
             holds it, then again weekly to fill mfapi's lag and pick up
             corrections (app.jobs update-navs).
* daily    - every scheme's latest NAV from AMFI's NAVAll.txt, nightly.

fund_metrics caches 3-year metrics per (scheme, benchmark) and is
recomputed only when a newer NAV arrives for either of them.
"""
import json
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from mf_insight import metrics as M

from .db import connect

REFRESH_AFTER = timedelta(days=7)
_DATE_FIELDS = ("drawdown_peak", "drawdown_trough", "drawdown_recovered")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def save(code: str, nav: pd.Series) -> int:
    rows = [(code, d.date().isoformat(), float(v)) for d, v in nav.items() if pd.notna(v) and v > 0]
    with connect() as c:
        c.executemany("INSERT OR REPLACE INTO nav_history(scheme_code, nav_date, nav) VALUES (?,?,?)", rows)
    return len(rows)


def save_daily(schemes) -> int:
    """Store one day's NAVs from AMFI (a list of nav_source.Scheme)."""
    rows = []
    for s in schemes:
        try:
            d = datetime.strptime(s.nav_date, "%d-%b-%Y").date()
        except ValueError:
            continue
        if s.nav > 0:
            rows.append((s.code, d.isoformat(), s.nav))
    with connect() as c:
        c.executemany("INSERT OR REPLACE INTO nav_history(scheme_code, nav_date, nav) VALUES (?,?,?)", rows)
    return len(rows)


def mark_backfilled(code: str) -> None:
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO nav_backfills(scheme_code, backfilled_at) VALUES (?, ?)",
                  (code, _utcnow().isoformat(timespec="seconds")))


def backfilled_at(code: str) -> datetime | None:
    with connect() as c:
        row = c.execute("SELECT backfilled_at FROM nav_backfills WHERE scheme_code=?", (code,)).fetchone()
    return datetime.fromisoformat(row[0]) if row else None


def needs_refresh(code: str) -> bool:
    when = backfilled_at(code)
    return when is None or _utcnow() - when > REFRESH_AFTER


def last_date(code: str) -> str | None:
    with connect() as c:
        return c.execute("SELECT MAX(nav_date) FROM nav_history WHERE scheme_code=?", (code,)).fetchone()[0]


def load(code: str) -> pd.Series:
    with connect() as c:
        rows = c.execute("SELECT nav_date, nav FROM nav_history WHERE scheme_code=? ORDER BY nav_date",
                         (code,)).fetchall()
    return pd.Series([r[1] for r in rows], index=pd.to_datetime([r[0] for r in rows]), name="nav", dtype=float)


def tracked_codes(benchmark_code: str = "") -> list[str]:
    """Schemes anyone holds or has held, plus the benchmark."""
    with connect() as c:
        codes = {r[0] for r in c.execute("SELECT DISTINCT scheme_code FROM transactions")}
    return sorted(codes | ({benchmark_code} if benchmark_code else set()))


def cached_metrics(code: str, nav: pd.Series, bench_code: str, bench: pd.Series | None) -> M.FundMetrics:
    as_of = f"{nav.index[-1].date()}|{bench.index[-1].date() if bench is not None and len(bench) else ''}"
    with connect() as c:
        row = c.execute("SELECT as_of, metrics FROM fund_metrics WHERE scheme_code=? AND benchmark_code=?",
                        (code, bench_code)).fetchone()
    if row and row[0] == as_of:
        d = json.loads(row[1])
        for f in _DATE_FIELDS:
            d[f] = date.fromisoformat(d[f]) if d.get(f) else None
        return M.FundMetrics(**d)
    m = M.compute_fund_metrics(nav, bench)
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO fund_metrics(scheme_code, benchmark_code, as_of, metrics) VALUES (?,?,?,?)",
                  (code, bench_code, as_of, json.dumps(asdict(m), default=str)))
    return m
