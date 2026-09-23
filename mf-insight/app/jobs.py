"""
Scheduled jobs. Run from cron, e.g. daily at 07:00:
    python -m app.jobs check-mail
"""
import sys

from .db import connect
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


if __name__ == "__main__":
    if sys.argv[1:] == ["check-mail"]:
        check_all_mail()
    else:
        print("usage: python -m app.jobs check-mail")
