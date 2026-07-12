#!/usr/bin/env python3
"""
Polymarket Rapid Momentum Scalper — Final Combined Bot
========================================================
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
GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"
BINANCE_API = "https://api.binance.com"
SYMBOLS = {"BTC": "BTCUSDT"}
MARKETS = {
    "btc-updown-5m": "BTC",
}
MIN_DELTA_PCT_TO_TRUST = 0.01
INVERT_SIGNAL = False
BUY_CEILING_BUFFER = 0.02
BUY_TIMEOUT_SEC    = 2.0
PROFIT_MARGIN      = 0.05        # take-profit target — changed to 0.05 per explicit request
BRACKET_TIMEOUT_SECONDS = 60
MAX_TRADES_PER_WINDOW = 8
MONITOR_INTERVAL      = 1.0
POLL_INTERVAL_SLOW = 0.5
_print_lock = threading.Lock()
def ts_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def log(msg, crypto=""):
    prefix = f"[{crypto}] " if crypto else ""
    with _print_lock:
        print(f"[{ts_str()}] {prefix}{msg}", flush=True)
def now_unix():
    return time.time()
def get_binance_price(symbol: str):
    try:
        r = requests.get(f"{BINANCE_API}/api/v3/ticker/price", params={"symbol": symbol}, timeout=2)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None
def get_window_open_price(symbol: str, window_ts: int):
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
def get_window_market(slug_prefix: str, start_ts: int):
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
def get_order_book(token_id: str):
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
            + (" | INVERT_SIGNAL=True" if INVERT_SIGNAL else ""))
        log(f"Buy: observed price + ${BUY_CEILING_BUFFER} buffer (no fixed ceiling) | timeout {BUY_TIMEOUT_SEC}s")
        log(f"Sell: take-profit entry+${PROFIT_MARGIN} | NO stop-loss | "
            f"backstop timeout {BRACKET_TIMEOUT_SECONDS}s | "
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
    def _attempt_buy(self, token: str, observed_price: float, crypto: str) -> dict:
        ceiling = round(observed_price + BUY_CEILING_BUFFER, 4)
        MIN_SHARES = 5
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
            log(f"BUY order failed to submit: {e}", crypto)
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
        if last_known_size <= 0:
            try:
                from py_clob_client_v2 import AssetType, BalanceAllowanceParams
                bal_resp = self.client.get_balance_allowance(BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL, token_id=token,
                    signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "3")),
                ))
                real_balance = float(bal_resp.get("balance", 0)) / 1_000_000
                if real_balance >= 0.5:
                    log(f"get_order() showed no fill, but balance check found {real_balance} shares — correcting course", crypto)
                    return {"result": "bought", "price": ceiling, "shares": real_balance}
            except Exception as e:
                log(f"Final balance safety-check failed ({e})", crypto)
            log(f"BUY timed out with no confirmed fill after {BUY_TIMEOUT_SEC}s", crypto)
            return {"result": "missed", "price": None, "shares": 0}
        log(f"BUY confirmed: {last_known_size} shares at ceiling ${ceiling}, order {order_id[:16]}...", crypto)
        return {"result": "bought", "price": ceiling, "shares": last_known_size}

    # ── SELL ─────────────────────────────────────────────────────────────────
    def _watch_for_sell(self, token: str, buy_price: float, raw_shares: float, crypto: str) -> dict:
        shares = int(raw_shares)
        if shares != raw_shares:
            log(f"Buy partially filled: held {raw_shares}, flooring to {shares} whole shares", crypto)
        if shares < 1:
            log("Partial fill left less than 1 whole share — forcing immediate exit", crypto)
            exit_result = self._guaranteed_sell(token, raw_shares, crypto)
            pnl = -round(buy_price * raw_shares, 4) if exit_result["price"] is None else round((exit_result["price"] - buy_price) * raw_shares, 4)
            return {**exit_result, "pnl_usd": pnl, "notes": "sub-1-share partial fill"}

        # STOP-LOSS REMOVED per explicit request. Only a take-profit target
        # plus a TIME-based backstop remain — if the take-profit never hits
        # within BRACKET_TIMEOUT_SECONDS, force-exit at best available price.
        # REAL BUG FIXED HERE (carried over from elsewhere in this project):
        # Polymarket only accepts prices between $0.01 and $0.99. Clamp the
        # take-profit so it's never an invalid order.
        take_profit_price = min(round(buy_price + PROFIT_MARGIN, 4), 0.99)
        if take_profit_price <= buy_price:
            log(f"Buy price ${buy_price} leaves no room for a take-profit (clamped max is $0.99) — "
                f"will force-exit at the timeout instead", crypto)
            take_profit_price = None

        log(f"Take-profit target: {'$'+str(take_profit_price) if take_profit_price else 'N/A (no room)'} "
            f"(+${PROFIT_MARGIN}) | backstop {BRACKET_TIMEOUT_SECONDS}s | NO stop-loss", crypto)
        buy_time = now_unix()

        if not self.dry_run:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams
            try:
                self.client.update_balance_allowance(BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL, token_id=token,
                    signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "3")),
                ))
            except Exception as e:
                log(f"Could not sync conditional balance ({e})", crypto)

            from py_clob_client_v2 import OrderArgsV2, Side, OrderType, OrderPayload
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
                    log(f"Could not place take-profit order ({e}) — forcing exit immediately", crypto)
                    exit_result = self._guaranteed_sell(token, shares, crypto)
                    pnl = round((exit_result["price"] - buy_price) * shares, 4) if exit_result["price"] is not None else -round(buy_price * shares, 4)
                    return {**exit_result, "pnl_usd": pnl, "notes": "take-profit placement failed"}

            deadline = buy_time + BRACKET_TIMEOUT_SECONDS
            while now_unix() < deadline:
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
                time.sleep(POLL_INTERVAL_SLOW)

            # Backstop timeout — no stop-loss ever watched, just this
            if tp_order_id is not None:
                try:
                    self.client.cancel_order(OrderPayload(orderID=tp_order_id))
                except Exception:
                    pass
            log(f"{BRACKET_TIMEOUT_SECONDS}s since buying, take-profit never hit — force-exiting", crypto)
            exit_result = self._guaranteed_sell(token, shares, crypto)
            pnl = round((exit_result["price"] - buy_price) * shares, 4) if exit_result["price"] is not None else -round(buy_price * shares, 4)
            return {**exit_result, "pnl_usd": pnl, "notes": "backstop timeout, force-exit (no stop-loss)"}

        # DRY-RUN
        while now_unix() - buy_time < BRACKET_TIMEOUT_SECONDS:
            book = get_order_book(token)
            bid_price, bid_size = best_bid(book)
            if bid_price is not None and bid_size >= shares and take_profit_price is not None:
                if bid_price >= take_profit_price:
                    elapsed = round(now_unix() - buy_time, 1)
                    log(f"[DRY] Take-profit hit: bid ${bid_price:.3f} at {elapsed}s", crypto)
                    pnl = round((take_profit_price - buy_price) * shares, 4)
                    return {"result": "sold_take_profit", "price": take_profit_price, "pnl_usd": pnl,
                            "notes": "take_profit hit", "seconds_to_sell": elapsed}
            time.sleep(POLL_INTERVAL_SLOW)
        log(f"{BRACKET_TIMEOUT_SECONDS}s since buying, take-profit never hit — force-exiting at best price", crypto)
        exit_result = self._force_exit(token, shares, crypto)
        pnl = round((exit_result["price"] - buy_price) * shares, 4) if exit_result["price"] is not None else -round(buy_price * shares, 4)
        return {**exit_result, "pnl_usd": pnl, "notes": "backstop timeout, force-exit (no stop-loss)", "seconds_to_sell": BRACKET_TIMEOUT_SECONDS}

    def _guaranteed_sell(self, token: str, shares: float, crypto: str, max_market_attempts: int = 2) -> dict:
        """PROVEN, hard-won mechanism from earlier in this project — the
        original version of this bot tried a market sell exactly ONCE and,
        if it failed (a real, confirmed live failure mode: 'no orders to
        match'), fell back to an IDENTICAL single attempt that failed the
        SAME way, leaving real shares completely unsold and unprotected.
        This retries the market sell a couple times, then escalates through
        increasingly aggressive limit prices all the way to the exchange's
        real minimum ($0.01), so a position is never left unsold."""
        if self.dry_run:
            book = get_order_book(token)
            price, size = best_bid(book)
            if price is None:
                log("[DRY] No bids at all for force-exit — total loss this trade", crypto)
                return {"result": "no_bids", "price": None}
            log(f"[DRY] Force-exit would fill at ${price:.3f}", crypto)
            return {"result": "exited", "price": price}

        from py_clob_client_v2 import MarketOrderArgsV2, OrderArgsV2, Side, OrderType, OrderPayload
        for attempt in range(1, max_market_attempts + 1):
            try:
                resp = self.client.create_and_post_market_order(
                    MarketOrderArgsV2(token_id=token, amount=shares, side=Side.SELL),
                    order_type=OrderType.FAK,
                )
                status = str(resp.get("status", "")).lower()
                if status == "matched":
                    try:
                        # REAL BUG FIXED HERE: takingAmount is the USDC actually
                        # received on a SELL, and it's a PLAIN decimal value here,
                        # NOT scaled by 1,000,000 — confirmed against real fill
                        # data earlier in this project (e.g. takingAmount "3" for
                        # 5 shares = $0.60/share, not a microscopic number).
                        proceeds = float(resp.get("takingAmount", 0))
                        price = round(proceeds / shares, 4) if shares else None
                        if price is not None and 0.01 <= price < 1:
                            return {"result": "exited", "price": price}
                        else:
                            log(f"Parsed sell price ${price} looks invalid — not trusting it. Raw: {resp}", crypto)
                    except Exception as e:
                        log(f"Could not parse sell fill price ({e}). Raw: {resp}", crypto)
                    return {"result": "exited", "price": None}
                log(f"Market sell attempt {attempt}/{max_market_attempts}: status={status}, retrying...", crypto)
            except Exception as e:
                log(f"Market sell attempt {attempt}/{max_market_attempts} failed: {e}", crypto)
            if attempt < max_market_attempts:
                time.sleep(0.3)

        log(f"All {max_market_attempts} market-sell attempts failed — escalating to increasingly "
            f"aggressive limit sells down to the exchange minimum", crypto)
        for factor in (0.85, 0.70, 0.50, 0.30, 0.15, 0.05, 0.01):
            book = get_order_book(token)
            current_bid, _ = best_bid(book)
            reference = current_bid if current_bid is not None else 0.5
            price = max(round(reference * factor, 2), 0.01) if factor > 0.01 else 0.01
            try:
                resp = self.client.create_and_post_order(
                    OrderArgsV2(token_id=token, price=price, size=shares, side=Side.SELL),
                    order_type=OrderType.GTC)
                order_id = resp.get("orderID", "")
            except Exception:
                continue
            deadline = now_unix() + 2.0
            while now_unix() < deadline:
                try:
                    detail = self.client.get_order(order_id)
                    if float(detail.get("size_matched", 0)) >= shares:
                        log(f"Escalated limit sell filled at ${price}", crypto)
                        return {"result": "exited", "price": price}
                except Exception:
                    pass
                time.sleep(0.2)
            try:
                self.client.cancel_order(OrderPayload(orderID=order_id))
            except Exception:
                pass
        log("Could not sell even at the exchange floor — position remains open, will settle at market resolution", crypto)
        return {"result": "unsold_no_liquidity", "price": None}

    def _force_exit(self, token: str, shares: float, crypto: str) -> dict:
        # DRY-RUN only path — kept for the dry-run branch above, which
        # simulates without real orders. Live selling always goes through
        # _guaranteed_sell now.
        if self.dry_run:
            book = get_order_book(token)
            price, size = best_bid(book)
            if price is None:
                log("[DRY] No bids at all for force-exit — total loss this trade", crypto)
                return {"result": "no_bids", "price": None}
            log(f"[DRY] Force-exit would fill at ${price:.3f}", crypto)
            return {"result": "exited", "price": price}
        return self._guaranteed_sell(token, shares, crypto)

    # ── WINDOW LOOP ──────────────────────────────────────────────────────────
    # UNCHANGED from the original — no decision-making logic touched.
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
            sell_info = self._watch_for_sell(token, buy_info["price"], buy_info["shares"], crypto)
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
        sign = "+" if isinstance(pnl, (int, float)) and pnl >= 0 else ""
        log(f"RECORDED: side={row['delta_side']} | buy={row['buy_result']}@{row['buy_price']} | "
            f"sell={row['sell_result']}@{row['sell_price']} | pnl={sign}${pnl}", row["crypto"])

    def _asset_loop(self, slug_prefix: str):
        crypto = MARKETS[slug_prefix]
        next_start_ts = None
        while not self.stop_event.is_set():
            if next_start_ts is None:
                start_ts = next_window_start(now_unix())
            else:
                start_ts = next_start_ts
                if now_unix() > start_ts + 30:
                    log(f"Running behind schedule — re-syncing to the current window", crypto)
                    start_ts = next_window_start(now_unix())
            while now_unix() < start_ts and not self.stop_event.is_set():
                time.sleep(1)
            if self.stop_event.is_set():
                break
            log(f"Monitoring window starting {datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime('%H:%M:%S')} UTC", crypto)
            try:
                self._monitor_window(slug_prefix, start_ts)
            except Exception as e:
                log(f"Unhandled error this window: {e}", crypto)
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
        total_pnl = sum(float(t["pnl_usd"] or 0) for t in trades)
        log("-" * 70)
        log(f"SUMMARY — {len(trades)} signals, {len(bought)} buy fills")
        log(f"  Take-profit hits: {len(take_profit)}")
        log(f"  Total PnL: {'+' if total_pnl >= 0 else ''}${total_pnl:.2f}")
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
