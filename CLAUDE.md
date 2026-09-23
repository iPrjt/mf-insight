# MF Insight — project context for Claude

## What this is
Subscription web app (India) that tracks a user's mutual fund portfolio with
advanced risk metrics (Sortino, Sharpe, alpha, beta, drawdown, rolling returns,
XIRR) and plain-language insights. Owner: Prjt, building it as a side project.
Primary target user: retail investors who find Groww/Kuvera/INDmoney too shallow.

## Hard rules
- **Analytics only, never advice.** No buy/sell/switch/"you should" recommendations
  in insights, AI output, alerts or UI copy. That needs SEBI RIA registration.
  Insights are neutral observations. Keep the disclaimer under insights.
- **Numbers come from code, never from an LLM.** The future AI layer explains
  precomputed metrics; it must not calculate them.
- **Never store** broker passwords, email passwords, card/bank numbers.
  Broker/Gmail tokens and the statement password are stored encrypted
  (`app/secrets_box.py`) and the statement password only with explicit consent.
- Never commit `.env`, `*.db`, `*.key`.

## Architecture
- `mf_insight/` — pure analytics engine (pandas/numpy). `metrics.py` conventions:
  monthly returns, 3-year lookback, annual risk-free rate (default 6.5%) converted
  to monthly; Sortino MAR = risk-free, all periods in denominator.
- `app/` — FastAPI + SQLite (Postgres later). Single-page vanilla-JS UI in
  `app/static/index.html`.
- `app/plans.py` — Free / Pro ₹149 / Premium ₹399. Gate features with
  `plans.require(user, feature)`; 402 = upgrade prompt. Never check plan names inline.
- `app/navs.py` — AMFI scheme master + mfapi.in NAV history; `MF_OFFLINE=1` uses
  6 simulated demo funds (codes 900001–900006, ISINs INFDEMO0000N).

## Data sources and why
- **Zerodha**: Kite Connect OAuth, `/mf/holdings` only (Coin funds). Gives units +
  average price, no full history → first sync creates `zerodha_opening` rows
  (XIRR shown as "Needs history"), later syncs add `zerodha_sync` deltas.
  Access tokens expire ~6 AM daily; no refresh. Multi-user needs paid Kite Connect app.
- **Groww**: official API is equity/F&O only, no mutual funds. Groww funds come in
  via **CAS statements** (CAMS/KFintech/MF Central), parsed with `casparser`.
- **Automatic CAS import**: Gmail (`gmail.readonly`, Testing mode ≤100 users,
  7-day token expiry until Google verification) and a per-user forwarding address
  via inbound-email webhook `POST /api/inbound/email`.
- CAS imports are **additive + de-duplicated** (`app/statements.py`) and remove
  broker estimate rows for the same fund.
- Account Aggregator is the long-term path but needs a regulated (RBI/SEBI) partner.

## Commands
- `make test` — 36 tests, offline demo data, must stay green
- `make lint` — ruff error rules (same as CI)
- `make demo` / `make run` (live, reads `.env`; see `.env.example`)
- CI: `.github/workflows/ci.yml` runs lint + tests on 3.11/3.12 for every push/PR

## Working agreements
- Add or update tests with every change; run `make lint && make test` before committing.
- Small commits, one branch per feature, PR to `main`, merge when CI is green.
- User-facing text: plain language, Indian number formatting (₹1,23,456), no jargon
  without explanation.

## Known gaps / not yet verified
- Live AMFI + mfapi.in verified 2026-09-23 (AMFI NAVAll.txt is now 8 columns: Plan and
  Option split out). mfapi.in intermittently hangs ~60s and lags AMFI by a few days;
  `_cached_get` retries and falls back to stale cache, API returns 503; `navs.with_latest`
  appends AMFI's latest NAV.
- Gmail connect + a real CAMS mailback CAS (casparser 1.4.1) verified 2026-09-24.
  Real Kite/Zerodha and the forwarding webhook are NOT tested end to end yet.
- Only CAMS (sender `camsonline.com`, subject "Consolidated Account Statement - CAMS
  Mailback Request") is confirmed. Other `STATEMENT_SENDERS` and the subject filter
  (`is_statement_subject`) are guesses for KFintech/NSDL/CDSL/MF Central.
- Coin MFs are demat; a CAMS CAS may omit them → possible double count if a user
  uses both Zerodha sync and a CDSL/NSDL CAS for the same fund.
- App sessions never expire; add expiry before public launch.
- Payments are a dev stub (`/api/billing/dev-activate`).

## Roadmap (next)
1. Test live mode + real CAS + real Gmail; fix issues found
2. Razorpay/Cashfree subscriptions via verified webhooks
3. Nightly NAV history store + precomputed metrics
4. Category medians/quartiles → "below category median" insights
5. AMC monthly holdings → fund overlap insights
6. Postgres + Docker Compose deploy
7. AI layer: monthly report + chat grounded on computed metrics
8. Alerts (ratio drop, manager change, quartile slip)
