"""
Paper-trading ledger for delta-neutral funding positions.

Each Position is two legs on the same symbol, opposite direction, equal
USD notional, on two different venues. PnL has two components each
poll:
  1. Funding accrual: notional * (short_venue_rate - long_venue_rate)
     for the elapsed fraction of an hour since the last mark. Positive
     when the short leg's funding exceeds the long leg's — which is
     the whole point of the trade.
  2. Basis PnL: the two legs' prices don't move in perfect lockstep
     (different venues, different oracle/mark conventions), so a small
     residual price PnL exists even though the position is "delta
     neutral" at entry. This is usually the smaller term but is not
     zero, and can matter around liquidations/oracle divergence.

This module never talks to a real exchange — it exists so the
strategy's expected economics can be validated against REAL, live
funding data before any capital or API keys are involved.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field

from config import (
    ASSUMED_ROUNDTRIP_COST_PCT,
    MAX_HOLD_HOURS,
    MIN_HOLD_APR_SPREAD_PCT,
    STARTING_EQUITY_USD,
    STATE_FILE,
)

log = logging.getLogger("portfolio")


@dataclass
class Position:
    symbol: str
    short_venue: str
    long_venue: str
    notional_usd: float
    entry_short_price: float
    entry_long_price: float
    entry_time: float
    last_mark_time: float
    funding_pnl_usd: float = 0.0
    basis_pnl_usd: float = 0.0
    entry_cost_usd: float = 0.0
    closed: bool = False
    close_time: float | None = None
    close_reason: str | None = None

    @property
    def age_hours(self) -> float:
        return (time.time() - self.entry_time) / 3600

    @property
    def total_pnl_usd(self) -> float:
        return self.funding_pnl_usd + self.basis_pnl_usd - self.entry_cost_usd


@dataclass
class Portfolio:
    equity_usd: float = STARTING_EQUITY_USD
    realized_pnl_usd: float = 0.0
    positions: list[Position] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)

    # -- persistence ----------------------------------------------------
    def save(self, path: str = STATE_FILE) -> None:
        payload = {
            "equity_usd": self.equity_usd,
            "realized_pnl_usd": self.realized_pnl_usd,
            "positions": [asdict(p) for p in self.positions],
            "history": self.history,
        }
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)

    @classmethod
    def load(cls, path: str = STATE_FILE) -> "Portfolio":
        if not os.path.exists(path):
            return cls()
        with open(path) as f:
            raw = json.load(f)
        pf = cls(
            equity_usd=raw.get("equity_usd", STARTING_EQUITY_USD),
            realized_pnl_usd=raw.get("realized_pnl_usd", 0.0),
            history=raw.get("history", []),
        )
        pf.positions = [Position(**p) for p in raw.get("positions", [])]
        return pf

    # -- queries ----------------------------------------------------------
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions if not p.closed]

    def has_open_position(self, symbol: str) -> bool:
        return any(p.symbol == symbol and not p.closed for p in self.positions)

    # -- actions ------------------------------------------------------------
    def open_position(self, symbol: str, short_venue: str, long_venue: str,
                       notional_usd: float, short_price: float, long_price: float) -> Position:
        entry_cost = notional_usd * (ASSUMED_ROUNDTRIP_COST_PCT / 100) * 2  # both legs, entry side
        pos = Position(
            symbol=symbol, short_venue=short_venue, long_venue=long_venue,
            notional_usd=notional_usd,
            entry_short_price=short_price, entry_long_price=long_price,
            entry_time=time.time(), last_mark_time=time.time(),
            entry_cost_usd=entry_cost,
        )
        self.positions.append(pos)
        log.info(
            "OPEN %s: short %s @ %.4f | long %s @ %.4f | notional $%.2f",
            symbol, short_venue, short_price, long_venue, long_price, notional_usd,
        )
        return pos

    def mark_position(self, pos: Position, short_rate_hourly: float, long_rate_hourly: float,
                       short_price: float, long_price: float) -> None:
        elapsed_hours = (time.time() - pos.last_mark_time) / 3600
        if elapsed_hours <= 0:
            return

        # 1) funding accrual (the actual strategy edge)
        funding_delta = pos.notional_usd * (short_rate_hourly - long_rate_hourly) * elapsed_hours
        pos.funding_pnl_usd += funding_delta

        # 2) basis pnl: qty per leg computed at entry price, marked at current price
        short_qty = pos.notional_usd / pos.entry_short_price
        long_qty = pos.notional_usd / pos.entry_long_price
        short_leg_pnl = -short_qty * (short_price - pos.entry_short_price)  # short: profit when price falls
        long_leg_pnl = long_qty * (long_price - pos.entry_long_price)       # long: profit when price rises
        pos.basis_pnl_usd = short_leg_pnl + long_leg_pnl

        pos.last_mark_time = time.time()

    def close_position(self, pos: Position, reason: str) -> None:
        exit_cost = pos.notional_usd * (ASSUMED_ROUNDTRIP_COST_PCT / 100) * 2
        pos.entry_cost_usd += exit_cost
        pos.closed = True
        pos.close_time = time.time()
        pos.close_reason = reason
        self.realized_pnl_usd += pos.total_pnl_usd
        self.equity_usd += pos.total_pnl_usd
        self.history.append({
            "symbol": pos.symbol,
            "short_venue": pos.short_venue,
            "long_venue": pos.long_venue,
            "notional_usd": pos.notional_usd,
            "held_hours": round(pos.age_hours, 2),
            "funding_pnl_usd": round(pos.funding_pnl_usd, 4),
            "basis_pnl_usd": round(pos.basis_pnl_usd, 4),
            "total_pnl_usd": round(pos.total_pnl_usd, 4),
            "reason": reason,
        })
        log.info(
            "CLOSE %s (%s/%s) after %.1fh — funding $%.2f, basis $%.2f, net $%.2f — %s",
            pos.symbol, pos.short_venue, pos.long_venue, pos.age_hours,
            pos.funding_pnl_usd, pos.basis_pnl_usd, pos.total_pnl_usd, reason,
        )

    def should_close(self, pos: Position, current_spread_apr_pct: float | None) -> str | None:
        if pos.age_hours >= MAX_HOLD_HOURS:
            return f"max hold time reached ({MAX_HOLD_HOURS}h)"
        if current_spread_apr_pct is not None and current_spread_apr_pct < MIN_HOLD_APR_SPREAD_PCT:
            return f"spread compressed below {MIN_HOLD_APR_SPREAD_PCT}% APR"
        return None
