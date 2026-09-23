from datetime import date
import numpy as np
import pandas as pd
import pytest
from mf_insight import metrics as M
from mf_insight.portfolio import Portfolio, Transaction, portfolio_overlap


def synthetic_nav(mu=0.12, vol=0.15, years=5, seed=1):
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(end="2026-09-18", periods=int(252 * years))
    r = rng.normal(mu / 252, vol / np.sqrt(252), len(days))
    return pd.Series(100 * np.exp(np.cumsum(r)), index=days)


def test_xirr_simple_doubling():
    # invest 100, get 200 exactly 1 year later -> 100%
    assert M.xirr([(date(2025, 1, 1), -100), (date(2026, 1, 1), 200)]) == pytest.approx(1.0, abs=1e-3)


def test_xirr_sip_known():
    flows = [(date(2025, m, 1), -1000) for m in range(1, 13)] + [(date(2026, 1, 1), 13000)]
    r = M.xirr(flows)
    assert 0.14 < r < 0.18  # ~15.9% for this SIP


def test_max_drawdown():
    nav = pd.Series([100, 120, 90, 60, 130], index=pd.date_range("2024-01-01", periods=5, freq="ME"))
    dd = M.max_drawdown(nav)
    assert dd.max_drawdown == pytest.approx(-0.5)
    assert dd.recovery_date is not None


def test_sortino_manual():
    r = pd.Series([0.02, -0.01, 0.03, -0.02, 0.01, 0.015])
    rf = 0.0
    expected = r.mean() * 12 / (np.sqrt(np.mean(np.minimum(r, 0) ** 2)) * np.sqrt(12))
    assert M.sortino_ratio(r, rf) == pytest.approx(expected)


def test_beta_of_benchmark_is_one():
    nav = synthetic_nav()
    r = M.monthly_returns(nav)
    beta, alpha = M.beta_alpha(r, r, 0.065)
    assert beta == pytest.approx(1.0)
    assert alpha == pytest.approx(0.0, abs=1e-9)


def test_compute_fund_metrics_runs():
    fund, bench = synthetic_nav(0.15, 0.16, seed=2), synthetic_nav(0.12, 0.14, seed=3)
    m = M.compute_fund_metrics(fund, bench)
    assert m.volatility > 0 and m.max_drawdown <= 0
    assert m.rolling_3y and 0 <= m.rolling_3y["pct_beat_benchmark"] <= 1


def test_overlap():
    a = {"INE1": 0.5, "INE2": 0.3, "INE3": 0.2}
    b = {"INE1": 0.4, "INE3": 0.4, "INE9": 0.2}
    assert portfolio_overlap(a, b) == pytest.approx(0.6)


def test_portfolio_end_to_end():
    navs = {"A": synthetic_nav(seed=4), "B": synthetic_nav(seed=5)}
    txns = []
    for code in navs:
        nav = navs[code]
        for d in pd.date_range("2023-01-02", "2026-08-01", freq="MS"):
            px = float(nav.asof(d))
            txns.append(Transaction(code, d.date(), 5000, 5000 / px))
    p = Portfolio(txns, navs, {"A": {"name": "Fund A - Regular Growth", "category": "Mid Cap"},
                               "B": {"name": "Fund B - Direct Growth", "category": "Flexi Cap"}})
    assert abs(sum(p.weights().values()) - 1) < 1e-9
    assert not np.isnan(p.portfolio_xirr(date(2026, 9, 18)))
    insights = p.health_checks({})
    assert any("regular plan" in i.title for i in insights)
