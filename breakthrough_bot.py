#!/usr/bin/env python3
"""
Polymarket BTC 1-Hour Momentum Scalper
========================================
Built directly on top of the proven 5-minute momentum scalper — same core
signal (delta-from-price-to-beat), same proven buy/sell mechanics (balance
verification, guaranteed-exit escalation, price clamping) — adapted for
1-hour BTC Up/Down windows instead of 5-minute ones.

WHY 1-HOUR, AND WHAT CHANGES:
  The 5-min bot's core problem was never the signal — it was that a wrong
  entry had almost no time to recover before the window forced resolution,
  which is exactly why a stop-loss existed (and why its slippage was such a
  problem). A 1-hour window gives a wrong entry much more room to still
  reach a small target before time runs out, so:
    - NO stop-loss. The risk control here is time and a tight target, not
      cutting losses reactively.
    - MUCH tighter take-profit (+$0.02/share, not +$0.15) — over an hour,
      a small favorable wiggle is far more likely to occur than over 5 min.
    - ONE position at a time — no new entry until the current one's target
      has been hit (or the window forces a backstop exit).
    - A session profit target: once cumulative profit for the CURRENT
      window reaches TARGET_PROFIT_PER_WINDOW, stop opening new positions
      for the rest of that window — the stated goal is $5-10/window, not
      squeezing out one trade too many.
    - No new entries in the final LATE_WINDOW_CUTOFF_MIN minutes — not
      enough time left for even a small move to develop and get sold.

WHAT STILL NEEDS LIVE VERIFICATION — READ BEFORE RUNNING LIVE:
  The 1-hour market's exact slug format is the one real unknown here. 5m,
  15m, and 4h markets are confirmed to use a clean, deterministic
  "btc-updown-{interval}-{unix_timestamp}" pattern (verified against real,
  live Polymarket URLs). Public reporting flags 1h specifically as having
  used an alternate, Eastern-Time-based slug at some point. This code uses
  the clean deterministic pattern ("btc-updown-1h-{ts}"), consistent with
  every other confirmed interval — but if the very first live run can't
  find a market, THIS is the first thing to check and adjust.

Usage:
  python hourly_bot.py --dry-run
  python hourly_bot.py --live --amount 10
"""
import time
import json
import csv
import argparse
import threading
import os
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
load_dotenv()

# ─── CONFIG ──────────────────────────────────────────────────────────────────
GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"
BINANCE_API = "https://api.binance.com"
SYMBOLS = {"BTC": "BTCUSDT"}
MARKETS = {"btc-updown-1h": "BTC"}

# ─── CTF (Conditional Token Framework) CONSTANTS — for the split mode ───────
# Verified against Polymarket's official documentation
# (github.com/Polymarket/agent-skills/blob/main/ctf-operations.md) — split is
# a DIRECT SMART CONTRACT CALL, not a py_clob_client_v2 / CLOB API method.
# Reused unchanged from the hedge bot, where this was already verified.
POLYGON_RPC = "https://polygon-rpc.com"
CTF_CONTRACT_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PARENT_COLLECTION_ID = "0x" + "00" * 32
BINARY_PARTITION = [1, 2]

CTF_ABI = [
    {
        "name": "splitPosition", "type": "function", "stateMutability": "nonpayable",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "partition", "type": "uint256[]"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
    },
]
ERC20_ABI = [
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]

WINDOW_SECONDS = 3600            # 1 hour, vs the original bot's 300 (5 min)
BINANCE_KLINE_INTERVAL = "1h"    # matches WINDOW_SECONDS

MIN_DELTA_PCT_TO_TRUST = 0.01    # same validated starting point as the 5-min bot — filters pure noise
BUY_CEILING_BUFFER = 0.02        # willing to pay up to (observed price + this) — same as the 5-min bot;
                                    # no reason to cap entry price, the dominant/settled side is exactly
                                    # where we want to be, even at a high price
THIN_MARKET_BUY_BUFFER = 0.05    # wider buffer used specifically when pricing off the best BID instead
                                    # of an ask (no ask currently resting) — a real spread gap needs a
                                    # more generous offer to have a realistic chance of actually filling
BUY_TIMEOUT_SEC = 2.0

PROFIT_MARGIN = 0.02             # take-profit target — MUCH tighter than the 5-min bot's 0.15, since an
                                    # hour gives far more time for even a small favorable wiggle to occur
# NO STOP-LOSS. Time itself is the risk control here, not a reactive cut.
BACKSTOP_SECONDS = 600           # ultimate backstop only — if take-profit never hits this long after
                                    # buying, force-exit at best available price. 10 minutes, not 60-80s
                                    # like the 5-min bot, since this window has vastly more time to spare
                                    # and a premature exit here defeats the whole point of using 1h markets

# ─── THREE ENTRY ZONES BY |delta from price-to-beat| ────────────────────────
# Starting thresholds based directly on the examples given (a $50-150 lean
# being a real but non-extreme settlement, $300+ being effectively decided) —
# not independently validated numbers, meant to be tuned against real data
# like every other threshold in this project.
SPLIT_ZONE_MIN = 50.0      # below this: too close to a coin-flip to split — single-sided momentum only
SPLIT_ZONE_MAX = 300.0     # above this: market has essentially decided — split the losing side would
                              # sit there forever, single-sided dominant-side only

