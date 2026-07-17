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

# REAL FIX, based on actual data: a real live session showed every catastrophic
# loss traced back to entries on a delta of only $8-9 (0.01-0.014%), confirmed
# by only a 2-second observation — genuinely just tick-to-tick noise, not a
# real signal. Average win was +$0.37; average loss on a position that never
# recovered was -$9.99 (single-sided) or -$5.28 (one split leg) — a single bad
# entry wiped out 14-27 wins worth of profit. The fix isn't a stop-loss (still
# not adding one back) — it's not entering on noise in the first place.
MIN_DELTA_PCT_TO_TRUST = 0.04    # raised from 0.01 — roughly $25-28 at current BTC prices, well above
MIN_DELTA_GROWTH_DURING_OBSERVATION = 10.0  # per explicit request: with $50/trade, require the delta to
                                               # genuinely GROW by at least this much during observation,
                                               # not just hold its sign — a real, strengthening move, not
                                               # a flat, unchanging reading. Starting guess from the stated
                                               # $10-15 range, needs tuning against real data like every
                                               # other threshold here.
                                    # the $8-9 noise-level deltas that caused every traced loss
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
# NO BACKSTOP by explicit request — a position exits ONLY via its take-profit
# order hitting, or real market settlement at window close. No time-based
# force-exit in between; that was still cutting positions off early, just
# on a timer instead of a price trigger, defeating the point of a long window.

MAX_CONCURRENT_POSITIONS = 1     # STRICT one-at-a-time, per explicit request — with $50/trade the risk per
                                    # position is much higher, so only one position open at any moment,
                                    # waiting for full resolution before considering the next entry

# ─── THREE ENTRY ZONES BY |delta from price-to-beat| ────────────────────────
# Starting thresholds based directly on the examples given (a $50-150 lean
# being a real but non-extreme settlement, $300+ being effectively decided) —
# not independently validated numbers, meant to be tuned against real data
# like every other threshold in this project.
SPLIT_ZONE_MIN = 50.0      # below this: too close to a coin-flip to split — single-sided momentum only
SPLIT_ZONE_MAX = 300.0     # above this: market has essentially decided — split the losing side would
                              # sit there forever, single-sided dominant-side only

# ─── MARKET-SENTIMENT CONFIRMATION ───────────────────────────────────────────
# HONEST CAVEAT: this whole section is a hypothesis built directly from the
# user's own stated market observation, NOT something independently verified
# against real data. It needs the same tuning-against-real-results treatment
# as every other threshold here — it's a real, testable model, not a proven rule.
#
# The core idea: the market's OWN pricing is an independent signal, separate
# from our own delta calculation — every other trader's money is already
# voting on how confident this move is. If delta is large but the price
# HASN'T moved to match, that means the market hasn't confirmed the move yet.
# Anchor points taken directly from the examples given: delta~$50 -> prices
# still floating near 0.55/0.45 (genuinely uncertain), delta~$100 -> price
# ~0.85 (market has reacted strongly, real confirmation).
PRICE_DELTA_CURVE = [
    (0, 0.50), (50, 0.55), (100, 0.85), (300, 0.97),
]
PRICE_CONFIRMATION_TOLERANCE = 0.05  # how far below the expected curve the actual price is allowed to be
                                        # before we treat it as "market hasn't confirmed this move yet"

# SPLIT price-band sanity check: confirmed live — a split entered at
# Down=$0.918/Up=$0.09 lost $3.50 because the cheap leg had almost no real
# room to gain $0.02 (it was already priced as a near-certain loser). Both
# legs need a genuine chance for a split to make sense.
MIN_SPLIT_LEG_PRICE = 0.15
MAX_SPLIT_LEG_PRICE = 0.85

# Retrospective liveliness check: confirmed via real data that price-curve
# and delta-size checks alone don't catch "one leg just isn't moving" —
# both real losses looked entirely reasonable by those checks. This directly
# observes real, current movement instead. Starting duration, needs tuning.
RETROSPECTIVE_CHECK_SECONDS = 30.0

LATE_WINDOW_CUTOFF_MIN = 10      # in the final N minutes: NOT a ban — just switch to a brief observation
                                    # window before entering, more caution rather than no entries at all
