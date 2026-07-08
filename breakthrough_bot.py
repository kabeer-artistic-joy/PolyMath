#!/usr/bin/env python3
"""
Polymarket Rapid Momentum Scalper — Final Combined Bot
========================================================
Built using only the pieces that have actually proven out across this whole
project, not speculative ideas layered back in:

  - Direction is decided SOLELY by delta-from-price-to-beat (BTC's real price
    vs the current window's own open) — this was the one genuine fix that
    corrected a real, confirmed flaw in an earlier bot (betting on a small
    local wiggle instead of the bigger picture). No local-only momentum
    signal is used here at all.
  - Buy price is NOT capped at a fixed ceiling — this bot is explicitly meant
    to catch momentum already in progress, which can mean buying at $0.80,
    not just near $0.50. Ceiling is relative: observed price + small buffer,
    just enough to actually get filled.
  - Sell mechanics reuse the proven resting-order pattern: instantly rest a
    sell at entry + PROFIT_MARGIN the moment a buy confirms, force-exit if
    bracket order (take-profit and stop-loss placed simultaneously) — the ultimate backstop is tied to the
    window's actual close time — a fixed BRACKET_TIMEOUT_SECONDS delay after buying instead.
  - Whole-share flooring, the balance-safety fallback, and the crash-safety
    None-price guard are all carried over unchanged.

Runs continuously throughout the ENTIRE 5-minute window (not just at open),
watching for a real delta signal and acting on it, up to MAX_TRADES_PER_WINDOW
times per window.

IMPORTANT — read before running live:
  This combines proven mechanics into a new configuration (much thinner
  margin, much shorter force-exit, no buy ceiling, more entries per window)
  that has NOT itself been validated with real data. Each PIECE is proven;
  this SPECIFIC COMBINATION is not. Run --dry-run for a meaningful sample
  before ever using --live.

Usage:
  python breakthrough_bot.py --dry-run
  python breakthrough_bot.py --live --amount 2
"""

import time
import json
import csv
import argparse
import threading
import os
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ─── CONFIG ──────────────────────────────────────────────────────────────────

GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"
BINANCE_API = "https://api.binance.com"

SYMBOLS = {"BTC": "BTCUSDT"}
MARKETS = {
    "btc-updown-5m": "BTC",
}

MIN_DELTA_PCT_TO_TRUST = 0.01   # same validated starting point from the momentum bot — filters pure noise
INVERT_SIGNAL = False           # Reverted per explicit request — betting opposite the delta lost too,
                                  # which is real, useful evidence (see explanation), not a dead end.
                                  # (e.g. a $0.01 delta on a $60k+ asset) while still catching real moves.
BUY_CEILING_BUFFER = 0.02        # willing to pay up to (observed price + this) — NOT a fixed cap, since this
                                  # bot is meant to catch momentum already in progress, which can mean buying
                                  # at $0.80, not just near $0.50.
BUY_TIMEOUT_SEC    = 2.0

PROFIT_MARGIN      = 0.15        # take-profit target — raised to 0.15 per explicit request, aiming to win
                                    # big and lose small rather than a thin, spread-vulnerable margin
STOP_LOSS_MARGIN   = 0.06        # WIDENED from 0.02 — real data showed several stop-losses triggering in
                                    # under 2 seconds, consistent with the old $0.02 gap sitting inside normal
                                    # bid-ask spread rather than reflecting real adverse movement. NOTE: for a
                                    # pure random walk with no directional edge, ANY take-profit/stop-loss ratio
                                    # has ~zero expected value in theory (a wider reward just means a lower win
                                    # rate, not higher profitability) — widening the stop specifically targets
                                    # spread noise, not the ratio itself.
                                    # instead of riding it down further
BRACKET_TIMEOUT_SECONDS = 80     # ultimate backstop only — raised from 60 per explicit request, to give a
                                    # still-favorably-moving position more room before being cut off. Reverted
                                    # to a fixed post-buy timer (not tied to window close) per explicit request.

MAX_TRADES_PER_WINDOW = 8        # raised from 6 per explicit request
MONITOR_INTERVAL      = 1.0      # how often to check for a new entry opportunity throughout the window

POLL_INTERVAL_SLOW = 0.15  # TIGHTENED from 0.5s — real data showed stop-loss overshooting its
                             # target by up to $0.26 during fast price moves, since the price can
                             # fall well past the threshold between two polls. Faster polling
                             # doesn't eliminate this (no native exchange stop-order exists here,
                             # confirmed earlier), but it meaningfully shrinks the gap.

# ─── UTILITIES ───────────────────────────────────────────────────────────────

_print_lock = threading.Lock()

def ts_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def log(msg, crypto=""):
    prefix = f"[{crypto}] " if crypto else ""
    with _print_lock:
        print(f"[{ts_str()}] {prefix}{msg}", flush=True)

def now_unix():
    return time.time()


def get_binance_price(symbol: str) -> float | None:
    try:
        r = requests.get(f"{BINANCE_API}/api/v3/ticker/price", params={"symbol": symbol}, timeout=2)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None