LATE_WINDOW_CUTOFF_MIN = 10      # in the final N minutes: NOT a ban — just switch to a brief observation
                                    # window before entering, more caution rather than no entries at all
LATE_WINDOW_OBSERVATION_SEC = 5.0  # how long to watch the delta hold steady before entering late in the window

TARGET_PROFIT_PER_WINDOW = 5.0   # once cumulative profit THIS window reaches this, stop opening new
                                    # positions for the rest of the window, REGARDLESS of time remaining —
                                    # this check has top priority over everything else below it

MONITOR_INTERVAL = 1.0           # how often to check for a new entry opportunity
POLL_INTERVAL_SLOW = 0.5         # how often to poll the take-profit order once a position is open

# ─── UTILITIES (proven patterns, reused from the 5-min bot) ─────────────────
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
    """Fetches the real 'price to beat' for a 1-hour window — BTC's price
    at the moment this window opened, using the matching 1h Binance candle."""
    try:
        r = requests.get(
            f"{BINANCE_API}/api/v3/klines",
            params={"symbol": symbol, "interval": BINANCE_KLINE_INTERVAL,
                     "startTime": window_ts * 1000, "limit": 1},
            timeout=3,
        )
        r.raise_for_status()
        candles = r.json()
        return float(candles[0][1]) if candles else None
    except Exception:
        return None

def _et_slug_candidates(start_ts: int):
    """1-hour markets use an Eastern-Time, human-readable slug — confirmed
    different from the clean 'btc-updown-1h-{ts}' pattern that works for
    5m/15m/4h markets. Generates BOTH known variants (with and without
    year), since Polymarket has shipped both — this could not be verified
    against the live API from this environment (sandboxed, gamma-api is
    network-blocked here), so this is built from real, confirmed Polymarket
    URLs found in research, not an executed test. The first live run is
    what actually confirms this."""
    dt_utc = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    dt_et = dt_utc.astimezone(ZoneInfo("America/New_York"))
    month = dt_et.strftime("%B").lower()
    day = dt_et.day
    hour_12 = dt_et.strftime("%I").lstrip("0") or "12"
    ampm = dt_et.strftime("%p").lower()
    return [
        f"bitcoin-up-or-down-{month}-{day}-{hour_12}{ampm}-et",
        f"bitcoin-up-or-down-{month}-{day}-{dt_et.year}-{hour_12}{ampm}-et",
    ]

def get_window_market(slug_prefix: str, start_ts: int):
    # Try the clean deterministic pattern first (works for 5m/15m/4h,
    # unconfirmed for 1h specifically), then fall back to the ET-based
    # human slug in both known year variants.
    candidates = [f"{slug_prefix}-{start_ts}"] + _et_slug_candidates(start_ts)
    event = None
    matched_slug = None
    for slug in candidates:
        try:
            r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=3)
            r.raise_for_status()
            data = r.json()
            if data:
                event = data[0]
                matched_slug = slug
                break
        except Exception:
            continue
    if event is None:
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
        "slug": matched_slug, "crypto": MARKETS[slug_prefix], "start_ts": start_ts, "close_ts": start_ts + WINDOW_SECONDS,
        "down_token": tokens["Down"], "up_token": tokens["Up"],
        "condition_id": market.get("conditionId", ""), "title": event.get("title", ""),
    }

