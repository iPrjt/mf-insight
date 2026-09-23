"""
Scheduled jobs. Run from cron, e.g.:
    python -m app.jobs update-navs    daily at 23:30 (AMFI publishes NAVs by ~23:00)
    python -m app.jobs check-mail     daily at 07:00
"""
import sys

from . import navs, navstore
from .db import connect, init_db
from .routes_mail import check_gmail


def check_all_mail() -> None:
    with connect() as c:
        users = [r["user_id"] for r in c.execute(
            "SELECT user_id FROM mail_settings WHERE gmail_refresh_enc IS NOT NULL")]
    for uid in users:
        try:
            print(uid, check_gmail(uid))
        except Exception as e:   # one user's failure must not stop the rest
            print(uid, "error:", getattr(e, "detail", e))


def update_navs() -> dict:
    """Store today's AMFI NAVs, backfill/refresh held funds, precompute their metrics."""
    from mf_insight.nav_source import AmfiLatest
    out = {"daily_rows": navstore.save_daily(AmfiLatest().fetch(max_age_hours=0.5)),
           "backfilled": 0, "metrics": 0, "errors": []}
    tracked = navstore.tracked_codes(navs.BENCHMARK_CODE)
    for code in tracked:
        if navstore.needs_refresh(code):
            try:
                navs.backfill(code, fresh=True)
                out["backfilled"] += 1
            except Exception as e:   # one fund's failure must not stop the rest
                out["errors"].append(f"{code}: backfill {type(e).__name__}")
    for code in tracked:
        try:
            navs.fund_metrics(code)
            out["metrics"] += 1
        except Exception as e:
            out["errors"].append(f"{code}: metrics {type(e).__name__}")
    return out


if __name__ == "__main__":
    init_db()   # the web app does this on start; a cron job may run first
    if sys.argv[1:] == ["check-mail"]:
        check_all_mail()
    elif sys.argv[1:] == ["update-navs"]:
        print(update_navs())
    else:
        print("usage: python -m app.jobs check-mail | update-navs")
