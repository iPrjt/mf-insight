"""
Subscription plans and entitlements. Every gated endpoint asks
`require(user, feature)` instead of checking plan names directly, so
changing what each tier includes is a one-line edit here.
"""
from datetime import date

from fastapi import HTTPException

PLANS = {
    "free":    {"price_month": 0,   "per_fund_metrics": False, "ai_questions": 3,   "monthly_report": False, "alerts": False},
    "pro":     {"price_month": 149, "per_fund_metrics": True,  "ai_questions": 50,  "monthly_report": True,  "alerts": True},
    "premium": {"price_month": 399, "per_fund_metrics": True,  "ai_questions": 200, "monthly_report": True,  "alerts": True},
}

UPGRADE_MSG = {
    "per_fund_metrics": "Per-fund ratios are part of Pro. Upgrade to see Sortino, Sharpe, alpha and drawdown for each fund.",
    "monthly_report": "Monthly portfolio reports are part of Pro.",
    "alerts": "Alerts are part of Pro.",
}


def effective_plan(user: dict) -> str:
    """Paid plans fall back to free once plan_valid_until has passed."""
    plan = user.get("plan", "free")
    valid = user.get("plan_valid_until")
    if plan != "free" and valid and date.fromisoformat(valid) < date.today():
        return "free"
    return plan


def entitlements(user: dict) -> dict:
    return PLANS[effective_plan(user)]


def require(user: dict, feature: str) -> None:
    if not entitlements(user).get(feature):
        # 402 Payment Required lets the frontend show an upgrade prompt.
        raise HTTPException(402, UPGRADE_MSG.get(feature, "Upgrade your plan to use this."))
