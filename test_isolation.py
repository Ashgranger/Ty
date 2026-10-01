"""Isolation: the bot must never cancel, modify or disarm anything that belongs to another pair."""
import asyncio, json, sys
from decimal import Decimal as D

import sim
from sim import make

fails = []
def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        fails.append(name)


def spy(bot):
    """Record every signed write (type + payload) instead of sending it."""
    sent = []
    async def write(req):
        sent.append(req)
        return {"status": 202, "result": {"status": "ACK"}}
    bot.ex.write = write
    return sent

def cancels(sent):
    return [r["payload"]["orderId"] for r in sent if r["type"] == "cancelOrder"]

ROWS = [
    {"orderId": "own-1", "marketId": 1, "market": "BTC-USD"},      # ours, known to the bot
    {"orderId": "orph-mine", "marketId": 1, "market": "BTC-USD"},  # ours, unknown to the bot -> orphan
    {"orderId": "eth-1", "marketId": 2, "market": "ETH-USD"},      # OTHER pair
    {"orderId": "eth-2", "market": "ETH-USD"},                     # other pair, name only
    {"orderId": "sol-3", "marketId": "7"},                         # other pair, id only (string)
    {"orderId": "mystery"},                                        # no market field at all
]

async def main():
    print("1. row_scope classifies venue rows")
    bot, s, clk = make()
    om = bot.om
    check("marketId match -> mine", om.row_scope({"orderId": "a", "marketId": 1}) == "mine")
    check("marketId other -> foreign", om.row_scope({"orderId": "a", "marketId": 2}) == "foreign")
    check("string marketId handled", om.row_scope({"orderId": "a", "marketId": "7"}) == "foreign")
    check("market name match (case-insens) -> mine", om.row_scope({"market": "btc-usd"}) == "mine")
    check("market name other -> foreign", om.row_scope({"market": "ETH-USD"}) == "foreign")
    check("no market field -> unknown", om.row_scope({"orderId": "z"}) == "unknown")
    check("marketId wins over a wrong name", om.row_scope({"marketId": 2, "market": "BTC-USD"}) == "foreign")

    print("2. reconcile never cancels another pair's orders as 'orphans'")
    bot, s, clk = make()
    sent = spy(bot)
    from orders import Order
    bot.om.orders["own-1"] = Order("own-1", "BUY", D("80000"), D("0.001"), D("0.001"), 0, 0.0, 0.0)
    bot.om.last_place_ts = -100
    await bot.om.reconcile(ROWS, 500.0)
    check("only our own orphan is cancelled", cancels(sent) == ["orph-mine"], str(cancels(sent)))
    check("cancel carries our marketId", all(r["payload"]["marketId"] == 1 for r in sent if r["type"] == "cancelOrder"))
    check("known own order is left alone", "own-1" not in cancels(sent))

    bot, s, clk = make(ORPHAN_CANCEL_UNVERIFIED=1)
    sent = spy(bot)
    bot.om.orders["own-1"] = Order("own-1", "BUY", D("80000"), D("0.001"), D("0.001"), 0, 0.0, 0.0)
    bot.om.last_place_ts = -100
    await bot.om.reconcile(ROWS, 500.0)
    check("opt-in cancels unmarked rows too, still never a foreign one",
          sorted(cancels(sent)) == ["mystery", "orph-mine"], str(cancels(sent)))

    print("3. cancel_all: per-order, own market only, never the account-wide endpoint")
    bot, s, clk = make()
    sent = spy(bot)
    async def fake_get(rtype, payload, timeout=8.0):
        fake_get.payload = (rtype, payload)
        return {"openOrders": ROWS}
    bot.ex.get = fake_get
    bot.om.orders["own-1"] = Order("own-1", "BUY", D("80000"), D("0.001"), D("0.001"), 0, 0.0, 0.0)
    bot.om.maybe_orders = True
    await bot.om.cancel_all(force=True)
    types = {r["type"] for r in sent}
    check("no cancelAllOrders / scheduleCancel sent", types == {"cancelOrder"}, str(types))
    check("cancelled exactly our own orders", sorted(cancels(sent)) == ["orph-mine", "own-1"], str(cancels(sent)))
    check("listing request is filtered by market name", fake_get.payload[1].get("market") == "BTC-USD", str(fake_get.payload))
    check("book cleared, leftovers flag reset", not bot.om.orders and not bot.om.maybe_orders)

    bot, s, clk = make()
    sent = spy(bot)
    async def bad_get(rtype, payload, timeout=8.0):
        raise asyncio.TimeoutError()
    bot.ex.get = bad_get
    bot.om.maybe_orders = True
    await bot.om.cancel_all(force=True)
    check("listing failure keeps the cleanup pending (retry later)", bot.om.maybe_orders is True)

    print("4. dead man's switch is account-wide -> off unless asked for")
    bot, s, clk = make()
    sent = spy(bot)
    check("default DMS_MODE is off", bot.cfg.dms_mode == "off")
    await bot.shutdown_orders()
    check("shutdown (mode off) sends no scheduleCancel", "scheduleCancel" not in {r["type"] for r in sent}, str({r["type"] for r in sent}))
    bot, s, clk = make(DMS_MODE="account")
    sent = spy(bot)
    await bot.shutdown_orders()
    check("mode account: shutdown disarms (we armed it)", "scheduleCancel" in {r["type"] for r in sent})
    try:
        make(DMS_MODE="yes")
        check("bad DMS_MODE rejected", False)
    except Exception:
        check("bad DMS_MODE rejected", True)

    print("5. positions of other markets never touch our ledger")
    bot, s, clk = make()
    bot.md.update(D("80000.0"), D("80000.1"), D("1"), D("1"), clk.t)
    bot.on_positions({"positions": [{"marketId": 2, "size": "5", "side": "LONG", "marketDisplayName": "ETH-USD"}]}, True, clk.t)
    check("ETH position ignored (our ledger flat)", bot.ledger.position == 0, str(bot.ledger.position))
    bot.on_positions({"positions": [{"marketId": 2, "size": "5", "side": "LONG"}]}, False, clk.t)
    check("non-snapshot update for other market is a no-op", bot.ledger.position == 0)

    print("\n" + ("ALL ISOLATION TESTS PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
    return 0 if not fails else 1

sys.exit(asyncio.run(main()))
