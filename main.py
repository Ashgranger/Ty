"""
Delta-neutral funding farming bot — paper trading mode.

Usage:
    python main.py --once        # run a single scan/mark/trade cycle and exit
    python main.py                # loop forever at POLL_INTERVAL_SECONDS
    python main.py --scan-only    # just print the funding scan, no paper trades

State (open positions, realized PnL) persists to portfolio_state.json
between runs, so stopping and restarting the bot doesn't lose history.
"""

from __future__ import annotations

import argparse
import logging
import time

from config import LOG_FILE, POLL_INTERVAL_SECONDS, STATE_FILE
from engine import run_cycle
from portfolio import Portfolio
from scanner import poll_all, rank_opportunities


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
    )


def print_scan() -> None:
    by_symbol = poll_all()
    opps = rank_opportunities(by_symbol)
    print(f"\n{'SYMBOL':<6} {'SHORT VENUE':<14} {'SHORT APR':>10} {'LONG VENUE':<14} {'LONG APR':>10} {'SPREAD':>10}")
    print("-" * 70)
    for o in opps:
        print(f"{o.symbol:<6} {o.short_venue:<14} {o.short_apr_pct:>9.1f}% {o.long_venue:<14} "
              f"{o.long_apr_pct:>9.1f}% {o.spread_apr_pct:>9.1f}%")
    if not opps:
        print("(no data — check network access / venue adapters)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--scan-only", action="store_true", help="print funding scan only, no trading")
    args = parser.parse_args()

    setup_logging()
    log = logging.getLogger("main")

    if args.scan_only:
        print_scan()
        return

    pf = Portfolio.load(STATE_FILE)
    log.info("loaded portfolio: equity=$%.2f, %d open position(s)", pf.equity_usd, len(pf.open_positions()))

    if args.once:
        run_cycle(pf)
        return

    log.info("starting continuous loop, polling every %ds (Ctrl+C to stop)", POLL_INTERVAL_SECONDS)
    try:
        while True:
            run_cycle(pf)
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        log.info("stopped by user, final state saved to %s", STATE_FILE)


if __name__ == "__main__":
    main()