def get_order_book(token_id: str) -> dict:
    try:
        r = requests.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=2)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        # REAL BUG FIXED HERE: this was silently swallowing every failure,
        # making a broken token ID look identical to a genuinely empty book.
        # Confirmed live: this exact pattern hid a real problem behind a
        # misleading "no liquidity" message for 30+ minutes straight.
        log(f"get_order_book failed for token {token_id[:20]}...: {e}")
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
    return int((now // WINDOW_SECONDS) + 1) * WINDOW_SECONDS

def get_reference_price(token_id: str):
    """Returns a usable reference price for a token: the real ask if one
    exists, otherwise falls back to the best bid — a thin market can have
    bids with no resting asks at a given moment, which isn't the same as
    having no liquidity at all. Returns (price, is_ask) or (None, None) if
    genuinely nothing is available on either side."""
    book = get_order_book(token_id)
    ask, _ = best_ask(book)
    if ask is not None:
        return ask, True
    bid, _ = best_bid(book)
    if bid is not None:
        return bid, False
    return None, None

# ─── PERSISTENT CSV LOG ──────────────────────────────────────────────────────
CSV_FIELDS = [
    "timestamp", "bot_name", "mode", "crypto", "slug", "trade_num_this_window",
    "delta_side", "delta_value", "delta_pct", "minutes_left_in_window",
    "buy_result", "buy_price", "buy_shares", "spread_at_buy",
    "sell_result", "sell_price", "seconds_to_sell", "pnl_usd",
    "cumulative_pnl_this_window", "notes",
]

class TradeLogger:
    def __init__(self, bot_name: str):
        self.path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hourly_trades_log.csv")
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
class HourlyBot:
    def __init__(self, dry_run: bool, amount: float):
        self.dry_run  = dry_run
        self.amount   = amount
        self.bot_name = os.getenv("BOT_NAME", "hourly_bot")
        self.mode_str = "dry_run" if dry_run else "live"
        self.stop_event = threading.Event()
        self.trades = []
        self.trades_lock = threading.Lock()
        self.logger = TradeLogger(self.bot_name)
        self.client = None
        if not dry_run:
            self._init_client()

        log("=" * 70)
        log(f"BTC 1-Hour Momentum Scalper | {self.mode_str.upper()} | ${amount:.2f}/trade | bot_name={self.bot_name}")
        log(f"Direction: delta-from-price-to-beat only (min {MIN_DELTA_PCT_TO_TRUST}% to trust) — "
            f"same signal as the 5-min bot, always betting the leaning/dominant side")
        log(f"Buy: observed price + ${BUY_CEILING_BUFFER} buffer (no fixed ceiling) | timeout {BUY_TIMEOUT_SEC}s")
        log(f"Sell: take-profit entry+${PROFIT_MARGIN} | NO stop-loss | backstop {BACKSTOP_SECONDS}s "
            f"({BACKSTOP_SECONDS/60:.0f} min)")
        log(f"ONE position at a time | last {LATE_WINDOW_CUTOFF_MIN} min: extra caution "
            f"({LATE_WINDOW_OBSERVATION_SEC}s observation before entering, not a ban)")
        log(f"Zones by |delta|: <${SPLIT_ZONE_MIN} or >=${SPLIT_ZONE_MAX} -> single-sided dominant side | "
            f"${SPLIT_ZONE_MIN}-${SPLIT_ZONE_MAX} -> SPLIT both sides")
        log(f"Session target: stop opening new positions once cumulative profit this window reaches "
            f"+${TARGET_PROFIT_PER_WINDOW}")
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
        # Split is a direct smart-contract call, needed for the split-zone
        # entries — reused verbatim from the hedge bot, where this was
        # already verified against Polymarket's official documentation.
        from web3 import Web3
        self.w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
        self.wallet_address = Web3.to_checksum_address(os.environ["POLY_PROXY_WALLET"])
        self.ctf_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_CONTRACT_ADDRESS), abi=CTF_ABI)
        self.usdc_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(USDC_E_ADDRESS), abi=ERC20_ABI)
        self._ensure_ctf_approval()

    def _ensure_ctf_approval(self):
        """Checks the real on-chain USDC.e allowance for the CTF contract
        first, only sending an approval transaction if actually needed."""
        from web3 import Web3
        try:
            current_allowance = self.usdc_contract.functions.allowance(
                self.wallet_address, Web3.to_checksum_address(CTF_CONTRACT_ADDRESS)
            ).call()
            if current_allowance > 10**12:
                log("CTF contract already approved to spend USDC.e — skipping approval tx")
                return
            log("CTF contract not yet approved for USDC.e — sending approval transaction "
                "(one-time, small gas cost on Polygon)")
            max_uint = 2**256 - 1
            approve_tx = self.usdc_contract.functions.approve(
                Web3.to_checksum_address(CTF_CONTRACT_ADDRESS), max_uint
            ).build_transaction({
                "from": self.wallet_address,
                "nonce": self.w3.eth.get_transaction_count(self.wallet_address),
                "gas": 100000,
                "gasPrice": self.w3.eth.gas_price,
            })
            signed = self.w3.eth.account.sign_transaction(approve_tx, private_key=os.environ["POLY_PRIVATE_KEY"])
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            log(f"Approval tx sent: {tx_hash.hex()} — waiting for confirmation...")
            self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            log("CTF approval confirmed")
        except Exception as e:
            log(f"Could not verify/send CTF approval ({e}) — split entries will likely fail until this is resolved")

    def _split_position(self, condition_id: str, amount: float) -> dict:
        """Mints `amount` shares of BOTH Up and Down via the CTF split
        operation — reused verbatim from the hedge bot. Fixed conversion:
        $1 always mints 1 Up share + 1 Down share, regardless of current odds."""
        if self.dry_run:
            return {"result": "split", "shares": amount}
        try:
            from web3 import Web3
            amount_units = int(round(amount * 1_000_000))
            condition_id_bytes = bytes.fromhex(condition_id.replace("0x", ""))
            tx = self.ctf_contract.functions.splitPosition(
                Web3.to_checksum_address(USDC_E_ADDRESS),
                bytes.fromhex(PARENT_COLLECTION_ID.replace("0x", "")),
                condition_id_bytes,
                BINARY_PARTITION,
                amount_units,
            ).build_transaction({
                "from": self.wallet_address,
                "nonce": self.w3.eth.get_transaction_count(self.wallet_address),
                "gas": 300000,
                "gasPrice": self.w3.eth.gas_price,
            })
            signed = self.w3.eth.account.sign_transaction(tx, private_key=os.environ["POLY_PRIVATE_KEY"])
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            if receipt.status != 1:
                log(f"Split transaction reverted: {tx_hash.hex()}")
                return {"result": "error", "shares": 0}
            log(f"Split confirmed: {tx_hash.hex()}")
            return {"result": "split", "shares": amount}
        except Exception as e:
            log(f"Split failed: {e}")
            return {"result": "error", "shares": 0}

    # ── BUY (proven pattern, reused from the 5-min bot unchanged) ───────────
    def _attempt_buy(self, token: str, observed_price: float, crypto: str, buffer_override: float = None) -> dict:
        buffer_to_use = buffer_override if buffer_override is not None else BUY_CEILING_BUFFER
        ceiling = round(observed_price + buffer_to_use, 4)
        MIN_SHARES = 5  # confirmed real exchange minimum. At $10/trade, this never actually
                          # binds above the intended spend (5 shares costs at most $4.95, since
                          # price is always < $1) — unlike the 5-min bot's $2 trades, no conflict here.
        if self.dry_run:
            book = get_order_book(token)
            price, size = best_ask(book)
            if price is not None and price <= ceiling:
                shares = max(MIN_SHARES, round(self.amount / price))
                log(f"[DRY] BUY would fill: ask ${price:.3f} (size {size})", crypto)
                return {"result": "bought", "price": price, "shares": shares}
            log(f"[DRY] BUY missed: no ask <= ${ceiling}", crypto)
            return {"result": "missed", "price": None, "shares": 0}

        from py_clob_client_v2 import OrderArgsV2, Side, OrderType, OrderPayload, AssetType, BalanceAllowanceParams
        size = max(MIN_SHARES, round(self.amount / ceiling))

        balance_before = 0.0
        try:
            bal_resp_before = self.client.get_balance_allowance(BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL, token_id=token,
                signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "3")),
            ))
            balance_before = float(bal_resp_before.get("balance", 0)) / 1_000_000
        except Exception as e:
            log(f"Could not check balance before buying ({e}) — proceeding", crypto)

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
                current_size = float(detail.get("size_matched", 0))
                if current_size > last_known_size:
                    last_known_size = current_size
            except Exception:
                pass
            time.sleep(0.25)
        try:
            self.client.cancel_order(OrderPayload(orderID=order_id))
        except Exception:
            pass

        # Same balance-delta reconciliation fix proven on the original bot —
        # catches both understated fills AND stale-balance contamination
        # from a prior trade, never trusting more than what was intended.
        final_shares = last_known_size
        try:
            bal_resp_after = self.client.get_balance_allowance(BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL, token_id=token,
                signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "3")),
            ))
            real_balance_after = float(bal_resp_after.get("balance", 0)) / 1_000_000
            delta = round(real_balance_after - balance_before, 4)
            if delta > final_shares:
                final_shares = min(delta, size)
        except Exception as e:
            log(f"Balance verification failed ({e}) — proceeding with tracked fill amount", crypto)

        if final_shares <= 0:
            log(f"BUY timed out with no confirmed fill after {BUY_TIMEOUT_SEC}s", crypto)
            return {"result": "missed", "price": None, "shares": 0}
        log(f"BUY confirmed: {final_shares} shares at ceiling ${ceiling}", crypto)
        return {"result": "bought", "price": ceiling, "shares": final_shares}

    # ── SELL: take-profit + time backstop, NO stop-loss ─────────────────────
    def _watch_for_sell(self, token: str, buy_price: float, raw_shares: float, crypto: str) -> dict:
        shares = int(raw_shares)
        if shares != raw_shares:
            log(f"Buy partially filled: held {raw_shares}, flooring to {shares} whole shares", crypto)
        if shares < 1:
            log("Partial fill left less than 1 whole share — forcing immediate exit", crypto)
            exit_result = self._guaranteed_sell(token, raw_shares, crypto)
            pnl = -round(buy_price * raw_shares, 4) if exit_result["price"] is None else round((exit_result["price"] - buy_price) * raw_shares, 4)
            return {**exit_result, "pnl_usd": pnl, "notes": "sub-1-share partial fill"}

        # Same price-clamping fix proven on the 5-min bot — Polymarket only
        # accepts prices $0.01-$0.99, never place an order outside that.
        take_profit_price = min(round(buy_price + PROFIT_MARGIN, 4), 0.99)
        if take_profit_price <= buy_price:
            log(f"Buy price ${buy_price} leaves no room for a take-profit — will force-exit at the backstop", crypto)
            take_profit_price = None

        log(f"Take-profit: {'$'+str(take_profit_price) if take_profit_price else 'N/A (no room)'} "
            f"(+${PROFIT_MARGIN}) | backstop {BACKSTOP_SECONDS}s | NO stop-loss", crypto)
        buy_time = now_unix()

        if not self.dry_run:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams
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
                except Exception:
                    pass
                time.sleep(0.5)
            if not balance_confirmed:
                log("Balance still not settled after 5s — proceeding anyway, take-profit placement may fail", crypto)

            from py_clob_client_v2 import OrderArgsV2, Side, OrderType, OrderPayload
            tp_order_id = None
            if take_profit_price is not None:
                try:
                    tp_resp = self.client.create_and_post_order(
                        OrderArgsV2(token_id=token, price=take_profit_price, size=shares, side=Side.SELL),
                        order_type=OrderType.GTC,
                    )
                    tp_order_id = tp_resp.get("orderID", "")
                    log(f"Take-profit resting order placed at ${take_profit_price}", crypto)
                except Exception as e:
                    log(f"Could not place take-profit order ({e}) — forcing exit immediately", crypto)
                    exit_result = self._guaranteed_sell(token, shares, crypto)
                    pnl = round((exit_result["price"] - buy_price) * shares, 4) if exit_result["price"] is not None else -round(buy_price * shares, 4)
                    return {**exit_result, "pnl_usd": pnl, "notes": "take-profit placement failed"}

            deadline = buy_time + BACKSTOP_SECONDS
            while now_unix() < deadline:
                if self.stop_event.is_set():
                    # Bot is being stopped — still resolve THIS position cleanly rather than abandon it
                    pass
                if tp_order_id is not None:
                    try:
                        detail = self.client.get_order(tp_order_id)
                        filled = float(detail.get("size_matched", 0))
                        if filled >= shares:
                            pnl = round((take_profit_price - buy_price) * shares, 4)
                            return {"result": "sold_take_profit", "price": take_profit_price, "pnl_usd": pnl,
                                    "notes": "take_profit hit"}
                    except Exception:
                        pass
                time.sleep(POLL_INTERVAL_SLOW)

            if tp_order_id is not None:
                try:
                    self.client.cancel_order(OrderPayload(orderID=tp_order_id))
                except Exception:
                    pass
            log(f"{BACKSTOP_SECONDS}s since buying, take-profit never hit — force-exiting", crypto)
            exit_result = self._guaranteed_sell(token, shares, crypto)
            pnl = round((exit_result["price"] - buy_price) * shares, 4) if exit_result["price"] is not None else -round(buy_price * shares, 4)
            return {**exit_result, "pnl_usd": pnl, "notes": "backstop timeout, force-exit (no stop-loss)"}

        # DRY-RUN
        while now_unix() - buy_time < BACKSTOP_SECONDS:
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
        log(f"{BACKSTOP_SECONDS}s since buying, take-profit never hit — force-exiting at best price", crypto)
        exit_result = self._guaranteed_sell(token, shares, crypto)
        pnl = round((exit_result["price"] - buy_price) * shares, 4) if exit_result["price"] is not None else -round(buy_price * shares, 4)
        return {**exit_result, "pnl_usd": pnl, "notes": "backstop timeout, force-exit (no stop-loss)", "seconds_to_sell": BACKSTOP_SECONDS}

    def _guaranteed_sell(self, token: str, shares: float, crypto: str, max_market_attempts: int = 2) -> dict:
        """Proven, hard-won mechanism from the 5-min bot — retries a market
        sell a couple times, then escalates through increasingly aggressive
        limit prices all the way to the exchange's real minimum ($0.01), so a
        position is never left unsold and unprotected."""
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
                        proceeds = float(resp.get("takingAmount", 0))  # plain USDC value, not scaled
                        price = round(proceeds / shares, 4) if shares else None
                        if price is not None and 0.01 <= price < 1:
                            return {"result": "exited", "price": price}
                    except Exception:
                        pass
                    return {"result": "exited", "price": None}
                log(f"Market sell attempt {attempt}/{max_market_attempts}: status={status}, retrying...", crypto)
            except Exception as e:
                log(f"Market sell attempt {attempt}/{max_market_attempts} failed: {e}", crypto)
            if attempt < max_market_attempts:
                time.sleep(0.3)

        log("All market-sell attempts failed — escalating through aggressive limit sells to the exchange minimum", crypto)
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

    # ── SPLIT ENTRY (moderate-lean zone only) ────────────────────────────────
    def _enter_split(self, market: dict, condition_id: str, down_ask: float, up_ask: float,
                       close_ts: float, crypto: str) -> dict:
        """Buys BOTH sides via split and places a take-profit sell on each —
        used only in the moderate-lean zone, where either side still has a
        real chance of a small favorable wiggle. No stop-loss here either —
        same time-based backstop as the single-sided path."""
        split_result = self._split_position(condition_id, self.amount)
        if split_result["result"] != "split":
            return {"outcome": "split_failed", "pnl_usd": 0, "notes": "split transaction failed"}
        shares = split_result["shares"]
        down_target = round(down_ask + PROFIT_MARGIN, 4)
        up_target = round(up_ask + PROFIT_MARGIN, 4)
        log(f"Split entered: {shares} Down @ ${down_ask} (target ${down_target}) | "
            f"{shares} Up @ ${up_ask} (target ${up_target})", crypto)

        down_sold = up_sold = False
        down_exit = up_exit = None

        if not self.dry_run:
            from py_clob_client_v2 import OrderArgsV2, Side, OrderType, OrderPayload
            try:
                down_resp = self.client.create_and_post_order(
                    OrderArgsV2(token_id=market["down_token"], price=down_target, size=shares, side=Side.SELL),
                    order_type=OrderType.GTC)
                down_order_id = down_resp.get("orderID", "")
            except Exception as e:
                log(f"Could not place Down sell: {e}", crypto)
                down_order_id = None
            try:
                up_resp = self.client.create_and_post_order(
                    OrderArgsV2(token_id=market["up_token"], price=up_target, size=shares, side=Side.SELL),
                    order_type=OrderType.GTC)
                up_order_id = up_resp.get("orderID", "")
            except Exception as e:
                log(f"Could not place Up sell: {e}", crypto)
                up_order_id = None

        entry_time = now_unix()
        backstop_deadline = min(close_ts, entry_time + BACKSTOP_SECONDS)
        while now_unix() < backstop_deadline and not (down_sold and up_sold):
            book_down = get_order_book(market["down_token"])
            book_up = get_order_book(market["up_token"])
            down_bid, down_size = best_bid(book_down)
            up_bid, up_size = best_bid(book_up)

            if self.dry_run:
                if not down_sold and down_bid is not None and down_bid >= down_target and down_size >= shares:
                    down_sold, down_exit = True, down_target
                if not up_sold and up_bid is not None and up_bid >= up_target and up_size >= shares:
                    up_sold, up_exit = True, up_target
            else:
                if not down_sold and down_order_id:
                    try:
                        detail = self.client.get_order(down_order_id)
                        if float(detail.get("size_matched", 0)) >= shares:
                            down_sold, down_exit = True, down_target
                    except Exception:
                        pass
                if not up_sold and up_order_id:
                    try:
                        detail = self.client.get_order(up_order_id)
                        if float(detail.get("size_matched", 0)) >= shares:
                            up_sold, up_exit = True, up_target
                    except Exception:
                        pass
            if not (down_sold and up_sold):
                time.sleep(POLL_INTERVAL_SLOW)

        if not self.dry_run:
            from py_clob_client_v2 import OrderPayload
            if not down_sold and down_order_id:
                try:
                    self.client.cancel_order(OrderPayload(orderID=down_order_id))
                except Exception:
                    pass
            if not up_sold and up_order_id:
                try:
                    self.client.cancel_order(OrderPayload(orderID=up_order_id))
                except Exception:
                    pass

        total_cost = shares * down_ask + shares * up_ask

        if down_sold and up_sold:
            proceeds = shares * down_exit + shares * up_exit
            pnl = round(proceeds - total_cost, 4)
            return {"outcome": "both_hit", "pnl_usd": pnl, "notes": "both legs hit target"}

        # Neither, or exactly one, sold — resolve the unsold leg(s) if the
        # window has actually closed by now; otherwise force-exit them at
        # best available price using the same guaranteed-sell escalation.
        if now_unix() >= close_ts:
            symbol = SYMBOLS.get(crypto)
            final_price = get_binance_price(symbol)
            window_open = get_window_open_price(symbol, market["start_ts"])
            up_won = (final_price is not None and window_open is not None and final_price > window_open)
            if not down_sold:
                down_exit = 0.0 if up_won else 1.0
            if not up_sold:
                up_exit = 1.0 if up_won else 0.0
            proceeds = shares * down_exit + shares * up_exit
            pnl = round(proceeds - total_cost, 4)
            return {"outcome": "resolved_at_close", "pnl_usd": pnl, "notes": "window closed, resolved at settlement"}

        # Backstop hit before window close — force-exit whichever leg(s) never sold
        if not down_sold:
            exit_result = self._guaranteed_sell(market["down_token"], shares, crypto)
            down_exit = exit_result["price"] if exit_result["price"] is not None else 0.0
        if not up_sold:
            exit_result = self._guaranteed_sell(market["up_token"], shares, crypto)
            up_exit = exit_result["price"] if exit_result["price"] is not None else 0.0
        proceeds = shares * down_exit + shares * up_exit
        pnl = round(proceeds - total_cost, 4)
        return {"outcome": "backstop_force_exit", "pnl_usd": pnl, "notes": "backstop timeout, force-exited unsold leg(s)"}

    # ── WINDOW LOOP ──────────────────────────────────────────────────────────
    def _monitor_window(self, slug_prefix: str, start_ts: int):
        crypto = MARKETS[slug_prefix]
        close_ts = start_ts + WINDOW_SECONDS
        symbol = SYMBOLS.get(crypto)
        market = None
        find_deadline = now_unix() + 5
        while now_unix() < find_deadline:
            market = get_window_market(slug_prefix, start_ts)
            if market:
                break
            time.sleep(0.5)
        if not market:
            log(f"Could not find market for window starting {start_ts} — skipping entire window "
                f"(if this happens every window, the 1h slug format needs checking)", crypto)
            return

        window_open_price = get_window_open_price(symbol, start_ts) if symbol else None
        if not window_open_price:
            log("Could not fetch price-to-beat — skipping entire window", crypto)
            return
        log(f"Price to beat this window: ${window_open_price:,.2f}", crypto)

        # Diagnostic: confirm the tokens themselves actually have SOME order
        # book right at window start, and show the actual event title so a
        # wrong-market match is immediately visible, not just wrong token IDs.
        down_token_raw = market["down_token"]
        up_token_raw = market["up_token"]
        log(f"Market found: '{market.get('title', '(no title)')}' | slug={market['slug']}", crypto)
        log(f"Down token (len={len(down_token_raw)}): {down_token_raw} | "
            f"Up token (len={len(up_token_raw)}): {up_token_raw}", crypto)
        if not down_token_raw or not up_token_raw or len(down_token_raw) < 20 or len(up_token_raw) < 20:
            log("WARNING: a token ID looks malformed (empty or unusually short) — real Polymarket "
                "CLOB token IDs are long numeric strings. This is likely the actual problem, not "
                "a liquidity gap. The market match (title above) or the outcomes/clobTokenIds parsing "
                "may be wrong for this specific event.", crypto)

        down_check = get_order_book(down_token_raw)
        up_check = get_order_book(up_token_raw)
        down_check_ask, _ = best_ask(down_check)
        up_check_ask, _ = best_ask(up_check)
        down_check_bid, _ = best_bid(down_check)
        up_check_bid, _ = best_bid(up_check)
        log(f"Order book check: Down (ask={down_check_ask}, bid={down_check_bid}) | "
            f"Up (ask={up_check_ask}, bid={up_check_bid})", crypto)
        if down_check_ask is None and up_check_ask is None and down_check_bid is None and up_check_bid is None:
            log("Genuinely nothing on either side (no ask, no bid) for BOTH tokens right at window "
                "start — if this persists for many minutes, it's very likely a broken token ID or a "
                "wrong market match, not real market conditions. Check the token IDs and title above.", crypto)

        trades_this_window = 0
        cumulative_pnl_this_window = 0.0
        position_open = False  # ONE position at a time — gate on this, not a trade counter

        while now_unix() < close_ts:
            if self.stop_event.is_set():
                return

            # TOP PRIORITY, unconditional: once the session target is reached,
            # stop opening new positions regardless of time remaining.
            if cumulative_pnl_this_window >= TARGET_PROFIT_PER_WINDOW:
                log(f"Session target reached (+${cumulative_pnl_this_window:.2f} >= "
                    f"+${TARGET_PROFIT_PER_WINDOW}) — no more entries this window, regardless of time left", crypto)
                time.sleep(MONITOR_INTERVAL)
                continue

            if position_open:
                time.sleep(MONITOR_INTERVAL)
                continue

            minutes_left = (close_ts - now_unix()) / 60
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

            # Late in the window: not a ban, just more caution — require the
            # delta to hold the SAME direction for a short observation window
            # before entering, rather than reacting to a single instantaneous read.
            if minutes_left <= LATE_WINDOW_CUTOFF_MIN:
                log(f"{minutes_left:.0f} min left — observing {LATE_WINDOW_OBSERVATION_SEC}s before "
                    f"entering (extra caution this late)", crypto)
                obs_deadline = now_unix() + LATE_WINDOW_OBSERVATION_SEC
                confirmed = True
                while now_unix() < obs_deadline:
                    time.sleep(0.5)
                    check_price = get_binance_price(symbol)
                    if check_price is None:
                        continue
                    check_delta = check_price - window_open_price
                    check_side = "Up" if check_delta > 0 else "Down"
                    if check_side != delta_side:
                        log(f"Direction flipped during observation ({delta_side} -> {check_side}) — skipping this signal", crypto)
                        confirmed = False
                        break
                if not confirmed:
                    time.sleep(MONITOR_INTERVAL)
                    continue
                # Refresh the reading after observing, since real time has passed
                current_btc_price = get_binance_price(symbol) if symbol else current_btc_price
                if current_btc_price is not None:
                    delta_value = current_btc_price - window_open_price
                    delta_pct = abs(delta_value) / window_open_price * 100
                    delta_side = "Up" if delta_value > 0 else "Down"

            abs_delta = abs(delta_value)
            in_split_zone = SPLIT_ZONE_MIN <= abs_delta < SPLIT_ZONE_MAX

            if in_split_zone:
                down_ask, down_is_ask = get_reference_price(market["down_token"])
                up_ask, up_is_ask = get_reference_price(market["up_token"])
                if down_ask is None or up_ask is None:
                    log(f"No price available (neither ask nor bid) for one or both sides "
                        f"(Down={down_ask}, Up={up_ask}) — genuinely no liquidity at all right now, retrying", crypto)
                    time.sleep(MONITOR_INTERVAL)
                    continue
                trades_this_window += 1
                position_open = True
                ref_note = f"(Down priced off {'ask' if down_is_ask else 'bid (no ask available)'}, " \
                           f"Up priced off {'ask' if up_is_ask else 'bid (no ask available)'})"
                log(f"Delta {delta_value:+.2f} ({minutes_left:.0f} min left) is in the moderate-lean zone "
                    f"(${SPLIT_ZONE_MIN}-${SPLIT_ZONE_MAX}) -> SPLIT both sides {ref_note}", crypto)
                split_outcome = self._enter_split(market, market["condition_id"], down_ask, up_ask, close_ts, crypto)
                cumulative_pnl_this_window += float(split_outcome["pnl_usd"] or 0)
                row = {
                    "timestamp": ts_str(), "bot_name": self.bot_name, "mode": self.mode_str, "crypto": crypto,
                    "slug": market["slug"], "trade_num_this_window": trades_this_window,
                    "delta_side": "SPLIT", "delta_value": round(delta_value, 4), "delta_pct": round(delta_pct, 4),
                    "minutes_left_in_window": round(minutes_left, 1),
                    "buy_result": "split", "buy_price": "", "buy_shares": "",
                    "sell_result": split_outcome["outcome"], "sell_price": "",
                    "pnl_usd": split_outcome["pnl_usd"], "cumulative_pnl_this_window": round(cumulative_pnl_this_window, 4),
                    "notes": split_outcome["notes"],
                }
                self._record(row)
                position_open = False
                time.sleep(MONITOR_INTERVAL)
                continue

            # Single-sided: either the early/uncertain zone (delta < SPLIT_ZONE_MIN)
            # or the extreme/decided zone (delta >= SPLIT_ZONE_MAX) — always bet
            # the leaning/dominant side, never the side unlikely to move.
            token = market["up_token"] if delta_side == "Up" else market["down_token"]
            observed_price, is_ask = get_reference_price(token)
            if observed_price is None:
                log(f"No price available (neither ask nor bid) for {delta_side} token — genuinely no "
                    f"liquidity at all right now, retrying", crypto)
                time.sleep(MONITOR_INTERVAL)
                continue
            trades_this_window += 1
            position_open = True
            book = get_order_book(token)
            observed_bid, _ = best_bid(book)
            spread_at_buy = round(observed_price - observed_bid, 4) if observed_bid is not None else None
            ref_note = "ask" if is_ask else "bid (no ask currently resting)"

            zone_label = "extreme/decided" if abs_delta >= SPLIT_ZONE_MAX else "early/uncertain"
            log(f"Delta signal (trade {trades_this_window}, {minutes_left:.0f} min left, {zone_label} zone): "
                f"{delta_value:+.2f} ({delta_pct:.4f}%) -> buying {delta_side} @ ~${observed_price} "
                f"(priced off {ref_note}, spread: ${spread_at_buy})", crypto)

            buy_buffer = BUY_CEILING_BUFFER if is_ask else THIN_MARKET_BUY_BUFFER
            buy_info = self._attempt_buy(token, observed_price, crypto, buffer_override=buy_buffer)
            row = {
                "timestamp": ts_str(), "bot_name": self.bot_name, "mode": self.mode_str, "crypto": crypto,
                "slug": market["slug"], "trade_num_this_window": trades_this_window,
                "delta_side": delta_side, "delta_value": round(delta_value, 4), "delta_pct": round(delta_pct, 4),
                "minutes_left_in_window": round(minutes_left, 1),
                "buy_result": buy_info["result"], "buy_price": buy_info["price"], "buy_shares": buy_info["shares"],
                "spread_at_buy": spread_at_buy,
            }
            if buy_info["result"] != "bought":
                row.update({"sell_result": "n/a", "sell_price": "", "pnl_usd": 0,
                            "cumulative_pnl_this_window": round(cumulative_pnl_this_window, 4), "notes": "no buy fill"})
                self._record(row)
                position_open = False
                time.sleep(MONITOR_INTERVAL)
                continue

            sell_info = self._watch_for_sell(token, buy_info["price"], buy_info["shares"], crypto)
            cumulative_pnl_this_window += float(sell_info["pnl_usd"] or 0)
            row.update({
                "sell_result": sell_info["result"], "sell_price": sell_info["price"],
                "seconds_to_sell": sell_info.get("seconds_to_sell", ""),
                "pnl_usd": sell_info["pnl_usd"], "cumulative_pnl_this_window": round(cumulative_pnl_this_window, 4),
                "notes": sell_info["notes"],
            })
            self._record(row)
            position_open = False
            time.sleep(MONITOR_INTERVAL)

        log(f"Window closed. Trades this window: {trades_this_window} | "
            f"Cumulative pnl this window: {'+' if cumulative_pnl_this_window>=0 else ''}${cumulative_pnl_this_window:.2f}", crypto)

    def _record(self, row: dict):
        with self.trades_lock:
            self.trades.append(row)
        self.logger.write(row)
        pnl = row.get("pnl_usd", 0)
        sign = "+" if isinstance(pnl, (int, float)) and pnl >= 0 else ""
        log(f"RECORDED: side={row['delta_side']} | buy={row['buy_result']}@{row['buy_price']} | "
            f"sell={row['sell_result']}@{row['sell_price']} | pnl={sign}${pnl} | "
            f"window total={'+' if row['cumulative_pnl_this_window']>=0 else ''}${row['cumulative_pnl_this_window']}",
            row["crypto"])

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
            log(f"Monitoring window starting {datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC "
                f"(closes in {WINDOW_SECONDS/60:.0f} min)", crypto)
            try:
                self._monitor_window(slug_prefix, start_ts)
            except Exception as e:
                log(f"Unhandled error this window: {e}", crypto)
            next_start_ts = start_ts + WINDOW_SECONDS

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
        if not trades:
            log("No completed trades this session.")
            return
        bought      = [t for t in trades if t["buy_result"] == "bought"]
        take_profit = [t for t in bought if t["sell_result"] == "sold_take_profit"]
        backstop    = [t for t in bought if t["notes"] and "backstop" in str(t["notes"])]
        total_pnl = sum(float(t["pnl_usd"] or 0) for t in trades)
        wins = [t for t in trades if isinstance(t["pnl_usd"], (int, float)) and t["pnl_usd"] > 0]
        losses = [t for t in trades if isinstance(t["pnl_usd"], (int, float)) and t["pnl_usd"] < 0]

        # Windows are identified by slug, since each window has its own unique slug
        windows_seen = sorted(set(t["slug"] for t in trades))

        log("-" * 70)
        log(f"SUMMARY — {len(trades)} signals, {len(bought)} buy fills, across {len(windows_seen)} window(s)")
        log(f"  Take-profit hits: {len(take_profit)}")
        log(f"  Backstop force-exits (no stop-loss, target never hit): {len(backstop)}")
        log(f"  Wins: {len(wins)} | Losses: {len(losses)}")
        log(f"  Total PnL: {'+' if total_pnl >= 0 else ''}${total_pnl:.2f}")
        if windows_seen:
            log(f"  Average PnL per window: {'+' if total_pnl/len(windows_seen) >= 0 else ''}${total_pnl/len(windows_seen):.2f}")
        log("-" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket BTC 1-Hour Momentum Scalper")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--amount", type=float, default=10.0)
    args = parser.parse_args()

    bot = HourlyBot(dry_run=args.dry_run, amount=args.amount)
    bot.run()
