"""External reference price -> a LEAD signal for the Arcus quote engine.

Why: on a DEX the biggest single source of adverse selection is the stale quote. When the deeper
venue (Binance USDT-M futures for BTC/ETH/SOL) moves first, anyone fast enough hits our resting
order before we have re-priced it. Public research on lead-lag between CEX and DEX perps measures
the lag at roughly 100-700 ms; a market maker that sees the leader's tick can pull/shade the side
that is about to be run over, and can lean its fair value toward where the price is going.

lead_bps > 0  : the reference is ABOVE Arcus (after removing the normal basis) -> Arcus is about
                to rise -> our ASK is the endangered side, a BUY is cheap.
lead_bps < 0  : the reverse.

The normal Arcus-vs-reference basis (funding, premium, USDT vs USD) is tracked with a slow EWMA so
only the *deviation* counts. No signal (ZERO) is returned until the basis has warmed up, and whenever
the reference is stale - so a dead feed degrades to the old behaviour instead of trading on junk.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
from decimal import Decimal
from typing import Callable, Optional

from utils import BPS, ZERO

log = logging.getLogger("reference")


class RefFeed:
    def __init__(self, cfg):
        self.cfg = cfg
        self.bid: Optional[Decimal] = None
        self.ask: Optional[Decimal] = None
        self.ts = 0.0
        self._basis_bps: Optional[float] = None     # EWMA of (arcus_mid / ref_mid - 1) in bps
        self._basis_since: Optional[float] = None
        self._basis_last = 0.0
        self.n_ticks = 0
        self.connected = False

    # ---- inbound ----------------------------------------------------------- #
    def on_quote(self, bid: Decimal, ask: Decimal, now: float) -> None:
        if bid <= 0 or ask <= 0 or bid > ask:
            return
        self.bid, self.ask, self.ts = bid, ask, now
        self.n_ticks += 1

    @property
    def mid(self) -> Optional[Decimal]:
        return (self.bid + self.ask) / 2 if self.bid is not None and self.ask is not None else None

    def fresh(self, now: float) -> bool:
        return self.mid is not None and (now - self.ts) <= self.cfg.ref_stale_s

    # ---- the signal ---------------------------------------------------------- #
    def lead_bps(self, arcus_mid: Optional[Decimal], now: float) -> Decimal:
        """Reference-vs-Arcus deviation (bps) net of the usual basis; ZERO when unusable."""
        if not self.cfg.enable_ref_feed or arcus_mid is None or not self.fresh(now):
            return ZERO
        ref = self.mid
        raw = float((arcus_mid / ref - 1) * BPS)                      # + = Arcus above reference
        if self._basis_bps is None:
            self._basis_bps, self._basis_since, self._basis_last = raw, now, now
            return ZERO
        dt = max(0.0, now - self._basis_last)
        self._basis_last = now
        a = 1.0 - math.exp(-dt / max(self.cfg.ref_basis_tau_s, 1.0))
        dev = raw - self._basis_bps                                    # deviation BEFORE absorbing it
        self._basis_bps += a * dev
        if now - (self._basis_since or now) < self.cfg.ref_warmup_s:
            return ZERO
        lead = -dev                                                    # Arcus below reference -> price will rise
        return Decimal(str(round(max(-25.0, min(25.0, lead)), 4)))

    # ---- websocket loop (Binance bookTicker format: {"b": bid, "a": ask}) ---- #
    async def run(self, now_fn: Callable[[], float], wake: asyncio.Event) -> None:
        if not self.cfg.enable_ref_feed:
            return
        try:
            import websockets
        except ImportError:
            log.warning("websockets not installed - reference feed disabled")
            return
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.cfg.ref_ws_url, ping_interval=15, ping_timeout=15,
                                              max_size=2 ** 20) as ws:
                    log.info("reference feed connected: %s", self.cfg.ref_ws_url)
                    self.connected, backoff = True, 1.0
                    async for raw in ws:
                        try:
                            m = json.loads(raw)
                            m = m.get("data", m)
                            bid = Decimal(str(m.get("b") or m.get("bestBid")))
                            ask = Decimal(str(m.get("a") or m.get("bestAsk")))
                        except Exception:
                            continue
                        self.on_quote(bid, ask, now_fn())
                        wake.set()                       # re-price right away: speed is the whole point
            except asyncio.CancelledError:
                raise
            except Exception as e:                       # blocked region, DNS, reset ... never crash the bot
                log.warning("reference feed problem: %r (retry in %.0fs) - quoting without lead signal", e, backoff)
            self.connected = False
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 60.0)