def get_window_open_price(symbol: str, window_ts: int) -> float | None:
    """Fetches the real 'price to beat' — BTC's price at the moment this window opened."""
    try:
        r = requests.get(
            f"{BINANCE_API}/api/v3/klines",
            params={"symbol": symbol, "interval": "5m", "startTime": window_ts * 1000, "limit": 1},
            timeout=3,
        )
        r.raise_for_status()
        candles = r.json()
        if candles:
            return float(candles[0][1])
        return None
    except Exception:
        return None


def get_window_market(slug_prefix: str, start_ts: int) -> dict | None:
    slug = f"{slug_prefix}-{start_ts}"
    try:
        r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=3)
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        event = data[0]
    except Exception:
        return None
    markets = event.get("markets", [])
    if not markets:
        return None
    market = markets[0]
    try:
        outcomes       = json.loads(market.get("outcomes", "[]"))
        clob_token_ids = json.loads(market.get("clobTokenIds", "[]"))
    except Exception:
        return None
    if len(outcomes) < 2 or len(clob_token_ids) < 2:
        return None
    tokens = dict(zip(outcomes, clob_token_ids))
    if "Down" not in tokens or "Up" not in tokens:
        return None
    return {
        "slug": slug, "crypto": MARKETS[slug_prefix], "start_ts": start_ts, "close_ts": start_ts + 300,
        "down_token": tokens["Down"], "up_token": tokens["Up"],
        "condition_id": market.get("conditionId", ""), "title": event.get("title", ""),
    }


def get_order_book(token_id: str) -> dict:
    try:
        r = requests.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=2)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def best_ask(book: dict):
    asks = book.get("asks", [])
    if not asks:
        return None, None
    cheapest = min(asks, key=lambda a: float(a["price"]))
    return float(cheapest["price"]), float(cheapest["size"])


def best_bid(book: dict):
    bids = book.get("bids", [])
    if not bids:
        return None, None
    highest = max(bids, key=lambda b: float(b["price"]))
    return float(highest["price"]), float(highest["size"])


