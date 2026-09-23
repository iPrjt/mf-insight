"""
Zerodha Kite Connect client (mutual fund holdings only).

Flow:
  1. Send the user to login_url(state). They sign in on Zerodha's own page;
     we never see their password.
  2. Zerodha redirects to KITE_REDIRECT_URL with request_token (+ our state).
  3. exchange_token() swaps it for an access_token (valid until ~6 AM next day;
     Kite has no refresh tokens, so the user reconnects when it expires).
  4. mf_holdings() returns units + average price per fund (ISIN).

Env: KITE_API_KEY, KITE_API_SECRET. Register the redirect URL
(e.g. http://127.0.0.1:8000/api/brokers/zerodha/callback) in the Kite
developer console. Serving other users needs a paid Kite Connect app;
the free Personal app only works for the developer's own account.
"""
import hashlib
import os
from dataclasses import dataclass
from urllib.parse import quote

import requests

API = "https://api.kite.trade"
LOGIN = "https://kite.zerodha.com/connect/login"


@dataclass
class BrokerHolding:
    isin: str
    fund_name: str
    folio: str
    units: float
    average_price: float


class BrokerAuthExpired(Exception):
    """Access token rejected; the user must reconnect."""


class KiteClient:
    def __init__(self, api_key: str | None = None, api_secret: str | None = None, session=None):
        self.api_key = api_key or os.environ.get("KITE_API_KEY", "")
        self.api_secret = api_secret or os.environ.get("KITE_API_SECRET", "")
        self.http = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def login_url(self, state: str) -> str:
        return f"{LOGIN}?v=3&api_key={self.api_key}&redirect_params={quote('state=' + state)}"

    def exchange_token(self, request_token: str) -> str:
        checksum = hashlib.sha256((self.api_key + request_token + self.api_secret).encode()).hexdigest()
        r = self.http.post(f"{API}/session/token", headers={"X-Kite-Version": "3"}, timeout=20,
                           data={"api_key": self.api_key, "request_token": request_token, "checksum": checksum})
        body = r.json()
        if r.status_code != 200 or body.get("status") != "success":
            raise BrokerAuthExpired(body.get("message", "Zerodha rejected the login."))
        return body["data"]["access_token"]

    def mf_holdings(self, access_token: str) -> list[BrokerHolding]:
        r = self.http.get(f"{API}/mf/holdings", timeout=20, headers={
            "X-Kite-Version": "3", "Authorization": f"token {self.api_key}:{access_token}"})
        body = r.json()
        if r.status_code == 403 or body.get("error_type") == "TokenException":
            raise BrokerAuthExpired("Your Zerodha session has expired.")
        if body.get("status") != "success":
            raise RuntimeError(body.get("message", "Zerodha returned an error."))
        return [BrokerHolding(h["tradingsymbol"], h.get("fund", ""), h.get("folio") or "",
                              float(h["quantity"]), float(h["average_price"]))
                for h in body["data"] if float(h.get("quantity", 0)) > 0]


class FakeKiteClient(KiteClient):
    """Offline stand-in used in MF_OFFLINE mode and tests."""
    holdings = [
        BrokerHolding("INFDEMO00001", "Demo Flexi Cap Fund - Direct Plan", "Z1", 1200.0, 19.5),
        BrokerHolding("INFDEMO00005", "Demo Multi Asset Allocation - Direct Plan", "Z1", 800.0, 16.0),
    ]

    def __init__(self):
        super().__init__("demo_key", "demo_secret")

    def login_url(self, state: str) -> str:
        return f"/api/brokers/zerodha/callback?request_token=demo&status=success&state={state}"

    def exchange_token(self, request_token: str) -> str:
        if request_token != "demo":
            raise BrokerAuthExpired("Zerodha rejected the login.")
        return "demo_access_token"

    def mf_holdings(self, access_token: str) -> list[BrokerHolding]:
        if access_token != "demo_access_token":
            raise BrokerAuthExpired("Your Zerodha session has expired.")
        return list(self.holdings)
