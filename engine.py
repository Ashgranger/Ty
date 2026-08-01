from __future__ import annotations

import logging

from config import (
    LEVERAGE_TARGET,
    MAX_CONCURRENT_POSITIONS,
    MAX_NOTIONAL_PER_LEG_USD,
    MIN_ENTRY_APR_SPREAD_PCT,
)
from portfolio import Portfolio
from scanner import poll_all, rank_opportunities

log = logging.getLogger("engine")


def run_cycle(pf: Portfolio) -> None:
    by_symbol = poll_all()
    opps = rank_opportunities(by_symbol)

    if not opps:
        log.warning("no funding data returned this cycle (check venue adapters / network)")
    else:
        top = opps[0]
        log.info(
            "best spread: %s short=%s(%.1f%% APR) long=%s(%.1f%% APR) -> spread=%.1f%% APR",
            top.symbol, top.short_venue, top.short_apr_pct,
            top.long_venue, top.long_apr_pct, top.spread_apr_pct,
        )

    # ---- 1. mark & possibly close existing positions ----
    for pos in pf.open_positions():
        snaps = by_symbol.get(pos.symbol, {})
        short_snap = snaps.get(pos.short_venue)
        long_snap = snaps.get(pos.long_venue)
        if not short_snap or not long_snap:
            log.warning("missing fresh data for open position %s, skipping mark this cycle", pos.symbol)
            continue

        pf.mark_position(
            pos,
            short_rate_hourly=short_snap.hourly_rate,
            long_rate_hourly=long_snap.hourly_rate,
            short_price=short_snap.mark_price,
            long_price=long_snap.mark_price,
        )

        current_spread = short_snap.apr_pct - long_snap.apr_pct
        reason = pf.should_close(pos, current_spread)
        if reason:
            pf.close_position(pos, reason)

    # ---- 2. open new positions from top opportunities, if room ----
    open_symbols = {p.symbol for p in pf.open_positions()}
    slots_free = MAX_CONCURRENT_POSITIONS - len(pf.open_positions())

    for opp in opps:
        if slots_free <= 0:
            break
        if opp.symbol in open_symbols:
            continue
        if opp.spread_apr_pct < MIN_ENTRY_APR_SPREAD_PCT:
            break  # sorted descending, nothing further will qualify

        notional = min(
            MAX_NOTIONAL_PER_LEG_USD,
            (pf.equity_usd * LEVERAGE_TARGET) / MAX_CONCURRENT_POSITIONS,
        )

        pf.open_position(
            symbol=opp.symbol,
            short_venue=opp.short_venue,
            long_venue=opp.long_venue,
            notional_usd=notional,
            short_price=opp.short_mark,
            long_price=opp.long_mark,
        )
        open_symbols.add(opp.symbol)
        slots_free -= 1

    pf.save()

    log.info(
        "cycle done — equity $%.2f | realized $%.2f | open positions %d",
        pf.equity_usd, pf.realized_pnl_usd, len(pf.open_positions()),
    )