LATE_WINDOW_OBSERVATION_SEC = 30.0  # longer than STANDARD_OBSERVATION_SEC — more caution late in the
                                       # window, not less; fixed an inversion introduced when the standard
                                       # observation was raised from 2s to 20s

# REAL FIX: the "normal" 2-second observation used everywhere else was long
# enough to catch a single tick reversal but nowhere near long enough to
# distinguish a real, developing trend from ordinary short-term noise —
# confirmed by every traced loss having passed this exact check. Over a full
# hour, spending 20-30 real seconds confirming a signal costs nothing.
STANDARD_OBSERVATION_SEC = 20.0

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

def _validate_token_on_clob(token_id: str) -> bool:
    """Confirms a token ID actually resolves on the CLOB (not a 404) —
    confirmed live: a matched event can contain token IDs that don't
    actually exist on the CLOB, which looks identical to 'no liquidity'
    unless specifically checked for."""
    try:
        r = requests.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=3)
        return r.status_code != 404
    except Exception:
        return False

def get_window_market(slug_prefix: str, start_ts: int):
    # Try the clean deterministic pattern first (works for 5m/15m/4h,
    # unconfirmed for 1h specifically), then fall back to the ET-based
    # human slug in both known year variants. Now tries ALL candidates
    # exhaustively if an earlier match's markets all fail validation,
    # rather than stopping at the first event found regardless of whether
    # its tokens actually work.
    candidates = [f"{slug_prefix}-{start_ts}"] + _et_slug_candidates(start_ts)

    for slug in candidates:
        try:
            r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=3)
            r.raise_for_status()
            data = r.json()
            if not data:
                continue
            event = data[0]
        except Exception:
            continue

        markets = event.get("markets", [])
        if not markets:
            continue

        # REAL BUG FIXED HERE: confirmed live via an actual HTTP 404 from the
        # CLOB — blindly taking markets[0] can hand back a token ID that
        # doesn't exist on the CLOB at all, which looks identical to "no
        # liquidity" unless specifically checked. Validate each candidate
        # market's tokens actually resolve before accepting it.
        for market in markets:
            try:
                outcomes       = json.loads(market.get("outcomes", "[]"))
                clob_token_ids = json.loads(market.get("clobTokenIds", "[]"))
            except Exception:
                continue
            if len(outcomes) < 2 or len(clob_token_ids) < 2:
                continue
            tokens = dict(zip(outcomes, clob_token_ids))
            if "Down" not in tokens or "Up" not in tokens:
                continue
            down_valid = _validate_token_on_clob(tokens["Down"])
            up_valid = _validate_token_on_clob(tokens["Up"])
            if down_valid and up_valid:
                return {
                    "slug": slug, "crypto": MARKETS[slug_prefix], "start_ts": start_ts,
                    "close_ts": start_ts + WINDOW_SECONDS,
                    "down_token": tokens["Down"], "up_token": tokens["Up"],
                    "condition_id": market.get("conditionId", ""), "title": event.get("title", ""),
                }
            log(f"Rejected a candidate market (slug={slug}, title='{event.get('title','')}', "
                f"condition={market.get('conditionId','')[:16]}...) — token validation failed "
                f"(Down valid={down_valid}, Up valid={up_valid})")

    log(f"No candidate slug produced a market with tokens that actually validate on the CLOB — "
        f"tried: {candidates}")
    return None

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

