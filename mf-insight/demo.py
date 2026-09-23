"""
Demo: compute advanced ratios for real funds.

  python demo.py <code> <code>                # live data from mfapi.in (AMFI scheme codes)
  python demo.py --bench <nifty50_index_code> <code>   # benchmark via an index fund
  python demo.py --offline                # synthetic data, no network

Find scheme codes by searching https://api.mfapi.in/mf/search?q=parag
"""
import argparse
import numpy as np
import pandas as pd
from mf_insight.metrics import compute_fund_metrics


DAYS = pd.bdate_range(end="2026-09-18", periods=252 * 6)
MARKET = np.random.default_rng(0).normal(0.12 / 252, 0.14 / 252 ** .5, len(DAYS))


def synthetic(beta, alpha, idio_vol, seed):
    """Fund = alpha + beta * market + idiosyncratic noise (so beta/capture are meaningful)."""
    noise = np.random.default_rng(seed).normal(0, idio_vol / 252 ** .5, len(DAYS))
    r = alpha / 252 + beta * MARKET + noise
    return pd.Series(100 * np.exp(np.cumsum(r)), index=DAYS)


def fmt(m, name):
    pct = lambda x: "  n/a" if x is None or pd.isna(x) else f"{x:6.1%}"
    num = lambda x: "  n/a" if x is None or pd.isna(x) else f"{x:6.2f}"
    print(f"\n{name}  (last {m.lookback_years:g} years, monthly returns)")
    print(f"  CAGR {pct(m.cagr)}   Volatility {pct(m.volatility)}   Max drawdown {pct(m.max_drawdown)}")
    print(f"  Sharpe {num(m.sharpe)}   Sortino {num(m.sortino)}   Beta {num(m.beta)}   Alpha {pct(m.alpha)}")
    if m.up_capture is not None:
        print(f"  Up capture {m.up_capture:5.0f}%   Down capture {m.down_capture:5.0f}%")
    if m.rolling_3y:
        r = m.rolling_3y
        extra = f"   beat benchmark {r['pct_beat_benchmark']:.0%} of windows" if "pct_beat_benchmark" in r else ""
        print(f"  Rolling 3Y CAGR: min {r['min']:.1%}  median {r['median']:.1%}  max {r['max']:.1%}{extra}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="*")
    ap.add_argument("--bench", help="scheme code of an index fund used as benchmark proxy")
    ap.add_argument("--rf", type=float, default=0.065, help="annual risk-free rate")
    ap.add_argument("--years", type=float, default=3)
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args()

    if a.offline:
        bench = pd.Series(100 * np.exp(np.cumsum(MARKET)), index=DAYS)
        cases = [("Defensive flexi-cap (beta 0.8)", 0.8, 0.03, 0.05),
                 ("Aggressive mid-cap (beta 1.3)", 1.3, 0.00, 0.10),
                 ("Balanced / multi-asset (beta 0.5)", 0.5, 0.04, 0.03)]
        for i, (name, b, al, iv) in enumerate(cases):
            fmt(compute_fund_metrics(synthetic(b, al, iv, i + 1), bench, a.rf, a.years), name)
        return

    from mf_insight.nav_source import MfapiHistory
    src = MfapiHistory()
    bench = src.fetch(a.bench)[0] if a.bench else None
    for code in a.codes:
        nav, meta = src.fetch(code)
        fmt(compute_fund_metrics(nav, bench, a.rf, a.years), meta.get("scheme_name", code))


if __name__ == "__main__":
    main()
