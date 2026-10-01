#!/usr/bin/env python3
"""Synthetic microstructure backtest: old behaviour vs v8, driving the REAL Strategy/Ledger/MarketData.

    python backtest.py [--minutes 40] [--seeds 4]

WHAT THIS IS: a model world with the ingredients that decide whether a maker survives a tight
market - a one/two-tick spread, queue position (we only fill part of the time we are at the touch),
quote latency, a leading reference price that Arcus follows with a lag, and "informed" takers that
pick off stale quotes. It checks the code does what it is designed to do and gives a feel for the size
of the effects.
WHAT THIS IS NOT: evidence of live profit. The lead-lag effect is an ASSUMPTION of the scenarios (the
literature measures ~100-700ms for CEX->DEX perps; nobody has published it for Arcus). Validate on
the paper run (DRY_RUN=1 with the reference feed on) and with analyze.py before risking size.
"""
from __future__ import annotations

import argparse
import math
import os
import random
import statistics
import types
from collections import deque
from decimal import Decimal as D

import sim
from market import Market
from utils import BUY, SELL, q_down

TICK = D("0.01")
MKT = Market(1, "SOL-USD", "ONLINE", TICK, D("0.01"), [], D("5"), D("0.01"), D("100000"), D("0"), False)


def load_env_example() -> dict:
    env = {}
    for line in open(os.path.join(os.path.dirname(__file__) or ".", ".env.example")):
        line = line.split("#")[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    for k in ("ARCUS_ENV", "ARCUS_WALLET_ADDRESS", "ARCUS_API_SIGNING_KEY", "DRY_RUN"):
        env.pop(k, None)
    return env


BASE_ENV = load_env_example()
OLD = {"ENABLE_REF_FEED": "0", "TIGHT_JOIN": "0", "TIGHT_SPREAD_BPS": "0", "LEARN_DEADBAND_BPS": "0",
       "EXIT_CAP_SPREAD_FRAC": "100"}
SCENARIOS = {
    # name: (arcus lag s, informed prob per 0.1s when stale, uninformed taker rate /s per side, vol bps per sqrt(s))
    "A tight market, Arcus lags leader 0.4s": (0.4, 0.45, 0.6, 1.0),
    "B tight market, NO lead-lag (pure noise flow)": (0.0, 0.0, 0.6, 1.0),
    "C toxic: lag 0.8s, aggressive pickers, vol x1.6": (0.8, 0.8, 0.6, 1.6),
}


def run(env_over: dict, scenario: tuple, seed: int, minutes: float):
    lag, p_inf, rate, vol = scenario
    rnd = random.Random(seed)
    env = dict(BASE_ENV)
    env.update({"MARKET": "SOL-USD", "ORDER_USD": "25", "MAX_POSITION_USD": "100", "SESSION_MAX_LOSS_USD": "1000000",
                "ENABLE_ONLINE_LEARNING": "1", "MAX_ACTIONS_PER_MIN": "100000"})
    env.update(env_over)
    bot, _, clock = sim.make(**env)
    bot.md.info = MKT
    bot._pull_side = lambda side, now: None
    cfg, md, lg = bot.cfg, bot.md, bot.ledger

    dt, plan_every, quote_lat, ref_lat = 0.1, 2, 0.15, 0.05
    steps = int(minutes * 60 / dt)
    lag_n, ref_lat_n = int(round(lag / dt)), int(round(ref_lat / dt))
    ref_hist = [150.0]
    mids = []
    spread_ticks, live, plans = 1, [], deque()
    last_take, fills_log, n_takes = -1e9, [], 0
    sig = vol / 1e4 * math.sqrt(dt)
    basis = 1.0 / 1e4                                    # Arcus normally trades 1bp above the reference

    def level(px):                                       # snap to tick
        return D(str(round(px / float(TICK)) * float(TICK))).quantize(TICK)

    for i in range(steps):
        t = 1000.0 + i * dt
        clock.t = t
        bot.md.info_ts = t
        ref = ref_hist[-1] * math.exp(rnd.gauss(0, sig))
        ref_hist.append(ref)
        fair = ref_hist[max(0, len(ref_hist) - 1 - lag_n)] * (1 + basis)
        if i % 10 == 0:
            spread_ticks = 1 if rnd.random() < 0.75 else 2
        mid_t = fair / float(TICK)
        bid_t = round(mid_t - spread_ticks / 2)
        bid, ask = D(bid_t) * TICK, D(bid_t + spread_ticks) * TICK
        md.update(bid, ask, D(str(round(rnd.uniform(0.5, 2), 2))), D(str(round(rnd.uniform(0.5, 2), 2))), t)
        rseen = ref_hist[max(0, len(ref_hist) - 1 - ref_lat_n)]
        bot.ref.on_quote(D(str(round(rseen * 0.99997, 4))), D(str(round(rseen * 1.00003, 4))), t)
        mid = md.mid
        mids.append(float(mid))

        # ---- the bot: plan every 0.2s, orders become live after quote latency -----------------
        if i % plan_every == 0:
            lg.process_markouts(mid, t)
            lg.last_now = lg.current_now = t
            lg.learner.tick_decay(t)
            plan = bot.strategy.plan(bot.snapshot(t))
            for side in (BUY, SELL):
                code = plan.blocked.get(side)
                if code == "trend":
                    bot.cooldown[side] = max(bot.cooldown[side], t + cfg.trend_hold_s)
            tg = {BUY: ([plan.bid] if plan.bid else []) + plan.extra_bids,
                  SELL: ([plan.ask] if plan.ask else []) + plan.extra_asks}
            plans.append((t + quote_lat, tg, plan))
            inv = plan.inv
            if inv is not None and inv.taker and t - last_take > cfg.taker_cooldown_s and not lg.is_flat(mid, MKT.min_notional):
                side = SELL if lg.position > 0 else BUY
                qty = q_down(abs(lg.position) * inv.taker_frac, MKT.step) or abs(lg.position)
                px = md.bid if side == SELL else md.ask
                o = types.SimpleNamespace(role="take", level=0)
                bot.on_fill(side, qty, px, o)
                last_take, n_takes = t, n_takes + 1
                fills_log.append((i, side, float(px), True))
        while plans and plans[0][0] <= t:
            _, tg, _ = plans.popleft()
            new = []
            for side in (BUY, SELL):
                for tr in tg[side]:
                    new.append({"side": side, "price": tr.price, "qty": tr.qty, "role": tr.role, "level": tr.level})
            live = new

        # ---- takers ----------------------------------------------------------------------------
        def trade(side, n_ticks, informed):
            """A taker of `side` sweeping n_ticks levels. Returns after filling any of our resting orders."""
            nonlocal live
            md.on_trade("BUY" if side == BUY else "SELL", D("1"), ask if side == BUY else bid, t)
            keep = []
            for o in live:
                hit = False
                if side == BUY and o["side"] == SELL:
                    lim = ask + TICK * (n_ticks - 1)
                    if o["price"] <= lim:
                        p = 0.9 if o["price"] < ask else (0.45 if o["price"] == ask else 0.55)
                        hit = rnd.random() < p
                elif side == SELL and o["side"] == BUY:
                    lim = bid - TICK * (n_ticks - 1)
                    if o["price"] >= lim:
                        p = 0.9 if o["price"] > bid else (0.45 if o["price"] == bid else 0.55)
                        hit = rnd.random() < p
                if hit:
                    if o["role"] == "reduce" and abs(lg.position) < o["qty"]:
                        o = dict(o, qty=abs(lg.position))
                    if o["qty"] > 0:
                        bot.on_fill(o["side"], o["qty"], o["price"], types.SimpleNamespace(role=o["role"], level=o["level"]))
                        fills_log.append((i, o["side"], float(o["price"]), False))
                    continue
                keep.append(o)
            live = keep

        pu = 1 - math.exp(-rate * dt)
        for side in (BUY, SELL):
            if rnd.random() < pu:
                trade(side, 1 if rnd.random() < 0.8 else 1 + int(rnd.expovariate(0.8)), False)
        gap_ticks = (rseen * (1 + basis) - fair) / fair * 1e4 / (float(TICK) / float(mid) * 1e4)   # ref vs Arcus, in ticks
        if abs(gap_ticks) > 0.8 and rnd.random() < p_inf:
            trade(BUY if gap_ticks > 0 else SELL, 1 + int(abs(gap_ticks)), True)

    end_mid = md.mid
    pnl = float(lg.total_pnl(end_mid))
    # 5s markout of every maker fill, measured against the mid
    mo = []
    for (i, side, px, taker) in fills_log:
        if taker or i + 50 >= len(mids):
            continue
        m = (mids[i + 50] - px) if side == BUY else (px - mids[i + 50])
        mo.append(m / px * 1e4)
    return {"pnl": pnl, "fills": lg.n_fills, "vol": float(lg.volume_usd), "markout": statistics.mean(mo) if mo else 0.0,
            "takes": n_takes, "edge": float(lg.avg_edge_bps), "end_inv": float(lg.position * end_mid)}


def main():
    import logging; logging.disable(logging.CRITICAL)
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=40)
    ap.add_argument("--seeds", type=int, default=4)
    ap.add_argument("--ablate", action="store_true", help="switch v8 features off one at a time")
    a = ap.parse_args()
    if a.ablate:
        rows = [("v8 full", {}), ("  - reference feed", {"ENABLE_REF_FEED": "0"}),
                ("  - tight exit decay", {"TIGHT_EXIT_S": "100000"}), ("  - tight join + EV scaling", {"TIGHT_JOIN": "0", "TIGHT_EV_FRAC": "100"}),
                ("  - learner deadband", {"LEARN_DEADBAND_BPS": "0"}), ("old (v7)", OLD)]
        for name, scn in SCENARIOS.items():
            print(name)
            for label, over in rows:
                pn = [run(over, scn, 100 + s, a.minutes)["pnl"] * 60 / a.minutes for s in range(a.seeds)]
                print(f"  {label:<28} PnL ${statistics.mean(pn):+7.3f}/h (se {statistics.stdev(pn) / math.sqrt(len(pn)):.3f})")
            print()
        return
    print(f"{a.minutes:g} simulated minutes x {a.seeds} seeds per cell. $25 clips, $100 position cap, tick=0.67bps.\n")
    for name, scn in SCENARIOS.items():
        print(name)
        for label, over in (("old (v7 behaviour)", OLD), ("v8 (this release)", {})):
            rs = [run(over, scn, 100 + s, a.minutes) for s in range(a.seeds)]
            f = lambda k: statistics.mean(r[k] for r in rs)
            pn = [r["pnl"] for r in rs]
            se = statistics.stdev(pn) / math.sqrt(len(pn)) if len(pn) > 1 else 0.0
            per_h = 60 / a.minutes
            print(f"  {label:<20} PnL ${f('pnl') * per_h:+8.3f}/h (se {se * per_h:.3f}) | fills {f('fills'):6.0f} "
                  f"| volume ${f('vol'):8.0f} | 5s markout {f('markout'):+5.2f}bps | taker cuts {f('takes'):4.1f} | end inv ${f('end_inv'):+6.1f}")
        print()


if __name__ == "__main__":
    main()
