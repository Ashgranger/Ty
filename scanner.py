"""
Pulls funding from every enabled venue and ranks cross-venue spread
opportunities per symbol: short the richest (most positive) funding
venue, long the cheapest (most negative / least positive) venue, for
equal USD notional. Delta is ~0 because both legs hold the same asset
in opposite directions; the position earns the funding-rate spread
each interval, roughly independent of price direction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from config import ENABLED_VENUES, SYMBOL_MAP
from exchanges import ADAPTERS, FundingSnapshot

log = logging.getLogger("scanner")


@dataclass
class Opportunity:
    symbol: str
    short_venue: str   # collect funding here (highest rate)
    long_venue: str    # pay/least-collect funding here (lowest rate)
    short_apr_pct: float
    long_apr_pct: float
    spread_apr_pct: float
    short_mark: float
    long_mark: float


def poll_all() -> dict[str, dict[str, FundingSnapshot]]:
    """Returns {symbol: {venue: FundingSnapshot}}."""
    symbols = list(SYMBOL_MAP.keys())
    by_symbol: dict[str, dict[str, FundingSnapshot]] = {s: {} for s in symbols}

    for venue in ENABLED_VENUES:
        adapter_cls = ADAPTERS.get(venue)
        if not adapter_cls:
            log.warning("no adapter registered for venue %s", venue)
            continue
        adapter = adapter_cls()
        try:
            snaps = adapter.fetch_funding(symbols)
        except Exception:
            log.exception("adapter %s raised while fetching funding", venue)
            continue
        for sym, snap in snaps.items():
            by_symbol[sym][venue] = snap

    return by_symbol


def rank_opportunities(by_symbol: dict[str, dict[str, FundingSnapshot]]) -> list[Opportunity]:
    opps: list[Opportunity] = []
    for sym, venues in by_symbol.items():
        if len(venues) < 2:
            continue  # need at least 2 venues quoting this symbol to pair a trade
        ranked = sorted(venues.items(), key=lambda kv: kv[1].apr_pct, reverse=True)
        richest_venue, richest_snap = ranked[0]
        cheapest_venue, cheapest_snap = ranked[-1]
        if richest_venue == cheapest_venue:
            continue
        spread = richest_snap.apr_pct - cheapest_snap.apr_pct
        opps.append(Opportunity(
            symbol=sym,
            short_venue=richest_venue,
            long_venue=cheapest_venue,
            short_apr_pct=richest_snap.apr_pct,
            long_apr_pct=cheapest_snap.apr_pct,
            spread_apr_pct=spread,
            short_mark=richest_snap.mark_price,
            long_mark=cheapest_snap.mark_price,
        ))
    opps.sort(key=lambda o: o.spread_apr_pct, reverse=True)
    return opps
