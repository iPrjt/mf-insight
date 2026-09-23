"""
Portfolio layer: turns a user's transactions into holdings, returns,
portfolio-level risk ratios and rule-based health checks.

Health checks produce neutral observations ("this fund's Sortino is in the
bottom quartile of its category"), never buy/sell instructions. That keeps
the product on the analytics side of SEBI's investment-advice rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pandas as pd

from . import metrics as M


@dataclass
class Transaction:
    scheme_code: str
    txn_date: date
    amount: float      # +ve = purchase / SIP, -ve = redemption (rupees)
    units: float       # +ve for purchase, -ve for redemption


@dataclass
class Holding:
    scheme_code: str
    name: str
    category: str
    units: float
    invested: float
    current_nav: float
    is_direct: bool

    @property
    def value(self) -> float:
        return self.units * self.current_nav


@dataclass
class Insight:
    severity: str       # "info" | "watch" | "attention"
    scheme_code: Optional[str]
    title: str
    detail: str


@dataclass
class Portfolio:
    transactions: list[Transaction]
    navs: dict[str, pd.Series]                  # scheme_code -> NAV history
    meta: dict[str, dict] = field(default_factory=dict)  # name, category

    # ------------------------------------------------ holdings & returns
    def holdings(self) -> list[Holding]:
        out = []
        df = pd.DataFrame([t.__dict__ for t in self.transactions])
        for code, g in df.groupby("scheme_code"):
            units = g["units"].sum()
            if units <= 1e-6:
                continue
            nav = M.clean_nav(self.navs[code])
            info = self.meta.get(code, {})
            name = info.get("name", code)
            out.append(Holding(code, name, info.get("category", ""), units,
                               g["amount"].sum(), float(nav.iloc[-1]),
                               "direct" in name.lower()))
        return out

    def total_value(self) -> float:
        return sum(h.value for h in self.holdings())

    def weights(self) -> dict[str, float]:
        hs = self.holdings()
        total = sum(h.value for h in hs)
        return {h.scheme_code: h.value / total for h in hs} if total else {}

    def portfolio_xirr(self, as_of: Optional[date] = None) -> float:
        as_of = as_of or date.today()
        flows = [(t.txn_date, -t.amount) for t in self.transactions]
        flows.append((as_of, self.total_value()))
        return M.xirr(flows)

    def scheme_xirr(self, code: str, as_of: Optional[date] = None) -> float:
        as_of = as_of or date.today()
        txns = [t for t in self.transactions if t.scheme_code == code]
        value = next((h.value for h in self.holdings() if h.scheme_code == code), 0.0)
        return M.xirr([(t.txn_date, -t.amount) for t in txns] + [(as_of, value)])

    # ------------------------------------------------ portfolio risk
    def portfolio_nav(self) -> pd.Series:
        """
        Synthetic NAV of the CURRENT allocation held over history (monthly
        rebalanced to today's weights). Answers "how risky is what I hold
        now?", which is what the user cares about for decisions.
        """
        w = self.weights()
        rets = pd.concat({c: M.monthly_returns(self.navs[c]) for c in w}, axis=1).dropna()
        port_r = sum(rets[c] * w[c] for c in w)
        return (1 + port_r).cumprod() * 100

    def portfolio_metrics(self, benchmark_nav=None, rf_annual=0.065, years=3.0):
        return M.compute_fund_metrics(self.portfolio_nav(), benchmark_nav, rf_annual, years)

    # ------------------------------------------------ health checks
    def health_checks(self, fund_metrics: dict[str, M.FundMetrics],
                      category_medians: Optional[dict[str, dict]] = None,
                      overlaps: Optional[dict[tuple[str, str], float]] = None) -> list[Insight]:
        insights: list[Insight] = []
        hs = self.holdings()
        w = self.weights()

        for h in hs:
            if not h.is_direct:
                insights.append(Insight(
                    "watch", h.scheme_code, f"{h.name} is a regular plan",
                    "Regular plans carry distributor commission in the expense ratio. "
                    "The direct plan of the same scheme typically costs less each year."))
            if w.get(h.scheme_code, 0) > 0.40:
                insights.append(Insight(
                    "watch", h.scheme_code, f"{w[h.scheme_code]:.0%} of the portfolio is in one fund",
                    "Heavy concentration means this single fund drives most of the outcome."))

            fm = fund_metrics.get(h.scheme_code)
            med = (category_medians or {}).get(h.category)
            if fm and med:
                if fm.sortino < med.get("sortino", float("-inf")):
                    insights.append(Insight(
                        "attention", h.scheme_code, f"{h.name}: Sortino below category median",
                        f"Sortino {fm.sortino:.2f} vs category median {med['sortino']:.2f} "
                        f"over {fm.lookback_years:g} years: less return per unit of downside risk than typical peers."))
                if fm.max_drawdown < med.get("max_drawdown", float("-inf")):
                    insights.append(Insight(
                        "info", h.scheme_code, f"{h.name}: deeper drawdown than peers",
                        f"Worst fall {fm.max_drawdown:.1%} vs category median {med['max_drawdown']:.1%}."))

        for (a, b), ov in (overlaps or {}).items():
            if ov >= 0.40:
                insights.append(Insight(
                    "watch", None, f"High overlap between {self.meta.get(a, {}).get('name', a)} "
                                   f"and {self.meta.get(b, {}).get('name', b)}",
                    f"{ov:.0%} of their portfolios are the same stocks, so they add less diversification than two funds suggest."))

        cats = pd.Series({h.category: 0.0 for h in hs})
        for h in hs:
            cats[h.category] += w[h.scheme_code]
        dup = [c for c in cats.index if sum(1 for h in hs if h.category == c) >= 3]
        for c in dup:
            insights.append(Insight("info", None, f"3+ funds in the same category",
                                    f"{c}: several funds with the same mandate often hold similar stocks."))
        order = {"attention": 0, "watch": 1, "info": 2}
        return sorted(insights, key=lambda i: order[i.severity])


def portfolio_overlap(holdings_a: dict[str, float], holdings_b: dict[str, float]) -> float:
    """
    Overlap between two funds' stock holdings (ISIN -> weight, weights 0-1),
    from AMC monthly portfolio disclosures. Sum of the smaller weight of each
    common stock: 0 = nothing shared, 1 = identical portfolios.
    """
    common = holdings_a.keys() & holdings_b.keys()
    return float(sum(min(holdings_a[s], holdings_b[s]) for s in common))
