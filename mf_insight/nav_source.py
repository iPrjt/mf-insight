"""
NAV data sources.

* AmfiLatest  - AMFI's free daily file of every scheme's latest NAV.
                Use it for the scheme master (code -> name, category) and
                for daily updates.
* MfapiHistory - full NAV history per scheme from api.mfapi.in, a free
                community API built on AMFI data. Good for prototyping;
                for production, build your own history store by saving the
                AMFI daily file every day (see README).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

AMFI_NAV_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
MFAPI_URL = "https://api.mfapi.in/mf/{code}"
CACHE_DIR = Path.home() / ".mf_insight_cache"


@dataclass
class Scheme:
    code: str
    name: str
    amc: str
    category: str
    nav: float
    nav_date: str
    isin_growth: str = ""
    isin_reinvest: str = ""


def _cached_get(url: str, cache_name: str, max_age_hours: float = 12,
                attempts: int = 3, timeout: float = 15) -> str:
    """
    Fetch with a disk cache. mfapi.in sometimes hangs for a minute and then
    answers instantly, so retry a few times, then fall back to a stale copy.
    """
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / cache_name
    if path.exists() and time.time() - path.stat().st_mtime < max_age_hours * 3600:
        return path.read_text(encoding="utf-8")
    for attempt in range(attempts):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            break
        except requests.RequestException as e:
            status = getattr(e.response, "status_code", None) or 0
            if 400 <= status < 500 or attempt == attempts - 1:
                if path.exists():
                    return path.read_text(encoding="utf-8")
                raise
            time.sleep(attempt + 1)
    # AMFI sends text/plain with no charset, so requests would guess ISO-8859-1.
    text = resp.content.decode("utf-8", errors="replace")
    path.write_text(text, encoding="utf-8")
    return text


def parse_amfi_navall(text: str) -> list[Scheme]:
    """
    Parse AMFI NAVAll.txt. Data lines look like:
    code;ISIN growth;ISIN reinvest;Scheme Name;Plan;Option;NAV;Date
    (older files had no Plan/Option columns; Plan/Option are blank for
    schemes whose name already says "Direct Plan - Growth").
    Category headers ("Open Ended Schemes(Equity Scheme - Mid Cap Fund)")
    and AMC names appear on their own lines between blocks.
    """
    schemes, category, amc = [], "", ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("Scheme Code"):
            continue
        parts = line.split(";")
        if len(parts) >= 6 and parts[0].isdigit():
            if len(parts) >= 8:
                name = " - ".join(p.strip(" -") for p in parts[3:6] if p.strip(" -"))
                nav_s, nav_date = parts[6], parts[7]
            else:
                name, nav_s, nav_date = parts[3].strip(), parts[4], parts[5]
            try:
                nav = float(nav_s)
            except ValueError:
                continue  # NAV sometimes "N.A."
            schemes.append(Scheme(parts[0], name, amc, category,
                                  nav, nav_date.strip(), parts[1].strip(), parts[2].strip()))
        elif "Schemes(" in line or "Schemes (" in line:
            category = line
        else:
            amc = line
    return schemes


class AmfiLatest:
    def fetch(self) -> list[Scheme]:
        return parse_amfi_navall(_cached_get(AMFI_NAV_URL, "NAVAll.txt"))


class MfapiHistory:
    def fetch(self, scheme_code: str) -> tuple[pd.Series, dict]:
        """Return (NAV series, scheme meta)."""
        text = _cached_get(MFAPI_URL.format(code=scheme_code), f"mfapi_{scheme_code}.json")
        payload = json.loads(text)
        return parse_mfapi(payload), payload.get("meta", {})


def parse_mfapi(payload: dict) -> pd.Series:
    rows = payload.get("data", [])
    s = pd.Series(
        [float(r["nav"]) for r in rows],
        index=pd.to_datetime([r["date"] for r in rows], format="%d-%m-%Y"),
        name="nav",
    )
    return s.sort_index()
