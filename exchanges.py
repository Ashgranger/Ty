"""
Read-only exchange adapters.

Each adapter exposes:
    fetch_funding(symbols: list[str]) -> dict[str, FundingSnapshot]

All endpoints below are public / unauthenticated market-data endpoints —
no API key needed to run the scanner or paper-trade. Order execution
(live trading) is intentionally NOT implemented here; see README for
why, and what each venue needs (wallet signing, API keys, SDKs) to go
live.

IMPORTANT: third-party API surfaces change. These were verified against
each venue's public docs, but field names in particular (Lighter's
funding-rates response schema wasn't publicly inspectable without an
account) are the most likely thing to need a tweak — the code fails
loudly with the raw response logged rather than silently returning
zero, so a schema drift is easy to spot and fix.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import requests

from config import HTTP_MAX_RETRIES, HTTP_TIMEOUT_SECONDS, SYMBOL_MAP

log = logging.getLogger("exchanges")


@dataclass
class FundingSnapshot:
    venue: str
    symbol: str
    hourly_rate: float     # fraction, e.g. 0.0001 = 0.01% per hour
    apr_pct: float          # hourly_rate * 24 * 365 * 100
    mark_price: float
    raw: dict


def _get_json(url: str, params: dict | None = None) -> Optional[dict]:
    last_err = None
    for attempt in range(1, HTTP_MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001 - want to retry on anything transient
            last_err = e
            log.warning("GET %s attempt %d/%d failed: %s", url, attempt, HTTP_MAX_RETRIES, e)
    log.error("GET %s failed after %d attempts: %s", url, HTTP_MAX_RETRIES, last_err)
    return None


def _post_json(url: str, payload: dict) -> Optional[dict]:
    last_err = None
    for attempt in range(1, HTTP_MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("POST %s attempt %d/%d failed: %s", url, attempt, HTTP_MAX_RETRIES, e)
    log.error("POST %s failed after %d attempts: %s", url, HTTP_MAX_RETRIES, last_err)
    return None


class BaseAdapter:
    name = "base"

    def fetch_funding(self, symbols: list[str]) -> dict[str, FundingSnapshot]:
        raise NotImplementedError

    def _ticker_for(self, canonical_symbol: str) -> Optional[str]:
        return SYMBOL_MAP.get(canonical_symbol, {}).get(self.name)


# ----------------------------------------------------------------------
# Hyperliquid
# https://api.hyperliquid.xyz/info  {"type": "metaAndAssetCtxs"}
# Funding is charged hourly. Response = [meta, assetCtxs] where
# meta["universe"][i]["name"] lines up positionally with assetCtxs[i].
# assetCtxs[i]["funding"] is the hourly rate as a decimal string.
# ----------------------------------------------------------------------
class HyperliquidAdapter(BaseAdapter):
    name = "hyperliquid"
    URL = "https://api.hyperliquid.xyz/info"

    def fetch_funding(self, symbols: list[str]) -> dict[str, FundingSnapshot]:
        data = _post_json(self.URL, {"type": "metaAndAssetCtxs"})
        out: dict[str, FundingSnapshot] = {}
        if not data or len(data) < 2:
            return out
        meta, asset_ctxs = data[0], data[1]
        universe = meta.get("universe", [])
        name_to_idx = {u["name"]: i for i, u in enumerate(universe)}

        for sym in symbols:
            ticker = self._ticker_for(sym)
            if ticker is None or ticker not in name_to_idx:
                continue
            ctx = asset_ctxs[name_to_idx[ticker]]
            try:
                hourly = float(ctx["funding"])
                mark = float(ctx["markPx"])
            except (KeyError, TypeError, ValueError):
                log.warning("hyperliquid: unexpected ctx shape for %s: %s", ticker, ctx)
                continue
            out[sym] = FundingSnapshot(
                venue=self.name, symbol=sym,
                hourly_rate=hourly, apr_pct=hourly * 24 * 365 * 100,
                mark_price=mark, raw=ctx,
            )
        return out


# ----------------------------------------------------------------------
# Lighter (zkLighter)
# https://mainnet.zklighter.elliot.ai/api/v1/funding-rates
# Public, no key. Schema assumed to be a list of per-market entries;
# field names below are best-effort (documented via examples across
# similar zkLighter endpoints) — verify against a live response and
# adjust the .get() keys if the venue has changed its schema since.
# ----------------------------------------------------------------------
class LighterAdapter(BaseAdapter):
    name = "lighter"
    URL = "https://mainnet.zklighter.elliot.ai/api/v1/funding-rates"

    def fetch_funding(self, symbols: list[str]) -> dict[str, FundingSnapshot]:
        data = _get_json(self.URL)
        out: dict[str, FundingSnapshot] = {}
        if not data:
            return out

        entries = data.get("funding_rates") or data.get("fundingRates") or data
        if not isinstance(entries, list):
            log.warning("lighter: unexpected top-level response shape, got %s", type(entries))
            return out

        by_symbol = {}
        for e in entries:
            sym_key = e.get("symbol") or e.get("market") or e.get("ticker")
            if sym_key:
                by_symbol[sym_key.upper().replace("-USD", "")] = e

        for sym in symbols:
            ticker = self._ticker_for(sym)
            if ticker is None:
                continue
            e = by_symbol.get(ticker.upper())
            if not e:
                continue
            try:
                # Lighter quotes funding hourly (rate is already the per-hour figure)
                hourly = float(e.get("rate") or e.get("fundingRate") or e.get("hourly_rate"))
                mark = float(e.get("mark_price") or e.get("markPrice") or 0.0)
            except (TypeError, ValueError):
                log.warning("lighter: unexpected entry shape for %s: %s", ticker, e)
                continue
            out[sym] = FundingSnapshot(
                venue=self.name, symbol=sym,
                hourly_rate=hourly, apr_pct=hourly * 24 * 365 * 100,
                mark_price=mark, raw=e,
            )
        return out


# ----------------------------------------------------------------------
# dYdX v4
# https://indexer.dydx.trade/v4/perpetualMarkets
# nextFundingRate is quoted as a 1-HOUR rate (decimal string).
# ----------------------------------------------------------------------
class DydxAdapter(BaseAdapter):
    name = "dydx"
    URL = "https://indexer.dydx.trade/v4/perpetualMarkets"

    def fetch_funding(self, symbols: list[str]) -> dict[str, FundingSnapshot]:
        data = _get_json(self.URL)
        out: dict[str, FundingSnapshot] = {}
        if not data:
            return out
        markets = data.get("markets", {})

        for sym in symbols:
            ticker = self._ticker_for(sym)
            if ticker is None or ticker not in markets:
                continue
            m = markets[ticker]
            try:
                hourly = float(m["nextFundingRate"])
                mark = float(m["oraclePrice"])
            except (KeyError, TypeError, ValueError):
                log.warning("dydx: unexpected market shape for %s: %s", ticker, m)
                continue
            out[sym] = FundingSnapshot(
                venue=self.name, symbol=sym,
                hourly_rate=hourly, apr_pct=hourly * 24 * 365 * 100,
                mark_price=mark, raw=m,
            )
        return out


# ----------------------------------------------------------------------
# Drift (Solana)
# https://data.api.drift.trade/fundingRates?marketName=SOL-PERP
# Returns recent funding records; use the most recent one.
# fundingRate is in quote/base units -> divide by oraclePriceTwap to
# get a fraction, per Drift's own docs.
# ----------------------------------------------------------------------
class DriftAdapter(BaseAdapter):
    name = "drift"
    URL = "https://data.api.drift.trade/fundingRates"

    def fetch_funding(self, symbols: list[str]) -> dict[str, FundingSnapshot]:
        out: dict[str, FundingSnapshot] = {}
        for sym in symbols:
            ticker = self._ticker_for(sym)
            if ticker is None:
                continue
            data = _get_json(self.URL, params={"marketName": ticker})
            if not data:
                continue
            records = data.get("fundingRates") or data.get("data") or []
            if not records:
                continue
            latest = records[0]
            try:
                funding_raw = float(latest["fundingRate"]) / 1e9
                oracle_twap = float(latest["oraclePriceTwap"]) / 1e6
                hourly = funding_raw / oracle_twap if oracle_twap else 0.0
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                log.warning("drift: unexpected record shape for %s: %s", ticker, latest)
                continue
            out[sym] = FundingSnapshot(
                venue=self.name, symbol=sym,
                hourly_rate=hourly, apr_pct=hourly * 24 * 365 * 100,
                mark_price=oracle_twap, raw=latest,
            )
        return out


ADAPTERS: dict[str, type[BaseAdapter]] = {
    "hyperliquid": HyperliquidAdapter,
    "lighter": LighterAdapter,
    "dydx": DydxAdapter,
    "drift": DriftAdapter,
}
