"""
Core analytics engine for mutual fund performance.

All functions take a NAV series (pd.Series indexed by date, values = NAV)
or a series of periodic returns. Numbers are computed by code only, never
by the LLM layer, so they are reproducible and auditable.

Conventions (match what Indian fund-data sites typically use):
  * Risk ratios use MONTHLY returns over a lookback window (default 3 years).
  * Risk-free rate is annual (e.g. 0.065 = 6.5%, roughly the 91-day T-bill);
    it is converted to a monthly rate for the calculation.
  * Sortino's downside deviation uses the risk-free rate as the minimum
    acceptable return (MAR), counting every period in the denominator.
Different sites use slightly different conventions, so values may differ
from Value Research / Morningstar by small amounts. That is expected.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date
from typing import Iterable, Optional

import numpy as np
import pandas as pd

MONTHS_PER_YEAR = 12


# ---------------------------------------------------------------- basics
def clean_nav(nav: pd.Series) -> pd.Series:
    """Sort, drop duplicates/non-positive values, ensure DatetimeIndex."""
    s = nav.copy()
    s.index = pd.to_datetime(s.index)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s = pd.to_numeric(s, errors="coerce").dropna()
    return s[s > 0]


def trailing_window(nav: pd.Series, years: float) -> pd.Series:
    """Last `years` of NAV history (inclusive of the start boundary)."""
    nav = clean_nav(nav)
    start = nav.index[-1] - pd.DateOffset(months=int(round(years * 12)))
    return nav[nav.index >= start]


def monthly_returns(nav: pd.Series) -> pd.Series:
    """Month-end to month-end simple returns."""
    month_end = clean_nav(nav).resample("ME").last().dropna()
    return month_end.pct_change().dropna()


def cagr(nav: pd.Series) -> float:
    nav = clean_nav(nav)
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    if years <= 0:
        return float("nan")
    return (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1


def _monthly_rf(rf_annual: float) -> float:
    return (1 + rf_annual) ** (1 / MONTHS_PER_YEAR) - 1


# ---------------------------------------------------------------- risk
def annualized_volatility(returns: pd.Series) -> float:
    return float(returns.std(ddof=1) * np.sqrt(MONTHS_PER_YEAR))


def downside_deviation(returns: pd.Series, rf_annual: float) -> float:
    """Annualized downside deviation below the monthly risk-free rate."""
    shortfall = np.minimum(returns - _monthly_rf(rf_annual), 0.0)
    return float(np.sqrt(np.mean(shortfall ** 2)) * np.sqrt(MONTHS_PER_YEAR))


def sharpe_ratio(returns: pd.Series, rf_annual: float) -> float:
    excess = returns - _monthly_rf(rf_annual)
    vol = returns.std(ddof=1)
    if vol == 0 or np.isnan(vol):
        return float("nan")
    return float(excess.mean() / vol * np.sqrt(MONTHS_PER_YEAR))


def sortino_ratio(returns: pd.Series, rf_annual: float) -> float:
    excess_annual = (returns - _monthly_rf(rf_annual)).mean() * MONTHS_PER_YEAR
    dd = downside_deviation(returns, rf_annual)
    if dd == 0:
        return float("inf") if excess_annual > 0 else float("nan")
    return float(excess_annual / dd)


def beta_alpha(fund_r: pd.Series, bench_r: pd.Series, rf_annual: float) -> tuple[float, float]:
    """CAPM beta and annualized Jensen's alpha against a benchmark."""
    df = pd.concat([fund_r, bench_r], axis=1, join="inner").dropna()
    if len(df) < 6:
        return float("nan"), float("nan")
    rf_m = _monthly_rf(rf_annual)
    f, b = df.iloc[:, 0] - rf_m, df.iloc[:, 1] - rf_m
    beta = float(np.cov(f, b, ddof=1)[0, 1] / np.var(b, ddof=1))
    alpha = float((f.mean() - beta * b.mean()) * MONTHS_PER_YEAR)
    return beta, alpha


def capture_ratios(fund_r: pd.Series, bench_r: pd.Series) -> tuple[float, float]:
    """Up- and down-market capture (%), geometric, on monthly returns."""
    df = pd.concat([fund_r, bench_r], axis=1, join="inner").dropna()
    df.columns = ["f", "b"]

    def _ratio(mask: pd.Series) -> float:
        sub = df[mask]
        if sub.empty:
            return float("nan")
        n = len(sub)
        f = (1 + sub["f"]).prod() ** (MONTHS_PER_YEAR / n) - 1
        b = (1 + sub["b"]).prod() ** (MONTHS_PER_YEAR / n) - 1
        return float(f / b * 100) if b != 0 else float("nan")

    return _ratio(df["b"] > 0), _ratio(df["b"] < 0)


