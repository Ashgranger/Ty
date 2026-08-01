# Delta-Neutral Funding Farming Bot (Paper Trading)

Scans funding rates across **Hyperliquid, Lighter, dYdX v4, and Drift**,
finds the biggest funding-rate spread on a symbol between two venues,
and paper-trades a delta-neutral position: **short the venue with the
richest funding, long the venue with the cheapest**, in equal USD
notional. The position is close to market-neutral (both legs hold the
same asset), so the P&L driver is the funding-rate spread itself, not
price direction.

This ships in **paper trading mode only** — it polls real, live
funding data from each venue's public API, but no orders are ever
sent and no wallet/API key is required to run it.

## How it works

1. `scanner.py` polls each venue's public market-data endpoint every
   cycle and normalizes funding to an annualized % (APR) for
   apples-to-apples comparison.
2. `engine.py` marks open positions against fresh data, closes any
   whose spread has compressed or that have hit the max hold time,
   then opens new positions on the best remaining opportunities
   (up to `MAX_CONCURRENT_POSITIONS`).
3. `portfolio.py` is the paper ledger: it accrues funding P&L each
   cycle based on the *actual* rate differential observed, tracks a
   small residual "basis" P&L from the two legs' prices not moving in
   perfect lockstep, and persists everything to `portfolio_state.json`
   so restarting the script doesn't lose your track record.

## Setup

```bash
pip install -r requirements.txt

# one-off: just see the current funding landscape, no trading
python main.py --scan-only

# run one scan + trade cycle and exit (good for a cron job)
python main.py --once

# run continuously, polling every POLL_INTERVAL_SECONDS (config.py)
python main.py
```

Needs outbound network access to `api.hyperliquid.xyz`,
`mainnet.zklighter.elliot.ai`, `indexer.dydx.trade`, and
`data.api.drift.trade` — run it from your Codespace or any machine
with normal internet access, not a network-isolated sandbox.

## Tuning (`config.py`)

- `MIN_ENTRY_APR_SPREAD_PCT` — spread required to open a position.
  Set well above your assumed round-trip cost so the edge is real
  after fees/slippage, not just before.
- `ASSUMED_ROUNDTRIP_COST_PCT` — your best guess at taker fee +
  slippage per leg. Tighten this once you have real fill data.
- `MAX_CONCURRENT_POSITIONS`, `LEVERAGE_TARGET`,
  `MAX_NOTIONAL_PER_LEG_USD` — position sizing / risk caps.
- `SYMBOL_MAP` — add more coins by adding their ticker on each venue.

## Known gaps before this could go live

This is a paper-trading skeleton, not an execution system. Going live
would require, at minimum:

- **Real order execution per venue** — Hyperliquid and dYdX need wallet
  signing (Hyperliquid uses EIP-712 signed actions; dYdX v4 needs a
  Cosmos-SDK transaction via `dydx-v4-client`). Lighter needs its own
  signer/SDK. Drift is Solana-native and needs a funded Solana wallet
  and the `driftpy` SDK. None of that is wired up here.
- **Margin/liquidation monitoring per venue** — this bot doesn't check
  whether either leg is anywhere near liquidation. A real version
  needs to pull account margin ratios and auto-deleverage or top up
  before either leg gets liquidated (which would instantly break the
  "delta neutral" assumption and leave you naked on the other leg).
- **Real slippage/depth checks** — the scanner assumes you can enter
  and exit at mark price. For real size, check order-book depth before
  sizing a position, especially on the less liquid venue in a pair.
- **Withdrawal/bridging friction** — capital efficiency across 4
  separate DEXs on different chains means real transfer times and
  costs between venues, which this model ignores entirely.
- **Lighter's response schema is unverified** — I could not pull an
  authenticated example response for `funding-rates`, so
  `exchanges.py` parses it defensively and logs the raw payload if the
  expected fields aren't found. Run `--scan-only` first and check the
  logs before trusting Lighter's numbers.

## Risks specific to this strategy (even once "live")

- **Funding can flip or spike** — a rich funding rate can reverse
  between polls, especially around volatile price moves; the spread
  you entered on isn't guaranteed to persist.
- **Two-venue execution risk** — you can get filled on one leg and not
  the other (network hiccup, one venue's liquidity drying up),
  leaving you directionally exposed until you close the gap.
- **Smart contract / custody risk is doubled** — you're now trusting
  two separate protocols' contracts and liquidation engines instead
  of one.
