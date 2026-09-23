import os, tempfile
os.environ["MF_OFFLINE"] = "1"
os.environ["MF_DB_PATH"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name

import numpy as np
import pandas as pd
import pytest

from app import jobs, navs, navstore
from app.db import connect, init_db
from mf_insight.nav_source import Scheme

init_db()
DAYS = pd.bdate_range(end="2026-09-18", periods=252 * 4)


def fake_nav(seed):
    r = np.random.default_rng(seed).normal(0.0005, 0.01, len(DAYS))
    return pd.Series(10 * np.exp(np.cumsum(r)), index=DAYS, name="nav")


@pytest.fixture
def live(monkeypatch):
    """Run the live (store-backed) code path with mfapi.in and AMFI faked."""
    calls = []

    class FakeMfapi:
        def fetch(self, code, max_age_hours=12):
            calls.append(code)
            return fake_nav(int(code)), {}

    import mf_insight.nav_source as src
    monkeypatch.setattr(navs, "OFFLINE", False)
    monkeypatch.setattr(navs, "BENCHMARK_CODE", "120716")
    monkeypatch.setattr(navs, "scheme_master", lambda: {})
    monkeypatch.setattr(src, "MfapiHistory", FakeMfapi)
    navs._loaded.clear()
    return calls


def test_first_use_backfills_once_then_reads_from_store(live):
    a = navs.nav_history("122639")
    b = navs.nav_history("122639")
    assert live == ["122639"]                    # mfapi.in called only once
    assert len(a) == len(DAYS) and b is a        # second read served from memory
    assert navstore.last_date("122639") == "2026-09-18"


def test_daily_amfi_rows_extend_history_and_invalidate(live):
    navs.nav_history("118989")
    navstore.save_daily([Scheme("118989", "x", "", "", 99.5, "23-Sep-2026"),
                         Scheme("100001", "old", "", "", 0.0, "02-Jul-2018")])   # zero NAV skipped
    s = navs.nav_history("118989")
    assert s.index[-1] == pd.Timestamp("2026-09-23") and s.iloc[-1] == 99.5
    assert navstore.last_date("100001") is None


def test_metrics_cached_until_new_nav(live, monkeypatch):
    first = navs.fund_metrics("125497")
    computed = []
    real = navstore.M.compute_fund_metrics
    monkeypatch.setattr(navstore.M, "compute_fund_metrics", lambda *a, **k: computed.append(1) or real(*a, **k))
    again = navs.fund_metrics("125497")
    assert computed == [] and again.sortino == pytest.approx(first.sortino)
    assert again.drawdown_trough == first.drawdown_trough          # dates survive the JSON round trip
    navstore.save_daily([Scheme("125497", "x", "", "", 500.0, "23-Sep-2026")])
    navs.fund_metrics("125497")
    assert computed == [1]


def test_update_navs_job(live, monkeypatch):
    import mf_insight.nav_source as src
    with connect() as c:
        c.execute("INSERT INTO users(email, password_hash) VALUES ('job@example.com', 'x')")
        uid = c.execute("SELECT id FROM users WHERE email='job@example.com'").fetchone()[0]
        c.execute("INSERT INTO transactions(user_id, scheme_code, txn_date, amount, units) "
                  "VALUES (?, '145206', '2024-01-05', 1000, 10)", (uid,))

    class FakeAmfi:
        def fetch(self, max_age_hours=12):
            return [Scheme("145206", "Tata Small Cap", "", "", 42.0, "23-Sep-2026")]

    monkeypatch.setattr(src, "AmfiLatest", FakeAmfi)
    out = jobs.update_navs()
    assert out["daily_rows"] == 1 and not out["errors"]
    assert "145206" in live and navstore.backfilled_at("120716")   # held fund and benchmark stored
    assert navstore.last_date("145206") == "2026-09-23"    # backfill kept today's AMFI NAV
    assert jobs.update_navs()["backfilled"] == 0            # refreshed weekly, not nightly