@dataclass
class Drawdown:
    max_drawdown: float          # negative number, e.g. -0.32 = -32%
    peak_date: Optional[date]
    trough_date: Optional[date]
    recovery_date: Optional[date]  # None if not yet recovered


def max_drawdown(nav: pd.Series) -> Drawdown:
    nav = clean_nav(nav)
    running_peak = nav.cummax()
    dd = nav / running_peak - 1
    trough = dd.idxmin()
    if dd.loc[trough] == 0:
        return Drawdown(0.0, None, None, None)
    peak = nav.loc[:trough].idxmax()
    after = nav.loc[trough:]
    recovered = after[after >= nav.loc[peak]]
    rec = recovered.index[0].date() if not recovered.empty else None
    return Drawdown(float(dd.loc[trough]), peak.date(), trough.date(), rec)


# ---------------------------------------------------------------- rolling
def rolling_cagr(nav: pd.Series, years: int) -> pd.Series:
    """CAGR for every start date with `years` of forward history."""
    daily = clean_nav(nav).resample("D").last().ffill()
    lag = int(round(years * 365.25))
    ratio = daily / daily.shift(lag)
    return (ratio ** (1 / years) - 1).dropna()


def rolling_summary(nav: pd.Series, years: int,
                    bench_nav: Optional[pd.Series] = None) -> dict:
    r = rolling_cagr(nav, years)
    if r.empty:
        return {}
    out = {
        "windows": int(len(r)),
        "min": float(r.min()),
        "median": float(r.median()),
        "max": float(r.max()),
        "pct_negative": float((r < 0).mean()),
    }
    if bench_nav is not None:
        br = rolling_cagr(bench_nav, years)
        both = pd.concat([r, br], axis=1, join="inner").dropna()
        if not both.empty:
            out["pct_beat_benchmark"] = float((both.iloc[:, 0] > both.iloc[:, 1]).mean())
    return out


# ---------------------------------------------------------------- XIRR
def xirr(cashflows: Iterable[tuple[date, float]]) -> float:
    """
    Money-weighted return. Investments are NEGATIVE, redemptions and the
    current value are POSITIVE. Solved by bisection (robust, no scipy).
    """
    flows = sorted((pd.Timestamp(d), float(a)) for d, a in cashflows)
    if not flows or all(a >= 0 for _, a in flows) or all(a <= 0 for _, a in flows):
        return float("nan")
    t0 = flows[0][0]
    times = np.array([(d - t0).days / 365.0 for d, _ in flows])
    amts = np.array([a for _, a in flows])

    def npv(rate: float) -> float:
        return float(np.sum(amts / (1 + rate) ** times))

    lo, hi = -0.9999, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return float("nan")
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-7:
            break
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return mid


# ---------------------------------------------------------------- report
@dataclass
class FundMetrics:
    lookback_years: float
    cagr: float
    volatility: float
    downside_deviation: float
    sharpe: float
    sortino: float
    max_drawdown: float
    drawdown_peak: Optional[date]
    drawdown_trough: Optional[date]
    drawdown_recovered: Optional[date]
    beta: Optional[float] = None
    alpha: Optional[float] = None
    up_capture: Optional[float] = None
    down_capture: Optional[float] = None
    rolling_3y: Optional[dict] = None

    def to_dict(self) -> dict:
        return asdict(self)


def compute_fund_metrics(nav: pd.Series,
                         benchmark_nav: Optional[pd.Series] = None,
                         rf_annual: float = 0.065,
                         years: float = 3.0) -> FundMetrics:
    """One-call summary of every ratio for a single fund."""
    window = trailing_window(nav, years)
    r = monthly_returns(window)
    dd = max_drawdown(window)
    m = FundMetrics(
        lookback_years=years,
        cagr=cagr(window),
        volatility=annualized_volatility(r),
        downside_deviation=downside_deviation(r, rf_annual),
        sharpe=sharpe_ratio(r, rf_annual),
        sortino=sortino_ratio(r, rf_annual),
        max_drawdown=dd.max_drawdown,
        drawdown_peak=dd.peak_date,
        drawdown_trough=dd.trough_date,
        drawdown_recovered=dd.recovery_date,
        rolling_3y=rolling_summary(nav, 3, benchmark_nav) or None,
    )
    if benchmark_nav is not None:
        br = monthly_returns(trailing_window(benchmark_nav, years))
        m.beta, m.alpha = beta_alpha(r, br, rf_annual)
        m.up_capture, m.down_capture = capture_ratios(r, br)
    return m