def expected_price_for_delta(abs_delta: float) -> float:
    """Piecewise-linear interpolation through PRICE_DELTA_CURVE — a rough
    model of what the market's own pricing SHOULD look like for a given
    |delta|, built directly from the anchor points given (delta~$50 -> still
    uncertain ~0.55, delta~$100 -> market has reacted strongly ~0.85). This
    is a hypothesis to test against real data, not a validated formula."""
    points = PRICE_DELTA_CURVE
    if abs_delta <= points[0][0]:
        return points[0][1]
    if abs_delta >= points[-1][0]:
        return points[-1][1]
    for i in range(len(points) - 1):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        if x0 <= abs_delta <= x1:
            fraction = (abs_delta - x0) / (x1 - x0)
            return y0 + fraction * (y1 - y0)
    return points[-1][1]  # unreachable, safety fallback

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
class PositionTracker:
    """Tracks realized pnl (from closed trades) and potential pnl (from
    currently-open, unresolved positions, assuming each hits its own
    take-profit) for ONE window. Thread-safe since multiple positions can
    now be open concurrently, each resolving in its own thread.

    The gating logic: stop opening NEW positions once realized + potential
    reaches the target — but if an open position ends up losing instead of
    winning, potential shrinks and realized may drop, automatically
    re-opening the gate for new entries without any separate 'resume' step."""
    def __init__(self, target_profit: float):
        self.target_profit = target_profit
        self.lock = threading.Lock()
        self.realized_pnl = 0.0
        self.open_positions = {}  # position_id -> potential pnl if it hits target
        self._next_id = 0

    def register_open(self, potential_pnl_contribution: float) -> int:
        with self.lock:
            pos_id = self._next_id
            self._next_id += 1
            self.open_positions[pos_id] = potential_pnl_contribution
            return pos_id

    def resolve(self, pos_id: int, actual_pnl: float):
        with self.lock:
            self.open_positions.pop(pos_id, None)
            self.realized_pnl += actual_pnl

    def should_stop_new_entries(self) -> bool:
        with self.lock:
            return (self.realized_pnl + sum(self.open_positions.values())) >= self.target_profit

    def totals(self):
        with self.lock:
            potential = sum(self.open_positions.values())
            return self.realized_pnl, potential, len(self.open_positions)


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
        log(f"Sell: take-profit entry+${PROFIT_MARGIN} | NO stop-loss, NO backstop — holds to window "
            f"close and resolves at real settlement if target never hits")
        log(f"Max {MAX_CONCURRENT_POSITIONS} concurrent positions | {STANDARD_OBSERVATION_SEC:.0f}s momentum-persistence "
            f"+ order-book-depth confirmation on every entry | last {LATE_WINDOW_CUTOFF_MIN} min: {LATE_WINDOW_OBSERVATION_SEC:.0f}s observation")
        log(f"Zones by |delta|: <${SPLIT_ZONE_MIN} or >=${SPLIT_ZONE_MAX} -> single-sided dominant side | "
            f"${SPLIT_ZONE_MIN}-${SPLIT_ZONE_MAX} -> SPLIT both sides")
        log(f"Market-sentiment confirmation: real price must be within ${PRICE_CONFIRMATION_TOLERANCE} of "
            f"the expected curve for this delta (hypothesis, needs real-data tuning) | Split legs must be "
            f"${MIN_SPLIT_LEG_PRICE}-${MAX_SPLIT_LEG_PRICE}")
        log(f"Session target: stop opening NEW positions once realized + potential (from open positions) "
            f"reaches +${TARGET_PROFIT_PER_WINDOW} — already-open positions still resolve independently")
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
    def _watch_for_sell(self, token: str, buy_price: float, raw_shares: float, crypto: str, close_ts: float,
                          side: str, window_open_price: float, symbol: str) -> dict:
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
            log(f"Buy price ${buy_price} leaves no room for a take-profit — will hold to window close, "
                f"resolving at real settlement", crypto)
            take_profit_price = None

        # NO BACKSTOP. By explicit request: a position exits ONLY two ways —
        # its take-profit order fills, or the window closes and it resolves
        # at the real market outcome. No time-based force-exit in between,
        # since that was still cutting positions off before the window's
        # actual time was used, just on a timer instead of a price trigger.
        log(f"Take-profit: {'$'+str(take_profit_price) if take_profit_price else 'N/A (no room)'} "
            f"(+${PROFIT_MARGIN}) | holds to window close if never hit | NO stop-loss, NO backstop", crypto)
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
                    log(f"Could not place take-profit order ({e}) — this position will resolve at "
                        f"window close instead", crypto)

            while now_unix() < close_ts:
                if self.stop_event.is_set():
                    break
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

            # Window closed without hitting target — cancel the resting order
            # and resolve at the REAL market outcome, not a forced exit price.
            if tp_order_id is not None:
                try:
                    self.client.cancel_order(OrderPayload(orderID=tp_order_id))
                except Exception:
                    pass
            return self._resolve_single_at_settlement(buy_price, shares, side, window_open_price, symbol, crypto)

        # DRY-RUN
        while now_unix() < close_ts:
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
        return self._resolve_single_at_settlement(buy_price, shares, side, window_open_price, symbol, crypto)

    def _resolve_single_at_settlement(self, buy_price: float, shares: float, side: str,
                                         window_open_price: float, symbol: str, crypto: str) -> dict:
        """Window closed without the take-profit ever hitting — resolves at
        the REAL market outcome (the side either pays $1/share or $0/share),
        not a forced exit at whatever price happened to be available."""
        final_price = get_binance_price(symbol)
        up_won = (final_price is not None and window_open_price is not None and final_price > window_open_price)
        this_side_won = up_won if side == "Up" else (not up_won)
        resolve_price = 1.0 if this_side_won else 0.0
        pnl = round((resolve_price - buy_price) * shares, 4)
        log(f"Window closed, take-profit never hit — resolved to ${resolve_price} at real settlement, "
            f"pnl={'+' if pnl>=0 else ''}${pnl}", crypto)
        return {"result": "resolved_at_close", "price": resolve_price, "pnl_usd": pnl,
                "notes": f"held to window close, resolved to ${resolve_price} (no backstop, no early exit)"}

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
    # ── CONCURRENT TRADE WORKERS (each runs in its own thread) ───────────────
    def _run_single_trade_thread(self, token: str, entry_price_ref: float, is_ask: bool,
                                    delta_side: str, close_ts: float, crypto: str, row_base: dict,
                                    window_open_price: float, symbol: str):
        buy_buffer = BUY_CEILING_BUFFER if is_ask else THIN_MARKET_BUY_BUFFER
        buy_info = self._attempt_buy(token, entry_price_ref, crypto, buffer_override=buy_buffer)
        row = dict(row_base)
        row.update({"buy_result": buy_info["result"], "buy_price": buy_info["price"], "buy_shares": buy_info["shares"]})
        if buy_info["result"] != "bought":
            realized, _, n_open = self.position_tracker.totals()
            row.update({"sell_result": "n/a", "sell_price": "", "pnl_usd": 0,
                        "cumulative_pnl_this_window": round(realized, 4), "notes": "no buy fill"})
            self._record(row)
            return

        target_price = min(round(buy_info["price"] + PROFIT_MARGIN, 4), 0.99)
        potential = max(target_price - buy_info["price"], 0) * buy_info["shares"]
        pos_id = self.position_tracker.register_open(potential)
        log(f"Position opened (id={pos_id}): {delta_side} {buy_info['shares']} @ ${buy_info['price']}, "
            f"potential +${potential:.2f} if it hits target", crypto)

        sell_info = self._watch_for_sell(token, buy_info["price"], buy_info["shares"], crypto, close_ts,
                                            delta_side, window_open_price, symbol)
        actual_pnl = float(sell_info["pnl_usd"] or 0)
        self.position_tracker.resolve(pos_id, actual_pnl)
        realized, potential_total, n_open = self.position_tracker.totals()

        row.update({
            "sell_result": sell_info["result"], "sell_price": sell_info["price"],
            "seconds_to_sell": sell_info.get("seconds_to_sell", ""),
            "pnl_usd": sell_info["pnl_usd"], "cumulative_pnl_this_window": round(realized, 4),
            "notes": sell_info["notes"],
        })
        self._record(row)
        log(f"Position resolved (id={pos_id}): realized=${realized:.2f} | still {n_open} open "
            f"(potential +${potential_total:.2f})", crypto)

    def _run_split_trade_thread(self, market: dict, down_ask: float, up_ask: float,
                                   close_ts: float, crypto: str, row_base: dict, window_open_price: float):
        # Split mints self.amount shares of EACH side at the fixed $1/pair
        # rate — potential pnl if BOTH legs hit their +PROFIT_MARGIN target:
        potential = self.amount * PROFIT_MARGIN * 2
        pos_id = self.position_tracker.register_open(potential)
        log(f"Split position opened (id={pos_id}): potential +${potential:.2f} if both legs hit target", crypto)

        split_outcome = self._enter_split(market, market["condition_id"], down_ask, up_ask, close_ts, crypto,
                                            window_open_price)
        actual_pnl = float(split_outcome["pnl_usd"] or 0)
        self.position_tracker.resolve(pos_id, actual_pnl)
        realized, potential_total, n_open = self.position_tracker.totals()

        row = dict(row_base)
        row.update({
            "buy_result": "split", "buy_price": "", "buy_shares": "",
            "sell_result": split_outcome["outcome"], "sell_price": "",
            "pnl_usd": split_outcome["pnl_usd"], "cumulative_pnl_this_window": round(realized, 4),
            "notes": split_outcome["notes"],
        })
        self._record(row)
        log(f"Split position resolved (id={pos_id}): realized=${realized:.2f} | still {n_open} open "
            f"(potential +${potential_total:.2f})", crypto)

    def _confirm_signal_persists(self, symbol: str, window_open_price: float, delta_side: str,
                                    observe_seconds: float, crypto: str, delta_at_start: float) -> bool:
        """Momentum-persistence + growth check: requires the delta to keep
        pointing the SAME direction AND to have genuinely GROWN by at least
        MIN_DELTA_GROWTH_DURING_OBSERVATION during the observation window —
        a flat, unchanging delta that happens to hold its sign is a much
        weaker signal than one that's actively strengthening. Raised bar per
        explicit request, given the much larger $50/trade size. This does
        NOT and cannot guarantee the direction won't reverse later; it only
        filters out signals that don't show real, continuing momentum."""
        deadline = now_unix() + observe_seconds
        last_known_delta = delta_at_start
        while now_unix() < deadline:
            time.sleep(0.5)
            check_price = get_binance_price(symbol)
            if check_price is None:
                continue
            check_delta = check_price - window_open_price
            check_side = "Up" if check_delta > 0 else "Down"
            if check_side != delta_side:
                return False
            last_known_delta = check_delta

        growth = abs(last_known_delta) - abs(delta_at_start)
        if growth < MIN_DELTA_GROWTH_DURING_OBSERVATION:
            log(f"Direction held but only grew ${growth:.2f} during observation (need at least "
                f"${MIN_DELTA_GROWTH_DURING_OBSERVATION}) — not a strong enough continuing move", crypto)
            return False
        return True

    def _check_order_book_depth(self, token: str, needed_shares: float) -> bool:
        """Requires real bid depth behind the current price (not just a
        wafer-thin book that could vanish immediately) — a basic order-book
        confirmation, not a guarantee against reversal."""
        book = get_order_book(token)
        bid, bid_size = best_bid(book)
        if bid is None or bid_size is None:
            return False
        return bid_size >= needed_shares * 0.5  # at least half our intended size resting as real interest

    def _check_both_sides_actively_repricing(self, down_token: str, up_token: str,
                                                down_start: float, up_start: float,
                                                duration: float, crypto: str) -> bool:
        """Directly observes whether BOTH sides are actively moving right
        now, rather than inferring it from delta size or a static price
        curve. Confirmed by real data: both actual losses had delta and
        price-curve readings that looked entirely reasonable, yet one leg
        never moved enough to hit its target — this check looks at real,
        current movement instead of a proxy for it. Tracks each side's own
        best movement toward its target during the window; requires BOTH
        to show at least half of PROFIT_MARGIN worth of real movement."""
        down_best = down_start
        up_best = up_start
        deadline = now_unix() + duration
        while now_unix() < deadline:
            time.sleep(1.0)
            down_bid, _ = best_bid(get_order_book(down_token))
            up_bid, _ = best_bid(get_order_book(up_token))
            if down_bid is not None and down_bid > down_best:
                down_best = down_bid
            if up_bid is not None and up_bid > up_best:
                up_best = up_bid

        down_movement = down_best - down_start
        up_movement = up_best - up_start
        required = PROFIT_MARGIN * 0.5
        if down_movement < required or up_movement < required:
            log(f"Retrospective check: Down moved ${down_movement:.3f}, Up moved ${up_movement:.3f} "
                f"during {duration:.0f}s (need ${required} on both) — not genuinely repricing on both "
                f"sides right now, skipping", crypto)
            return False
        log(f"Retrospective check passed: Down moved ${down_movement:.3f}, Up moved ${up_movement:.3f} "
            f"during {duration:.0f}s — real, active two-sided movement confirmed", crypto)
        return True

    def _enter_split(self, market: dict, condition_id: str, down_ask: float, up_ask: float,
                       close_ts: float, crypto: str, window_open_price: float) -> dict:
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

        # NO BACKSTOP — by explicit request, a position only ever exits via
        # its own take-profit hit or real market settlement at window close.
        while now_unix() < close_ts and not (down_sold and up_sold):
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

        # Window closed with one or both legs never having hit target —
        # resolve at REAL market settlement, never a forced early exit.
        # Uses the SAME window_open_price established at window start,
        # not an independent re-fetch (which would both risk the same
        # race condition and risk a subtly different reference value
        # than what entry decisions were based on).
        symbol = SYMBOLS.get(crypto)
        final_price = get_binance_price(symbol)
        up_won = (final_price is not None and window_open_price is not None and final_price > window_open_price)
        if not down_sold:
            down_exit = 0.0 if up_won else 1.0
        if not up_sold:
            up_exit = 1.0 if up_won else 0.0
        proceeds = shares * down_exit + shares * up_exit
        pnl = round(proceeds - total_cost, 4)
        outcome = "one_hit_other_resolved" if (down_sold or up_sold) else "neither_hit_resolved_at_close"
        return {"outcome": outcome, "pnl_usd": pnl, "notes": "held to window close, resolved at real settlement"}

    # ── WINDOW LOOP ──────────────────────────────────────────────────────────
    def _monitor_window(self, slug_prefix: str, start_ts: int):
        crypto = MARKETS[slug_prefix]
        close_ts = start_ts + WINDOW_SECONDS
        symbol = SYMBOLS.get(crypto)
        market = None
        find_deadline = now_unix() + 15  # generous — the new token-validation logic does more HTTP
                                            # calls per attempt (checking each candidate market's tokens
                                            # actually resolve), worth the extra time given we have a
                                            # full hour to work with either way
        while now_unix() < find_deadline:
            market = get_window_market(slug_prefix, start_ts)
            if market:
                break
            time.sleep(0.5)
        if not market:
            log(f"Could not find market for window starting {start_ts} — skipping entire window "
                f"(if this happens every window, the 1h slug format needs checking)", crypto)
            return

        # REAL FIX: previously looked up the historical 1h candle via
        # get_window_open_price, which can race against Binance's own
        # candle-indexing right at the top of the hour. Since this is
        # ALWAYS called within seconds of the window actually starting,
        # the current spot price IS the window-open price for practical
        # purposes — no historical lookup needed, no race condition possible.
        window_open_price = get_binance_price(symbol) if symbol else None
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

        self.position_tracker = PositionTracker(TARGET_PROFIT_PER_WINDOW)
        trades_this_window = 0
        trade_count_lock = threading.Lock()
        open_threads = []
        gate_was_closed = False  # tracks state so we only log on CHANGE, not every second
        concurrent_cap_was_hit = False  # same log-on-change pattern for the concurrent-position cap

        while now_unix() < close_ts:
            if self.stop_event.is_set():
                break

            # TOP PRIORITY, unconditional: stop opening NEW positions once the
            # projected total (realized + potential from open positions) hits
            # the target — but keep monitoring whatever's already open.
            if self.position_tracker.should_stop_new_entries():
                realized, potential, n_open = self.position_tracker.totals()
                if n_open > 0:
                    if not gate_was_closed:
                        log(f"Projected total (${realized + potential:.2f}) already at/above target — "
                            f"waiting for {n_open} open position(s) to resolve before considering new entries", crypto)
                        gate_was_closed = True
                    time.sleep(MONITOR_INTERVAL)
                    continue
                # REAL FIX, per explicit request: target achieved and nothing left
                # open — sleep until close to window end instead of polling every
                # second for the rest of the hour with nothing left to do.
                sleep_until = close_ts - 60  # wake up 1 minute before the window closes
                remaining = sleep_until - now_unix()
                if remaining > 0:
                    if not gate_was_closed:
                        log(f"Target achieved (${realized:.2f}) with nothing left open — sleeping until "
                            f"1 min before this window closes", crypto)
                        gate_was_closed = True
                    time.sleep(min(remaining, 60))
                    continue
                time.sleep(MONITOR_INTERVAL)
                continue
            gate_was_closed = False

            # REAL FIX: caps concurrent open positions so the bot can't
            # impulse-fire many entries in the first minute of a window on
            # thin, unconfirmed signals — confirmed live as the exact pattern
            # behind the worst losing windows.
            _, _, n_open_now = self.position_tracker.totals()
            if n_open_now >= MAX_CONCURRENT_POSITIONS:
                if not concurrent_cap_was_hit:
                    log(f"At the concurrent-position cap ({MAX_CONCURRENT_POSITIONS}) — waiting for one "
                        f"to resolve before considering new entries", crypto)
                    concurrent_cap_was_hit = True
                time.sleep(MONITOR_INTERVAL)
                continue
            concurrent_cap_was_hit = False

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

            # Momentum-persistence confirmation on EVERY entry now, not just
            # late-window ones — longer observation late in the window
            # (more caution when there's less time to recover), shorter
            # early on (more time available either way). This reduces
            # exposure to pure single-tick noise; it does not and cannot
            # guarantee the direction won't reverse later.
            observe_secs = LATE_WINDOW_OBSERVATION_SEC if minutes_left <= LATE_WINDOW_CUTOFF_MIN else STANDARD_OBSERVATION_SEC
            log(f"Delta {delta_value:+.2f} ({minutes_left:.0f} min left) — observing {observe_secs}s "
                f"for persistence before entering", crypto)
            if not self._confirm_signal_persists(symbol, window_open_price, delta_side, observe_secs, crypto, delta_value):
                log(f"Signal did not pass confirmation — skipping", crypto)
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
                    log(f"No price available for one or both sides — retrying", crypto)
                    time.sleep(MONITOR_INTERVAL)
                    continue
                # Order-book depth confirmation on both legs
                if not (self._check_order_book_depth(market["down_token"], self.amount) and
                        self._check_order_book_depth(market["up_token"], self.amount)):
                    log(f"Order book depth too thin on one or both sides — skipping this signal", crypto)
                    time.sleep(MONITOR_INTERVAL)
                    continue
                # REAL FIX: confirmed live — a split entered at Down=$0.918/Up=$0.09
                # lost $3.50 because the cheap leg had almost no real room to gain
                # $0.02. Both legs need a genuine chance for a split to make sense.
                leading_price = max(down_ask, up_ask)
                trailing_price = min(down_ask, up_ask)
                if trailing_price < MIN_SPLIT_LEG_PRICE or leading_price > MAX_SPLIT_LEG_PRICE:
                    log(f"Split prices too extreme (Down=${down_ask}, Up=${up_ask}) — the cheap leg has "
                        f"little real room to hit its own target, skipping this split", crypto)
                    time.sleep(MONITOR_INTERVAL)
                    continue
                # Market-sentiment confirmation: does the market's OWN pricing agree
                # this delta is real? See PRICE_DELTA_CURVE definition for the caveat.
                expected = expected_price_for_delta(abs_delta)
                if leading_price < expected - PRICE_CONFIRMATION_TOLERANCE:
                    log(f"Market pricing (leading side ${leading_price}) hasn't caught up to what delta "
                        f"${abs_delta:.0f} would suggest (expected ~${expected:.2f}) — market hasn't "
                        f"confirmed this move yet, skipping", crypto)
                    time.sleep(MONITOR_INTERVAL)
                    continue

                # Retrospective check: directly observe real, active repricing on
                # BOTH sides before committing capital, rather than inferring it.
                if not self._check_both_sides_actively_repricing(
                        market["down_token"], market["up_token"], down_ask, up_ask,
                        RETROSPECTIVE_CHECK_SECONDS, crypto):
                    time.sleep(MONITOR_INTERVAL)
                    continue

                # Refresh prices since real time passed during the retrospective check
                down_ask, down_is_ask = get_reference_price(market["down_token"])
                up_ask, up_is_ask = get_reference_price(market["up_token"])
                if down_ask is None or up_ask is None:
                    log(f"Price disappeared after retrospective check — skipping", crypto)
                    time.sleep(MONITOR_INTERVAL)
                    continue

                with trade_count_lock:
                    trades_this_window += 1
                    this_trade_num = trades_this_window
                row_base = {
                    "timestamp": ts_str(), "bot_name": self.bot_name, "mode": self.mode_str, "crypto": crypto,
                    "slug": market["slug"], "trade_num_this_window": this_trade_num,
                    "delta_side": "SPLIT", "delta_value": round(delta_value, 4), "delta_pct": round(delta_pct, 4),
                    "minutes_left_in_window": round(minutes_left, 1),
                }
                log(f"Delta {delta_value:+.2f} ({minutes_left:.0f} min left) is in the moderate-lean zone "
                    f"(${SPLIT_ZONE_MIN}-${SPLIT_ZONE_MAX}), prices confirm (${leading_price} vs expected "
                    f"${expected:.2f}), real repricing confirmed -> SPLIT both sides (trade {this_trade_num})", crypto)
                t = threading.Thread(target=self._run_split_trade_thread,
                                       args=(market, down_ask, up_ask, close_ts, crypto, row_base, window_open_price),
                                       daemon=True)
                t.start()
                open_threads.append(t)
                time.sleep(MONITOR_INTERVAL)
                continue

            # Single-sided: either the early/uncertain zone (delta < SPLIT_ZONE_MIN)
            # or the extreme/decided zone (delta >= SPLIT_ZONE_MAX) — always bet
            # the leaning/dominant side, never the side unlikely to move.
            token = market["up_token"] if delta_side == "Up" else market["down_token"]
            observed_price, is_ask = get_reference_price(token)
            if observed_price is None:
                log(f"No price available for {delta_side} token — retrying", crypto)
                time.sleep(MONITOR_INTERVAL)
                continue
            if not self._check_order_book_depth(token, self.amount):
                log(f"Order book depth too thin on {delta_side} — skipping this signal", crypto)
                time.sleep(MONITOR_INTERVAL)
                continue

            # Same market-sentiment confirmation for single-sided entries — just
            # because a side is priced high doesn't mean it'll keep moving;
            # confirm the market's own pricing actually matches this delta.
            expected = expected_price_for_delta(abs_delta)
            if observed_price < expected - PRICE_CONFIRMATION_TOLERANCE:
                log(f"Market pricing (${observed_price}) hasn't caught up to what delta ${abs_delta:.0f} "
                    f"would suggest (expected ~${expected:.2f}) — market hasn't confirmed this move yet, "
                    f"skipping", crypto)
                time.sleep(MONITOR_INTERVAL)
                continue

            with trade_count_lock:
                trades_this_window += 1
                this_trade_num = trades_this_window
            row_base = {
                "timestamp": ts_str(), "bot_name": self.bot_name, "mode": self.mode_str, "crypto": crypto,
                "slug": market["slug"], "trade_num_this_window": this_trade_num,
                "delta_side": delta_side, "delta_value": round(delta_value, 4), "delta_pct": round(delta_pct, 4),
                "minutes_left_in_window": round(minutes_left, 1),
            }

            book = get_order_book(token)
            observed_bid, _ = best_bid(book)
            spread_at_buy = round(observed_price - observed_bid, 4) if observed_bid is not None else None
            ref_note = "ask" if is_ask else "bid (no ask currently resting)"
            zone_label = "extreme/decided" if abs_delta >= SPLIT_ZONE_MAX else "early/uncertain"
            log(f"Delta signal (trade {this_trade_num}, {minutes_left:.0f} min left, {zone_label} zone), "
                f"prices confirm (${observed_price} vs expected ${expected:.2f}): "
                f"{delta_value:+.2f} ({delta_pct:.4f}%) -> buying {delta_side} @ ~${observed_price} "
                f"(priced off {ref_note}, spread: ${spread_at_buy})", crypto)
            row_base["spread_at_buy"] = spread_at_buy

            t = threading.Thread(target=self._run_single_trade_thread,
                                   args=(token, observed_price, is_ask, delta_side, close_ts, crypto, row_base,
                                          window_open_price, symbol),
                                   daemon=True)
            t.start()
            open_threads.append(t)
            time.sleep(MONITOR_INTERVAL)

        # Window closing — let any still-open positions resolve on their own
        # (each thread's own backstop is already capped at close_ts), but
        # don't block the NEXT window's discovery waiting for them.
        realized, potential, n_open = self.position_tracker.totals()
        log(f"Window closed. Trades opened this window: {trades_this_window} | "
            f"Realized pnl: {'+' if realized>=0 else ''}${realized:.2f}"
            + (f" | {n_open} position(s) still resolving (potential +${potential:.2f})" if n_open else ""), crypto)

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
    parser.add_argument("--amount", type=float, default=50.0)
    args = parser.parse_args()

    bot = HourlyBot(dry_run=args.dry_run, amount=args.amount)
    bot.run()
