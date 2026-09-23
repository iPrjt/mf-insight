from mf_insight.nav_source import parse_amfi_navall

# Trimmed from the live NAVAll.txt (Sep 2026): 8 columns, Plan/Option blank on some rows.
NAVALL = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - Flexi Cap Fund)

PPFAS Mutual Fund

122639;INF879O01027;-;Parag Parikh Flexi Cap Fund - Direct Plan - Growth;;;89.8569;23-Sep-2026
135762;INF846K01WO1;-;Axis Children's Fund;Direct Plan;Growth Option;30.0236;23-Sep-2026
120197;INF109K01Q49;-;ICICI Prudential Liquid Fund -;Direct Plan;Growth;421.1551;23-Sep-2026
100001;INF000000001;-;Wound Up Fund;;;N.A.;02-Jul-2018
"""


def test_parse_amfi_eight_columns():
    schemes = {s.code: s for s in parse_amfi_navall(NAVALL)}
    assert set(schemes) == {"122639", "135762", "120197"}  # N.A. NAV skipped
    assert schemes["120197"].name == "ICICI Prudential Liquid Fund - Direct Plan - Growth"
    ppfas = schemes["122639"]
    assert ppfas.name == "Parag Parikh Flexi Cap Fund - Direct Plan - Growth"
    assert ppfas.nav == 89.8569 and ppfas.nav_date == "23-Sep-2026"
    assert ppfas.amc == "PPFAS Mutual Fund"
    assert ppfas.category == "Open Ended Schemes(Equity Scheme - Flexi Cap Fund)"
    assert ppfas.isin_growth == "INF879O01027"
    assert schemes["135762"].name == "Axis Children's Fund - Direct Plan - Growth Option"


def test_parse_amfi_legacy_six_columns():
    text = "Open Ended Schemes(Equity Scheme - Mid Cap Fund)\nSome AMC\n" \
           "118989;INF179K01XQ0;-;HDFC Mid Cap Fund - Direct Plan - Growth;210.5;23-Sep-2026\n"
    (s,) = parse_amfi_navall(text)
    assert s.name == "HDFC Mid Cap Fund - Direct Plan - Growth" and s.nav == 210.5


class _Resp:
    def __init__(self, body):
        self.content = body.encode("utf-8")

    def raise_for_status(self):
        pass


def test_cached_get_retries_then_uses_stale_copy(tmp_path, monkeypatch):
    import os
    import requests
    from mf_insight import nav_source
    monkeypatch.setattr(nav_source, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(nav_source.time, "sleep", lambda s: None)
    calls = []

    def flaky(url, timeout):
        calls.append(url)
        if len(calls) < 3:
            raise requests.ReadTimeout("hung")
        return _Resp("Children’s Fund")

    monkeypatch.setattr(nav_source.requests, "get", flaky)
    assert nav_source._cached_get("u", "x.txt") == "Children’s Fund"  # UTF-8, not ISO-8859-1
    assert len(calls) == 3

    os.utime(tmp_path / "x.txt", (0, 0))  # make the cache stale
    monkeypatch.setattr(nav_source.requests, "get",
                        lambda url, timeout: (_ for _ in ()).throw(requests.ConnectionError()))
    assert nav_source._cached_get("u", "x.txt") == "Children’s Fund"


def test_amfi_latest_nav_appended_when_mfapi_lags():
    import pandas as pd
    from app.navs import with_latest
    nav = pd.Series([89.1, 89.8569], index=pd.to_datetime(["2026-09-17", "2026-09-18"]), name="nav")
    fresh = with_latest(nav, {"nav": 90.25, "nav_date": "23-Sep-2026"})
    assert fresh.index[-1] == pd.Timestamp("2026-09-23") and fresh.iloc[-1] == 90.25 and len(fresh) == 3
    assert with_latest(nav, {"nav": 1.0, "nav_date": "18-Sep-2026"}) is nav   # not newer
    assert with_latest(nav, {}) is nav                                          # demo / unknown fund
