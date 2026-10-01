# v8 changes (on top of Level 7)

## Competing in tight spreads / profitability
- TIGHT_JOIN: when spread is 0.3-2.5 bps and signals are benign, quote AT the touch (queue priority) instead of 1-2 ticks behind it. EV hurdle scales to the half-spread.
- Exit target capped to a fraction of the live spread and decays to "plain touch" over TIGHT_EXIT_S (was a flat >=1bp: unreachable at a 0.7bp spread -> stale inventory -> taker cuts).
- Learner dead-band: |markout| <= LEARN_DEADBAND_BPS is noise and no longer ratchets edges wider.
- Reference feed (Binance futures bookTicker): lead signal shifts fair value, pulls the endangered side, and feeds the inventory risk score. Fails safe (stale/blocked feed -> old behaviour). Crypto markets only.
- Learner state saves are throttled (event-loop friendly); forced on shutdown.

## Isolation: never touch other pairs
- Startup/shutdown/halt cleanup no longer uses the account-level cancelAllOrders. It lists open orders filtered by `market`, then re-checks every row client-side (marketId / market name) and sends one signed cancelOrder per own-market order.
- Reconcile "orphan cancel" only cancels rows positively identified as THIS market. Rows with no market field are skipped (ORPHAN_CANCEL_UNVERIFIED=1 to override on single-market accounts).
- scheduleCancel (dead man's switch) is account-wide -> DMS_MODE=off by default; shutdown only disarms a switch this bot armed.
- Positions of other markets are ignored by the ledger (tested).
- Run under a supervisor (systemd Restart=always): without the DMS a killed process leaves quotes resting until the next start, which cleans up this market only.

## Tests
python test_bot.py; python test_level7.py; python test_inventory.py; python test_isolation.py
python backtest.py --minutes 30 --seeds 4     # synthetic A/B, NOT evidence of live profit
