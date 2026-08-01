"""
Central configuration for the delta-neutral funding farming bot.
Edit these values to change behavior — nothing else in the codebase
should need touching for basic tuning.
"""

# ----------------------------------------------------------------------
# Capital & risk
# ----------------------------------------------------------------------
STARTING_EQUITY_USD = 1000.0     # paper-trading starting balance
LEVERAGE_TARGET = 2.0            # notional per leg = equity * LEVERAGE_TARGET / num_concurrent_positions
MAX_CONCURRENT_POSITIONS = 3     # cap how many symbol/venue pairs are open at once
MAX_NOTIONAL_PER_LEG_USD = 2000  # hard cap regardless of leverage math (safety rail)

# ----------------------------------------------------------------------
# Strategy thresholds
# ----------------------------------------------------------------------
# Minimum ANNUALIZED funding spread (%) between the richest and cheapest
# venue before a position is opened. This should comfortably clear
# round-trip taker fees + estimated slippage on both legs.
MIN_ENTRY_APR_SPREAD_PCT = 15.0

# Close a position if the spread compresses below this (capture window closed)
MIN_HOLD_APR_SPREAD_PCT = 4.0

# Close a position if it's been open this long regardless of spread
# (funding regimes drift; don't let paper-trading "positions" run forever)
MAX_HOLD_HOURS = 72

# ----------------------------------------------------------------------
# Assumed round-trip cost per leg (taker fee + slippage), as a fraction
# of notional. Subtracted once at entry and once at exit in the paper
# ledger so displayed APRs aren't fantasy numbers. Tune per venue once
# you have real fill data.
# ----------------------------------------------------------------------
ASSUMED_ROUNDTRIP_COST_PCT = 0.06  # 0.06% = 6 bps per leg, each way

# ----------------------------------------------------------------------
# Universe: canonical symbol -> ticker on each venue.
# Extend this dict to add coins; a venue is simply skipped for a symbol
# if it has no mapping.
# ----------------------------------------------------------------------
SYMBOL_MAP = {
    "BTC": {
        "hyperliquid": "BTC",
        "lighter": "BTC",
        "dydx": "BTC-USD",
        "drift": "BTC-PERP",
    },
    "ETH": {
        "hyperliquid": "ETH",
        "lighter": "ETH",
        "dydx": "ETH-USD",
        "drift": "ETH-PERP",
    },
    "SOL": {
        "hyperliquid": "SOL",
        "lighter": "SOL",
        "dydx": "SOL-USD",
        "drift": "SOL-PERP",
    },
}

# Which adapters to actually poll. Comment venues out to disable them
# without touching exchanges.py.
ENABLED_VENUES = ["hyperliquid", "lighter", "dydx", "drift"]

# ----------------------------------------------------------------------
# Loop / IO
# ----------------------------------------------------------------------
POLL_INTERVAL_SECONDS = 300       # how often to refresh funding & mark positions
HTTP_TIMEOUT_SECONDS = 10
HTTP_MAX_RETRIES = 3

STATE_FILE = "portfolio_state.json"
LOG_FILE = "bot.log"