def next_window_start(now: float) -> int:
    return int((now // 300) + 1) * 300


# ─── PERSISTENT CSV LOG ──────────────────────────────────────────────────────

CSV_FIELDS = [
    "timestamp", "bot_name", "mode", "crypto", "slug", "trade_num_this_window",
    "delta_side", "delta_value", "delta_pct",
    "buy_result", "buy_price", "buy_shares", "buy_elapsed_ms", "spread_at_buy",
    "sell_result", "sell_price", "seconds_to_sell", "pnl_usd", "notes",
]

class TradeLogger:
    def __init__(self, bot_name: str):
        self.path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_log.csv")
        self.lock = threading.Lock()
        if not os.path.exists(self.path):
            with open(self.path, "w", newline="") as f:
                csv.writer(f).writerow(CSV_FIELDS)

    def write(self, row: dict):
        row = {**{k: "" for k in CSV_FIELDS}, **row}
        with self.lock:
            with open(self.path, "a", newline="") as f:
                csv.writer(f).writerow([row[k] for k in CSV_FIELDS])


# ─── CORE BOT ────────────────────────────────────────────────────────────────

class BreakthroughBot:
    def __init__(self, dry_run: bool, amount: float):
        self.dry_run  = dry_run
        self.amount   = amount
        self.bot_name = os.getenv("BOT_NAME", "breakthrough_bot")
        self.mode_str = "dry_run" if dry_run else "live"
        self.stop_event = threading.Event()
        self.trades = []
        self.trades_lock = threading.Lock()
        self.logger = TradeLogger(self.bot_name)

        self.client = None
        if not dry_run:
            self._init_client()

        log("=" * 70)
        log(f"Rapid Momentum Scalper | {self.mode_str.upper()} | ${amount:.2f}/trade | bot_name={self.bot_name}")
        log(f"Direction: delta-from-price-to-beat only (min {MIN_DELTA_PCT_TO_TRUST}% to trust)"
            + (" | ⚠️ INVERT_SIGNAL=True — betting AGAINST the delta signal (test mode)" if INVERT_SIGNAL else ""))
        log(f"Buy: observed price + ${BUY_CEILING_BUFFER} buffer (no fixed ceiling) | timeout {BUY_TIMEOUT_SEC}s")
        log(f"Sell: bracket order — take-profit entry+${PROFIT_MARGIN} | stop-loss entry-${STOP_LOSS_MARGIN} | "
            f"backstop: {BRACKET_TIMEOUT_SECONDS}s after buying | "
            f"max {MAX_TRADES_PER_WINDOW} trades/window")
        log(f"Trade log: {self.logger.path}")
        log("=" * 70)

    def _init_client(self):
        from py_clob_client_v2 import ClobClient, AssetType, BalanceAllowanceParams
        signature_type = int(os.getenv("POLY_SIGNATURE_TYPE", "3"))
        self.client = ClobClient(
            host=CLOB_API, key=os.environ["POLY_PRIVATE_KEY"], chain_id=137,
            signature_type=signature_type, funder=os.environ["POLY_PROXY_WALLET"],
        )
        self.client.set_api_creds(self.client.create_or_derive_api_key())
        self.client.update_balance_allowance(BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL, signature_type=signature_type,
        ))

    # ── BUY ──────────────────────────────────────────────────────────────────

    def _attempt_buy(self, token: str, observed_price: float, crypto: str) -> dict:
        ceiling = round(observed_price + BUY_CEILING_BUFFER, 4)
        MIN_SHARES = 5  # CONFIRMED via a real live API error on the other bots in this project

        if self.dry_run:
            book = get_order_book(token)
            price, size = best_ask(book)
            if price is not None and price <= ceiling:
                shares = max(MIN_SHARES, round(self.amount / price))
                log(f"[DRY] BUY would fill: ask ${price:.3f} (size {size})", crypto)
                return {"result": "bought", "price": price, "shares": shares}
            log(f"[DRY] BUY missed: no ask <= ${ceiling}", crypto)
            return {"result": "missed", "price": None, "shares": 0}

        from py_clob_client_v2 import OrderArgsV2, Side, OrderType, OrderPayload
        size = max(MIN_SHARES, round(self.amount / ceiling))
        try:
            resp = self.client.create_and_post_order(
                OrderArgsV2(token_id=token, price=ceiling, size=size, side=Side.BUY),
                order_type=OrderType.GTC,
            )
        except Exception as e:
            log(f"❌ BUY order failed to submit: {e}", crypto)
            return {"result": "error", "price": None, "shares": 0}

        order_id = resp.get("orderID", "")
        deadline = now_unix() + BUY_TIMEOUT_SEC
        last_known_size = 0.0
        while now_unix() < deadline:
            try:
                detail = self.client.get_order(order_id)
            except Exception:
                detail = None
            if detail is None:
                break
            try:
                current_size = float(detail.get("size_matched", 0))
                if current_size > last_known_size:
                    last_known_size = current_size
            except (TypeError, ValueError):
                pass
            time.sleep(0.25)

        try:
            self.client.cancel_order(OrderPayload(orderID=order_id))
        except Exception:
            pass

        # REAL GAP CLOSED HERE: previously only checked the real on-chain
        # balance when the poll showed ZERO fill. If the order filled
        # partially right as the timeout/cancel happened, polling could
        # understate the true fill — leaving real shares untracked and never
        # sold later, the same class of danger as the sell-side bug. Now
        # always verifies against the real balance and trusts whichever
        # number is higher, since the on-chain balance is ground truth.
        final_shares = last_known_size
        try:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams
            bal_resp = self.client.get_balance_allowance(BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL, token_id=token,
                signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "3")),
            ))
            real_balance = float(bal_resp.get("balance", 0)) / 1_000_000
            if real_balance > final_shares:
                log(f"⚠️ Real balance ({real_balance}) exceeds tracked fill ({last_known_size}) — using real balance", crypto)
                final_shares = real_balance
        except Exception as e:
            log(f"⚠️ Balance verification failed ({e}) — proceeding with tracked fill amount", crypto)

        if final_shares <= 0:
            log(f"❌ BUY timed out with no confirmed fill after {BUY_TIMEOUT_SEC}s", crypto)
            return {"result": "missed", "price": None, "shares": 0}

        log(f"✅ BUY confirmed: {final_shares} shares at ceiling ${ceiling}, order {order_id[:16]}...", crypto)
        return {"result": "bought", "price": ceiling, "shares": final_shares}

    # ── SELL ─────────────────────────────────────────────────────────────────

    def _watch_for_sell(self, token: str, buy_price: float, raw_shares: float, crypto: str, close_ts: float) -> dict:
        shares = int(raw_shares)
        if shares != raw_shares:
            log(f"⚠️ Buy partially filled: held {raw_shares}, flooring to {shares} whole shares", crypto)
        if shares < 1:
            log("⚠️ Partial fill left less than 1 whole share — forcing immediate exit", crypto)
            exit_result = self._guaranteed_sell(token, raw_shares, crypto)
            pnl = -round(buy_price * raw_shares, 4) if exit_result["price"] is None else round((exit_result["price"] - buy_price) * raw_shares, 4)
            return {**exit_result, "pnl_usd": pnl, "notes": "sub-1-share partial fill"}

        # BRACKET ORDER: take-profit and stop-loss placed at the same time,
        # right after buying. Whichever the price reaches first determines
        # the outcome — a real win capped losses, or a small, controlled
        # loss if the market moves the other way. This replaces the old
        # single-target + blind-timeout design.
        # REAL BUG FIXED HERE: Polymarket only accepts prices between $0.01 and
        # $0.99. Any entry above $0.84 made the full +$0.15 take-profit target
        # invalid (>$0.99) — confirmed live: this caused repeated "could not
        # place take-profit" failures and immediate force-exits exactly when
        # buying into a strongly-moving, already-high-priced market. Clamping
        # gives these trades a real (if smaller) chance at the target instead
        # of guaranteeing an instant exit.
        take_profit_price = min(round(buy_price + PROFIT_MARGIN, 4), 0.99)
        stop_loss_price   = max(round(buy_price - STOP_LOSS_MARGIN, 4), 0.01)

        if take_profit_price <= buy_price:
            # Buy price itself was already too close to $0.99 — there's no
            # valid room left for ANY take-profit above entry. No point
            # placing an order that can't win; go straight to the stop-loss
            # watch only, relying on the backstop if price never drops either.
            log(f"⚠️ Buy price ${buy_price} leaves no room for a take-profit (clamped max is $0.99) — "
                f"skipping take-profit, watching stop-loss only", crypto)
            take_profit_price = None

        log(f"Bracket: take-profit {'$'+str(take_profit_price) if take_profit_price else 'N/A (no room)'} "
            f"(+${PROFIT_MARGIN}) | stop-loss ${stop_loss_price} (-${STOP_LOSS_MARGIN})", crypto)
        buy_time = now_unix()

        if not self.dry_run:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams

            # REAL BUG FIXED HERE: confirmed live — a buy can show as
            # "matched" via the order API before the underlying on-chain
            # settlement fully completes. Trying to place the take-profit
            # order immediately can then fail with "balance: 0" even though
            # the buy genuinely succeeded, forcing an unnecessary early exit
            # (confirmed: this cost a real trade that could have reached its
            # $0.78 take-profit target instead of exiting at a loss). Actively
            # wait for the real balance to reflect the shares, up to 5s,
            # instead of assuming it's available instantly.
            balance_confirmed = False
            wait_deadline = now_unix() + 5
            while now_unix() < wait_deadline:
                try:
                    self.client.update_balance_allowance(BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL, token_id=token,
                        signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "3")),
                    ))
                    bal_resp = self.client.get_balance_allowance(BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL, token_id=token,
                        signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "3")),
                    ))
                    real_balance = float(bal_resp.get("balance", 0)) / 1_000_000
                    if real_balance >= shares - 0.01:
                        balance_confirmed = True
                        break
                except Exception as e:
                    log(f"⚠️ Balance check failed ({e}), retrying...", crypto)
                time.sleep(0.5)
            if not balance_confirmed:
                log(f"⚠️ Balance still not settled after 5s — proceeding anyway, take-profit placement may fail", crypto)

            from py_clob_client_v2 import OrderArgsV2, MarketOrderArgsV2, Side, OrderType, OrderPayload

            # ONLY the take-profit is placed as a real resting order. This is
            # safe — it only ever fills if the market genuinely reaches that
            # price, since no rational buyer pays MORE than the current fair
            # price just to trade with us.
            #
            # REAL BUG FIXED HERE: the stop-loss must NOT be placed as a
            # resting order the same way. A resting sell below the current
            # price is a standing, always-executable bargain — any buyer
            # scanning the book would snap it up immediately, even while the
            # price is genuinely rising, not falling. Polymarket's client
            # library has no native conditional/stop order type (confirmed:
            # no "stop" order type exists in py_clob_client_v2), so instead
            # we watch the real price ourselves and only submit a real sell
            # the INSTANT it actually reaches the stop level — never before.
            tp_order_id = None
            if take_profit_price is not None:
                try:
                    tp_resp = self.client.create_and_post_order(
                        OrderArgsV2(token_id=token, price=take_profit_price, size=shares, side=Side.SELL),
                        order_type=OrderType.GTC,
                    )
                    tp_order_id = tp_resp.get("orderID", "")
                    log(f"Take-profit resting order placed at ${take_profit_price}, order {tp_order_id[:12]}...", crypto)
                except Exception as e:
                    log(f"⚠️ Could not place take-profit order ({e}) — forcing exit immediately", crypto)
                    exit_result = self._guaranteed_sell(token, shares, crypto)
                    return self._exit_outcome(exit_result, buy_price, shares, "take-profit placement failed")

            deadline = buy_time + BRACKET_TIMEOUT_SECONDS
            while now_unix() < deadline:
                # Check take-profit fill (only if one was actually placed)
                if tp_order_id is not None:
                    try:
                        detail = self.client.get_order(tp_order_id)
                    except Exception:
                        detail = None
                    if detail is not None:
                        try:
                            filled = float(detail.get("size_matched", 0))
                        except (TypeError, ValueError):
                            filled = 0
                        if filled >= shares:
                            pnl = round((take_profit_price - buy_price) * shares, 4)
                            return {"result": "sold_take_profit", "price": take_profit_price, "pnl_usd": pnl,
                                    "notes": "take_profit hit"}

                # Check the REAL current price against the stop-loss level —
                # only submit a real sell order once it's actually been reached.
                book = get_order_book(token)
                current_bid, current_bid_size = best_bid(book)
                if current_bid is not None and current_bid <= stop_loss_price:
                    log(f"Stop-loss level reached (bid ${current_bid:.3f} <= ${stop_loss_price}) — "
                        f"cancelling take-profit and exiting now", crypto)
                    if tp_order_id is not None:
                        try:
                            self.client.cancel_order(OrderPayload(orderID=tp_order_id))
                        except Exception:
                            pass
                    exit_result = self._guaranteed_sell(token, shares, crypto)
                    outcome = self._exit_outcome(exit_result, buy_price, shares, "stop_loss hit", fallback_price=current_bid)
                    return {"result": "sold_stop_loss" if outcome["result"] == "exited" else outcome["result"],
                            "price": outcome["price"], "pnl_usd": outcome["pnl_usd"], "notes": outcome["notes"]}

                time.sleep(POLL_INTERVAL_SLOW)

            # Neither triggered within the backstop timeout — cancel the resting
            # take-profit order (the stop-loss was never a real resting order,
            # so there's nothing else to cancel) and force-exit.
            if tp_order_id is not None:
                try:
                    self.client.cancel_order(OrderPayload(orderID=tp_order_id))
                except Exception:
                    pass
            log(f"⏰ {BRACKET_TIMEOUT_SECONDS}s since buying, neither bracket level reached — force-exiting", crypto)
            exit_result = self._guaranteed_sell(token, shares, crypto)
            return self._exit_outcome(exit_result, buy_price, shares, "bracket timeout, force-exit")

        # DRY-RUN
        while now_unix() - buy_time < BRACKET_TIMEOUT_SECONDS:
            book = get_order_book(token)
            bid_price, bid_size = best_bid(book)
            if bid_price is not None and bid_size >= shares:
                elapsed = round(now_unix() - buy_time, 1)
                if take_profit_price is not None and bid_price >= take_profit_price:
                    log(f"[DRY] Take-profit hit: bid ${bid_price:.3f} at {elapsed}s", crypto)
                    pnl = round((take_profit_price - buy_price) * shares, 4)
                    return {"result": "sold_take_profit", "price": take_profit_price, "pnl_usd": pnl,
                            "notes": "take_profit hit", "seconds_to_sell": elapsed}
                elif bid_price <= stop_loss_price:
                    log(f"[DRY] Stop-loss hit: bid ${bid_price:.3f} at {elapsed}s", crypto)
                    pnl = round((stop_loss_price - buy_price) * shares, 4)
                    return {"result": "sold_stop_loss", "price": stop_loss_price, "pnl_usd": pnl,
                            "notes": "stop_loss hit", "seconds_to_sell": elapsed}
            time.sleep(POLL_INTERVAL_SLOW)

        log(f"⏰ {BRACKET_TIMEOUT_SECONDS}s since buying, neither bracket level reached — force-exiting at best price", crypto)
        exit_result = self._force_exit(token, shares, crypto)
        pnl = round((exit_result["price"] - buy_price) * shares, 4) if exit_result["price"] is not None else -round(buy_price * shares, 4)
        return {**exit_result, "pnl_usd": pnl, "notes": "bracket timeout, force-exit", "seconds_to_sell": BRACKET_TIMEOUT_SECONDS}

    def _guaranteed_sell(self, token: str, shares: float, crypto: str, max_market_attempts: int = 2) -> dict:
        """
        Sells the given shares NO MATTER WHAT — never leaves a position unsold
        and unprotected. REAL BUG FIXED HERE: the old code tried a market (FAK)
        sell exactly once, and if it failed with "no orders to match" (a real,
        confirmed live failure — liquidity can vanish for a moment), the
        fallback retried the IDENTICAL thing and failed the IDENTICAL way,
        leaving real shares completely unsold and unprotected. This retries the
        market sell a couple times first (liquidity often reappears within a
        second), then escalates to an increasingly aggressive resting limit
        sell until it actually fills, rather than ever giving up. TIGHTENED:
        real data showed stop-loss fills overshooting their target by up to
        $0.26 during fast price moves — every second spent retrying here is
        a second the price keeps moving further against the position, so
        retries and delays are kept as short as still reasonably useful.
        """
        from py_clob_client_v2 import MarketOrderArgsV2, OrderArgsV2, Side, OrderType, OrderPayload

        for attempt in range(1, max_market_attempts + 1):
            try:
                resp = self.client.create_and_post_market_order(
                    MarketOrderArgsV2(token_id=token, amount=shares, side=Side.SELL),
                    order_type=OrderType.FAK,
                )
                status = str(resp.get("status", "")).lower()
                if status == "matched":
                    exit_price = None
                    try:
                        # CONFIRMED REAL BUG FIXED HERE: takingAmount in this
                        # response is a PLAIN decimal USDC amount, not scaled by
                        # 1,000,000 — confirmed against real fill data (e.g.
                        # takingAmount "3" for 5 shares = $0.60/share, not a
                        # microscopic number). Dividing by 1e6 here was wrong.
                        proceeds = float(resp.get("takingAmount", 0))
                        candidate_price = round(proceeds / shares, 4) if shares else None
                        if candidate_price is not None and 0.01 <= candidate_price < 1:
                            exit_price = candidate_price
                        else:
                            log(f"⚠️ Parsed sell price ${candidate_price} looks invalid — NOT trusting it. Raw: {resp}", crypto)
                    except Exception as e:
                        log(f"⚠️ Could not parse sell fill price ({e}). Raw: {resp}", crypto)
                    return {"result": "exited", "price": exit_price, "price_is_estimate": exit_price is None}
                log(f"⚠️ Market sell attempt {attempt}/{max_market_attempts}: status={status}, retrying...", crypto)
            except Exception as e:
                log(f"⚠️ Market sell attempt {attempt}/{max_market_attempts} failed: {e}", crypto)
            if attempt < max_market_attempts:
                time.sleep(0.3)

        # All market-sell attempts failed — escalate to an increasingly
        # aggressive resting limit sell, all the way down to Polymarket's
        # actual minimum price ($0.01), before ever stopping. REAL FIX: the
        # previous version stopped at 30% of bid and asked for manual
        # intervention — that's not viable running 8 trades across hundreds
        # of markets unattended. Any price this market will actually pay is
        # better than sitting unsold, and $0.01 is always a valid, postable
        # price on this exchange.
        log(f"⚠️ All {max_market_attempts} market-sell attempts failed — escalating to increasingly "
            f"aggressive limit sells down to the exchange minimum, no manual step involved", crypto)

        remaining = shares
        total_proceeds = 0.0  # sum of (price * filled) across every partial fill, for a correct weighted-average price

        def record_fill(attempt_result):
            nonlocal remaining, total_proceeds
            filled = attempt_result["filled"]
            if filled > 0:
                total_proceeds += filled * attempt_result["price"]
                remaining -= filled

        for factor in (0.85, 0.70, 0.50, 0.30, 0.15, 0.05):
            if remaining <= 0:
                break
            book = get_order_book(token)
            current_bid, _ = best_bid(book)
            reference = current_bid if current_bid is not None else 0.5
            aggressive_price = max(round(reference * factor, 2), 0.01)
            attempt_result = self._try_resting_sell(token, remaining, aggressive_price, crypto, wait_seconds=1.5)
            record_fill(attempt_result)
            if remaining <= 0:
                avg_price = round(total_proceeds / shares, 4)
                return {"result": "exited", "price": avg_price, "price_is_estimate": False}

        # Final, absolute floor — Polymarket's actual minimum valid price.
        # If even this doesn't fill, there is genuinely no buyer anywhere in
        # this market at any price, which no selling mechanism can change —
        # the position will settle at resolution like any other outcome.
        if remaining > 0:
            log("Escalated all the way to the exchange minimum ($0.01) — placing final floor order", crypto)
            attempt_result = self._try_resting_sell(token, remaining, 0.01, crypto, wait_seconds=3)
            record_fill(attempt_result)

        if remaining <= 0:
            avg_price = round(total_proceeds / shares, 4)
            return {"result": "exited", "price": avg_price, "price_is_estimate": False}

        if total_proceeds > 0:
            # Sold SOME shares across the escalation but not all — report the
            # real weighted-average price for what did sell, and flag the
            # remainder clearly rather than pretending the whole position closed.
            avg_price = round(total_proceeds / (shares - remaining), 4)
            log(f"Partially exited: sold {shares - remaining}/{shares} shares at avg ${avg_price}, "
                f"{remaining} shares remain unsold and will settle at market resolution", crypto)
            return {"result": "partially_exited", "price": avg_price, "price_is_estimate": False,
                    "shares_remaining_unsold": remaining}

        log("No buyer found anywhere in this market even at the $0.01 floor — "
            "this position will settle at market resolution, same as any other outcome. No action needed.", crypto)
        return {"result": "unsold_no_liquidity", "price": None, "price_is_estimate": False}

    def _try_resting_sell(self, token: str, shares: float, price: float, crypto: str, wait_seconds: float) -> dict:
        """Places one resting sell at the given price and waits briefly for a fill.
        Always returns the actual filled amount (0 if nothing filled) so the
        caller can track partial fills across multiple escalation levels
        instead of losing track of shares that did sell before a timeout."""
        from py_clob_client_v2 import OrderArgsV2, Side, OrderType, OrderPayload
        try:
            resp = self.client.create_and_post_order(
                OrderArgsV2(token_id=token, price=price, size=shares, side=Side.SELL),
                order_type=OrderType.GTC,
            )
            order_id = resp.get("orderID", "")
            log(f"Limit sell placed at ${price} for {shares} shares, order {order_id[:12]}...", crypto)
        except Exception as e:
            log(f"⚠️ Could not place limit sell at ${price} ({e}) — trying next level", crypto)
            return {"filled": 0, "price": price}

        deadline = now_unix() + wait_seconds
        filled_amt = 0.0
        while now_unix() < deadline:
            try:
                detail = self.client.get_order(order_id)
                filled_amt = float(detail.get("size_matched", 0))
            except Exception:
                pass
            if filled_amt >= shares:
                log(f"Limit sell fully filled at ${price}", crypto)
                return {"filled": filled_amt, "price": price}
            time.sleep(0.2)
        try:
            self.client.cancel_order(OrderPayload(orderID=order_id))
        except Exception:
            pass
        if filled_amt > 0:
            log(f"⚠️ Limit sell at ${price} partially filled {filled_amt}/{shares} before timeout — "
                f"continuing with the remainder at the next level", crypto)
        else:
            log(f"⚠️ Limit sell at ${price} did not fill in time — trying next level", crypto)
        return {"filled": filled_amt, "price": price}

    def _exit_outcome(self, exit_result: dict, buy_price: float, shares: float, base_notes: str, fallback_price: float = None) -> dict:
        """Translates a _guaranteed_sell result into a consistent pnl/notes
        format, correctly handling all three real outcomes: fully sold,
        partially sold (some shares still open), or nothing sold at all."""
        if exit_result["result"] == "unsold_no_liquidity":
            return {"result": exit_result["result"], "price": None, "pnl_usd": None,
                    "notes": "no liquidity found at any price — will settle at market resolution"}
        if exit_result["result"] == "partially_exited":
            sold_shares = shares - exit_result["shares_remaining_unsold"]
            realized_pnl = round((exit_result["price"] - buy_price) * sold_shares, 4)
            notes = (f"{base_notes} — PARTIAL: sold {sold_shares}/{shares} shares "
                     f"(realized pnl below is for the sold portion only), "
                     f"{exit_result['shares_remaining_unsold']} shares unsold, will settle at market resolution")
            return {"result": exit_result["result"], "price": exit_result["price"], "pnl_usd": realized_pnl, "notes": notes}
        exit_price = exit_result["price"]
        is_estimate = exit_result.get("price_is_estimate", False)
        if exit_price is None and fallback_price is not None:
            exit_price = fallback_price
            is_estimate = True
        pnl = round((exit_price - buy_price) * shares, 4) if exit_price is not None else -round(buy_price * shares, 4)
        notes = base_notes
        if is_estimate:
            notes += " (price ESTIMATED, not exchange-confirmed — verify against real account)"
        return {"result": exit_result["result"], "price": exit_price, "pnl_usd": pnl, "notes": notes}

    def _force_exit(self, token: str, shares: float, crypto: str) -> dict:
        if self.dry_run:
            book = get_order_book(token)
            price, size = best_bid(book)
            if price is None:
                log("[DRY] No bids at all for force-exit — total loss this trade", crypto)
                return {"result": "no_bids", "price": None}
            log(f"[DRY] Force-exit would fill at ${price:.3f}", crypto)
            return {"result": "exited", "price": price}

        from py_clob_client_v2 import MarketOrderArgsV2, Side, OrderType
        # Capture the current best bid BEFORE selling, so we have a real
        # fallback estimate if the response parsing below doesn't work.
        book_before = get_order_book(token)
        observed_bid_before, _ = best_bid(book_before)
        try:
            resp = self.client.create_and_post_market_order(
                MarketOrderArgsV2(token_id=token, amount=shares, side=Side.SELL),
                order_type=OrderType.FAK,
            )
        except Exception as e:
            log(f"⚠️ Force-exit order failed: {e}", crypto)
            return {"result": "error", "price": None}
        status = str(resp.get("status", "")).lower()
        if status == "matched":
            exit_price = None
            # CONFIRMED REAL BUG FIXED HERE: for a SELL order, makingAmount is
            # the shares GIVEN, not USDC received — takingAmount is what you
            # actually get paid. Using makingAmount here silently computed a
            # price of $0.00 on every force-exit, confirmed against real
            # account history where the actual fills were completely normal.
            try:
                # CONFIRMED REAL BUG FIXED HERE (second instance): takingAmount
                # is a PLAIN decimal USDC amount in this response, not scaled by
                # 1,000,000 — confirmed against real fill data. Dividing by 1e6
                # was wrong and made every real fill look invalid.
                proceeds = float(resp.get("takingAmount", 0))
                candidate_price = round(proceeds / shares, 4) if shares else None
                if candidate_price is not None and 0.01 <= candidate_price < 1:
                    exit_price = candidate_price
                else:
                    log(f"⚠️ Parsed force-exit price ${candidate_price} looks invalid — "
                        f"NOT trusting it. Raw response: {resp}", crypto)
            except Exception as e:
                log(f"⚠️ Could not parse force-exit fill price ({e}). Raw response: {resp}", crypto)
            if exit_price is None:
                exit_price = observed_bid_before
                log(f"Using observed bid ${observed_bid_before} as an ESTIMATE for this force-exit — "
                    f"verify against your real account", crypto)
            return {"result": "exited", "price": exit_price}
        return {"result": "unmatched", "price": None}

    # ── WINDOW LOOP ──────────────────────────────────────────────────────────

    def _monitor_window(self, slug_prefix: str, start_ts: int):
        crypto = MARKETS[slug_prefix]
        close_ts = start_ts + 300
        symbol = SYMBOLS.get(crypto)

        market = None
        find_deadline = now_unix() + 5
        while now_unix() < find_deadline:
            market = get_window_market(slug_prefix, start_ts)
            if market:
                break
            time.sleep(0.5)
        if not market:
            log(f"Could not find market for window starting {start_ts} — skipping entire window", crypto)
            return

        window_open_price = get_window_open_price(symbol, start_ts) if symbol else None
        if window_open_price:
            log(f"Price to beat this window: ${window_open_price:,.2f}", crypto)
        else:
            log("Could not fetch price-to-beat — skipping entire window (no reliable direction signal without it)", crypto)
            return

        trades_this_window = 0
        while now_unix() < close_ts and trades_this_window < MAX_TRADES_PER_WINDOW:
            if self.stop_event.is_set():
                return

            current_btc_price = get_binance_price(symbol) if symbol else None
            if current_btc_price is None:
                time.sleep(MONITOR_INTERVAL)
                continue

            delta_value = current_btc_price - window_open_price
            delta_pct = abs(delta_value) / window_open_price * 100
            delta_side = "Up" if delta_value > 0 else "Down"

            if delta_pct < MIN_DELTA_PCT_TO_TRUST:
                time.sleep(MONITOR_INTERVAL)
                continue

            raw_delta_side = delta_side
            if INVERT_SIGNAL:
                delta_side = "Down" if raw_delta_side == "Up" else "Up"

            token = market["up_token"] if delta_side == "Up" else market["down_token"]
            book = get_order_book(token)
            observed_price, _ = best_ask(book)
            if observed_price is None:
                time.sleep(MONITOR_INTERVAL)
                continue

            # Log the real spread at this exact moment — directly tests whether
            # stop-losses are being triggered by real movement or just spread noise.
            observed_bid, _ = best_bid(book)
            spread_at_buy = round(observed_price - observed_bid, 4) if observed_bid is not None else None

            trades_this_window += 1
            invert_note = f" [INVERTED from {raw_delta_side}]" if INVERT_SIGNAL else ""
            log(f"Delta signal (trade {trades_this_window}/{MAX_TRADES_PER_WINDOW}): "
                f"{delta_value:+.2f} ({delta_pct:.4f}%) -> buying {delta_side}{invert_note} @ ~${observed_price} "
                f"(spread: ${spread_at_buy})", crypto)

            buy_info = self._attempt_buy(token, observed_price, crypto)
            row = {
                "timestamp": ts_str(), "bot_name": self.bot_name, "mode": self.mode_str, "crypto": crypto,
                "slug": market["slug"], "trade_num_this_window": trades_this_window,
                "delta_side": delta_side, "delta_value": round(delta_value, 4), "delta_pct": round(delta_pct, 4),
                "buy_result": buy_info["result"], "buy_price": buy_info["price"], "buy_shares": buy_info["shares"],
                "spread_at_buy": spread_at_buy,
            }

            if buy_info["result"] != "bought":
                row.update({"sell_result": "n/a", "sell_price": "", "pnl_usd": 0, "notes": "no buy fill"})
                self._record(row)
                time.sleep(MONITOR_INTERVAL)
                continue

            sell_info = self._watch_for_sell(token, buy_info["price"], buy_info["shares"], crypto, close_ts)
            row.update({
                "sell_result": sell_info["result"], "sell_price": sell_info["price"],
                "seconds_to_sell": sell_info.get("seconds_to_sell", ""),
                "pnl_usd": sell_info["pnl_usd"], "notes": sell_info["notes"],
            })
            self._record(row)
            time.sleep(MONITOR_INTERVAL)

    def _record(self, row: dict):
        with self.trades_lock:
            self.trades.append(row)
        self.logger.write(row)
        pnl = row.get("pnl_usd", 0)
        if pnl is None:
            pnl_str = "PENDING (will settle at market resolution)"
        else:
            pnl_str = f"{'+' if pnl >= 0 else ''}${pnl}"
        log(f"RECORDED: side={row['delta_side']} | buy={row['buy_result']}@{row['buy_price']} | "
            f"sell={row['sell_result']}@{row['sell_price']} | pnl={pnl_str}", row["crypto"])

    def _asset_loop(self, slug_prefix: str):
        crypto = MARKETS[slug_prefix]
        # REAL BUG FIXED HERE: recomputing "next_window_start(now)" after each
        # window finished processing could SKIP an entire window. If the loop
        # took even a few seconds of processing overhead past a window's
        # close, by the time it asked "what's the next window", it was
        # already a few seconds INSIDE the next one — so next_window_start
        # correctly answered with the window AFTER that, silently dropping
        # the one that should have started. Confirmed live: a 5-minute
        # window never started at all. Fix: track windows sequentially
        # (previous start + 300) instead of recomputing from "now" each time
        # — only the very first window ever uses next_window_start().
        next_start_ts = None
        while not self.stop_event.is_set():
            if next_start_ts is None:
                start_ts = next_window_start(now_unix())
            else:
                start_ts = next_start_ts
                if now_unix() > start_ts + 30:
                    # We're badly behind (more than 30s late) — likely a long
                    # stall or restart. Re-sync to the real next window rather
                    # than trying to "catch up" on windows that already closed.
                    log(f"⚠️ Running {int(now_unix() - start_ts)}s behind schedule — re-syncing to the current window", crypto)
                    start_ts = next_window_start(now_unix())

            while now_unix() < start_ts and not self.stop_event.is_set():
                time.sleep(1)
            if self.stop_event.is_set():
                break
            log(f"Monitoring window starting {datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime('%H:%M:%S')} UTC", crypto)
            try:
                self._monitor_window(slug_prefix, start_ts)
            except Exception as e:
                log(f"⚠️ Unhandled error this window: {e}", crypto)
            next_start_ts = start_ts + 300

    def run(self):
        threads = [threading.Thread(target=self._asset_loop, args=(prefix,), daemon=True) for prefix in MARKETS]
        for t in threads:
            t.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log("Stopping...")
            self.stop_event.set()
            self._print_summary()

    def _print_summary(self):
        with self.trades_lock:
            trades = list(self.trades)
        bought      = [t for t in trades if t["buy_result"] == "bought"]
        take_profit = [t for t in bought if t["sell_result"] == "sold_take_profit"]
        stop_loss   = [t for t in bought if t["sell_result"] == "sold_stop_loss"]
        pending     = [t for t in bought if t["pnl_usd"] is None]
        total_pnl = sum(float(t["pnl_usd"]) for t in trades if t["pnl_usd"] is not None)
        log("-" * 70)
        log(f"SUMMARY — {len(trades)} signals, {len(bought)} buy fills")
        log(f"  Take-profit hits: {len(take_profit)}")
        log(f"  Stop-loss hits: {len(stop_loss)}")
        if pending:
            log(f"  Pending settlement (no liquidity found, not yet resolved): {len(pending)}")
        log(f"  Total PnL (excludes pending): {'+' if total_pnl >= 0 else ''}${total_pnl:.2f}")
        log("-" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket Rapid Momentum Scalper")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--amount", type=float, default=2.0)
    args = parser.parse_args()

    bot = BreakthroughBot(dry_run=args.dry_run, amount=args.amount)
    bot.run()
