# -*- coding: utf-8 -*-
"""
VortexAlpha v9.2 - ICT/SMC AMD Strategy Bot
Author   : Nayem
Strategy : AMD (Accumulation-Manipulation-Distribution) — EURUSD + GBPUSD + XAUUSD
Deploy   : Railway (GitHub)

Architecture:
  Twelve Data API  → OHLC candles (candle-slot cache + per-minute credit limiter)
  yfinance         → Real-time price + 1m high/low bars (30s interval, FREE)
                     XAUUSD: GC=F futures, basis-corrected to Twelve Data spot
  ForexFactory JSON→ Economic calendar (news filter)
  Telegram Bot     → Signal alerts + commands
  Supabase         → Full persistence + analytics + shadow signals

AMD Strategy — 5 Mandatory Gates (strictly chronological, closed-candle structure):
  GATE 1: Bias              → Daily + Weekly; both neutral = skip; on conflict
                              DAILY leads with a -1.5 score penalty (v9.2)
  GATE 2: Liquidity Sweep   → 1H sweep, close back inside MANDATORY
  GATE 3: Displacement      → strong impulsive candle AT or AFTER the sweep candle
  GATE 4: FVG               → created by the displacement candle, not invalidated since
  GATE 5: FVG Retest Entry  → live price back in FVG zone → MARKET entry at live price

SL : FVG boundary ± 0.3×ATR(1H)      TP1: 1.8R (50% close, SL → BE)      TP2: 3.0R

========================================================================
CHANGELOG — v9.2 (on top of v9.1) — 3-month DEMO configuration
Everything not listed here is byte-for-byte the v9.1 logic.
========================================================================
  [A-1] XAUUSD added to PAIRS_CFG (pip_mult=10 → 1 pip = $0.10).
  [A-2] YFINANCE_SYMBOLS "XAU/USD" → "GC=F" (COMEX gold futures).
  [A-3] PRICE FEED FIX for basis pairs (PRICE_BASIS_PAIRS = {"XAU/USD"}):
          * Every scan measures basis = Twelve Data spot (latest 1H candle
            close, fetched seconds earlier) − GC=F last price.
          * Price monitor applies the basis to every GC=F 1m bar (high, low,
            close) → SL/TP checks run on spot-equivalent prices.
          * Gate 5 live price for basis pairs = Twelve Data forming-candle
            close (spot), never the raw futures price.
          * Outside the scan window (open trade overnight) the basis is
            refreshed from Twelve Data /price at most every 15 min
            (≤ 4 credits/hour) — never every 30 s.
  [A-4] Basis sanity: every scan logs |GC=F + previous basis − spot|;
        above $2.00 → one Telegram alert per day.
  [A-5] XAU sl_min_pips = 30 ($3.00): with a typical ~$0.30 gold spread the
        spread stays ≤ 10% of risk. Raise it if your account's spread is wider.
  [B-6] score_threshold = 7.0 for all pairs (demo test). score_threshold_weak
        stays 7.0.
  [B-7] Shadow band is therefore 6.5–6.9 (SHADOW_SCORE_MIN unchanged = 6.5).
  [G1-1] Gate 1: Daily/Weekly conflict no longer skips. DAILY bias leads,
        SCORE_WEEKLY_CONFLICT = -1.5 is added, and weekly_conflict is saved in
        the result dict, the trades row and the shadow row. The signal shows
        "Weekly : opposed ⚠️ (daily leads)". Both-neutral skip and the
        daily-neutral → weekly fallback are unchanged.
  [D-13] Min-SL guard: per-pair sl_min_pips (EUR/USD 8, GBP/USD 8,
        XAU/USD 30). Structural SL below it → setup skipped (logged).
  [P3-6] 3-pair credit safety: HTF fetch order is interval-major (4H for all
        pairs first) and an HTF catch-up pass runs between scans (:07:20,
        :22:20, :37:20, :52:20) to refresh slots the scan served stale. No extra
        credits — the same fetches happen earlier. Without it, ~1–2 Daily/Weekly
        slots per day were never refreshed with 3 pairs (verified by simulation).
  [G-18] ENABLED_PAIRS env var (e.g. "EURUSD,GBPUSD,XAUUSD"; default all).
        Open trades of a disabled pair are still monitored to completion.
  NEWS_CURRENCIES unchanged ({"USD","EUR","GBP"}) — gold is driven by USD news.

HOTFIX v9.2.1 (after the first live day, 2026-09-29 log)
  [A-3a] Gold basis is now measured on MATCHED 1-minute bars: median of
        (Twelve Data 1min close − GC=F 1min close) over the last ≤10 minutes
        closed on both feeds. The live log showed the unmatched method jumping
        $1–12 per scan because Yahoo's feed lags; that lag now cancels out.
        Cost: +1 credit per scan (Twelve Data 1min candles) ≈ +40/day.
        Yahoo lag is logged every scan and shown in /status.
  [A-3b] Price monitor window is bar-based (per-trade last_bar_ts) instead of
        wall-clock: bars that a lagging feed delivers late are no longer
        skipped (v9.1 window silently dropped them → missed wicks).
        Applies to all pairs; for a real-time feed the result is identical.
  Logging goes to stdout (Railway showed every INFO line as "error").

v9.2 MIGRATION (run once, safe to re-run) — in addition to the v9.1 SQL below:
alter table trades         add column if not exists weekly_conflict boolean default false;
alter table shadow_signals add column if not exists weekly_conflict boolean default false;
(If you skip it, the bot keeps working: it saves without the column and
 sends one Telegram notice.)

3-MONTH REVIEW SQL — see REVIEW_SQL at the end of this file.

========================================================================
CHANGELOG — v9.1 (on top of v9 Final Locked Baseline)
Scoring weights, thresholds, sweep/disp/FVG constants, session, cooldown,
TP multipliers and SL buffer are UNCHANGED. Only bugs/logic flaws fixed.
========================================================================
PHASE 1 — Core logic
  [P1-1] Gate sequence: detect_sweeps() now returns the sweep candle index.
         find_amd_setup() only accepts a displacement candle with
         index >= sweep index. (v9: sweep and displacement were searched in
         unrelated windows, so displacement could PRECEDE the sweep.)
  [P1-2] Repainting: sweep / displacement / FVG / OB / BOS are detected on
         CLOSED candles only (split_closed()). The forming candle is used
         only as touch-evidence for the retest trigger. The signal itself
         fires on the LIVE price (a price-level trigger, not a pattern that
         can repaint). Entry = actual live price (v9 clamped close to the
         zone, which could print an entry that was not executable).
  [P1-3] Multiple candidates: all sweeps (freshest first) and all
         displacement candles after each sweep are tried until a valid FVG
         is found. (v9 returned only the FIRST displacement; if it had no
         FVG the setup was lost. v9's retest window was also effectively
         <=7 bars instead of 12.)
  [P1-4] FVG invalidation: a closed candle beyond the far edge of the FVG
         (BUY: close < bottom, SELL: close > top) kills the setup.
  [P1-5] Sweep quality: "close back inside" is now MANDATORY
         (+ wick OR body). v9's 2-of-3 rule let breakdown candles pass.
  [P1-6] HTF OB dead-code fix: detect_obs() loop range + per-timeframe
         max_age. v9 required 3 subsequent candles AND age < 36h, which is
         impossible on Daily → HTF bonus (+1.25/+2.0) was never awarded.
         Now Daily (30d) + Weekly (26w) OBs are detected, mitigated OBs are
         dropped, and the impulse must be in the trade direction.
PHASE 2 — Risk & accounting
  [P2-1] Blended R-multiple: orig_sl / initial_risk stored at entry and
         never overwritten. Close P&L = 50%×TP1 leg + 50%×runner leg.
         (v9: TP1→BE counted 0 pips, TP2 counted 3R on a half position,
         RR divided by the moved SL.)
  [P2-2] Wick-accurate monitoring: SL/TP checked against 1m high/low since
         the last check (v9 only saw the last price every 30s).
  [P2-3] Persistence: reset/report markers, sent-signal hashes and shadow
         trades are persisted → a Railway restart no longer resets the
         Daily Loss Limit or duplicates reports/signals.
  [P2-4] Open-risk check: used + open + new <= daily limit. If the full
         risk doesn't fit, risk is scaled down to what remains (min 0.25%),
         otherwise the signal is skipped.
  [P2-5] MEDIUM-news risk cap applied AFTER dynamic risk (v9 let the
         STRONG_TREND multiplier undo it).
  [P2-6] Partial-TP / trailing state is persisted immediately (v9 only
         saved on close, so a restart after TP1 lost the BE move).
PHASE 3 — Performance
  [P3-1] Scans aligned to wall clock (:00:20, :15:20, :30:20, :45:20 UTC).
  [P3-2] Candle cache keyed to candle slots + Twelve Data credit limiter;
         the fixed 60s wait is gone. HTF data may be served one slot stale
         rather than blocking a scan.
  [P3-3] News: ForexFactory weekly JSON feed (cached 4h) replaces HTML
         scraping (bs4 no longer required). Improved heuristic fallback +
         one Telegram alert per day when the feed is unavailable.
  [P3-4] DXY bias cached (CACHE_TTL_DXY was defined but unused in v9).
  [P3-5] bot_state persisted/loaded with ONE bulk request (v9: 19 calls).
PHASE 4 — Shadow mode
  [P4-1] Setups that pass all 5 gates with SHADOW_SCORE_MIN <= score <
         threshold are NOT sent to Telegram; they are logged to
         `shadow_signals` and tracked virtually with the same management
         (TP1 partial, BE, trailing) so their real R-expectancy is known.
         /shadow command + Sunday report summarise results by score bucket.

========================================================================
SUPABASE MIGRATION — run once in the SQL editor (safe to re-run)
========================================================================
-- 1) New optional columns on `trades` (bot works without them: it detects
--    the missing columns and saves without them, and sends one alert).
alter table trades add column if not exists orig_sl          numeric;
alter table trades add column if not exists r_multiple       numeric;
alter table trades add column if not exists fvg_touch_count  integer;
alter table trades add column if not exists strategy_version text;

-- 2) Shadow signals table
create table if not exists shadow_signals (
  id                 bigserial primary key,
  created_at         timestamptz not null default now(),
  strategy_version   text,
  pair               text,
  direction          text,
  session            text,
  regime             text,
  score              numeric,
  threshold          numeric,
  confidence         text,
  entry              numeric,
  sl                 numeric,
  tp1                numeric,
  tp2                numeric,
  sl_size_pips       numeric,
  fvg_top            numeric,
  fvg_bottom         numeric,
  fvg_touch_count    integer,
  sweep_price        numeric,
  has_ob             boolean,
  near_htf_ob        boolean,
  htf_strength       text,
  vp_confluence      boolean,
  in_killzone        boolean,
  killzone_name      text,
  weekly_confluence  boolean,
  dxy_confirms       boolean,
  news_impact        text,
  sig_hash           text,
  result             text default 'OPEN',
  exit_price         numeric,
  r_multiple         numeric,
  pips               numeric,
  partial_tp_hit     boolean default false,
  duration_hrs       numeric,
  max_adverse_pips   numeric,
  max_favorable_pips numeric,
  closed_at          timestamptz
);
create index if not exists shadow_signals_created_idx on shadow_signals (created_at);

Note: rows written by v9 keep their old `pips`/`rr` semantics. Rows written
by v9.1 store BLENDED pips in `pips` and the R-multiple in `rr`.
Filter analytics with strategy_version = 'v9.2' for the demo statistics.

Environment variables (all optional):
  TD_CREDITS_PER_MIN   Twelve Data credits per minute (default 8 = free tier)
  SHADOW_ENABLED       "1" (default) / "0"
  NEWS_FEED_URL        override the ForexFactory JSON URL
  ENABLED_PAIRS        v9.2: comma list, e.g. "EURUSD,GBPUSD,XAUUSD" (default all)
"""

import copy
import hashlib
import sys
import html
import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,   # Railway tags stderr lines as "error"; INFO belongs on stdout
)
log = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 1 — ENVIRONMENT & GLOBAL CONFIG
# ═════════════════════════════════════════════════════════════════════════════

STRATEGY_VERSION = "v9.2"   # v9.2.1 hotfix: matched-minute gold basis

TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
SUPABASE_URL        = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY        = os.environ.get("SUPABASE_KEY", "")

# [P3-1] Wall-clock aligned scanning
SCAN_INTERVAL       = 15 * 60
SCAN_OFFSET_SEC     = 20          # scan 20s after each quarter-hour (candle close)
HTF_CATCHUP_OFFSET_SEC = 7 * 60 + 20   # [P3-6] HTF catch-up at :07:20/:22:20/:37:20/:52:20
BD_OFFSET           = timedelta(hours=6)
SENT_SIGNALS_TTL    = 24 * 3600

# [P3-2] Twelve Data credit limiter + candle-slot refresh cadence
TD_CREDITS_PER_MIN  = int(os.environ.get("TD_CREDITS_PER_MIN", "8"))
INTERVAL_MINUTES = {"1week": 10080, "1day": 1440, "4h": 240, "1h": 60, "15min": 15}
# How often each interval is re-fetched (minutes, epoch-aligned). Mirrors the
# v9 TTLs, but aligned so a new candle is picked up right at its boundary.
REFRESH_MINUTES  = {"1week": 240, "1day": 120, "4h": 60, "1h": 15, "15min": 15}
CACHE_TTL_DXY    = 30 * 60

PRICE_MONITOR_INTERVAL  = 30
PRICE_MONITOR_MAX_FAILS = 5

SIGNAL_COOLDOWN = 4 * 3600

SESSION_START_UTC = 7
SESSION_END_UTC   = 17

# [P3-3] News
NEWS_FEED_URL       = os.environ.get("NEWS_FEED_URL",
                                     "https://nfs.faireconomy.media/ff_calendar_thisweek.json")
NEWS_FEED_TTL       = 4 * 3600      # refetch cadence (the feed is rate-limited; don't hammer it)
NEWS_RETRY_SEC      = 15 * 60       # retry cadence after a failed fetch
NEWS_FEED_STALE_MAX = 36 * 3600     # older than this → heuristic fallback
NEWS_BLOCK_MINUTES  = 30
NEWS_CURRENCIES     = {"USD", "EUR", "GBP"}

MAX_DAILY_LOSS_PCT   = 2.0
MIN_RISK_PCT         = 0.25         # [P2-4] smallest risk worth sending after scaling
TRAILING_ATR_MULT    = 1.0
PARTIAL_TP_PCT       = 50
BLACKLIST_MIN_LOSSES = 3
BLACKLIST_WR_CEIL    = 30.0

ADX_HIGH_THRESHOLD = 25
ADX_MID_THRESHOLD  = 15
SWING_MIN          = 3
SWING_OK           = 2
VOL_OK_LOW         = 0.8
VOL_OK_HIGH        = 1.2
VOL_HIGH_THRESHOLD = 1.8
OB_FRESH_HOURS     = 16
OB_STALE_HOURS     = 24
OB_EXPIRED_HOURS   = 36          # 4H OB expiry (unchanged)
OB_IMPULSE_MULT    = 1.3
DISP_THRESHOLD     = 1.5
PDH_PDL_LOOKBACK   = 48
VP_BINS            = 20
VP_HVN_THRESHOLD   = 0.15
VP_DIST_MULT       = 1.2
HTF_OB_DIST_MULT   = 1.5
LIQ_DIST_MULT      = 0.5

# [P1-6] HTF OB lifetimes per timeframe (v9 applied the 36h 4H expiry to Daily)
HTF_OB_MAX_AGE_DAILY_H  = 24 * 30        # 30 days
HTF_OB_MAX_AGE_WEEKLY_H = 24 * 7 * 26    # 26 weeks

# ── AMD Sweep parameters (1H-based, LOCKED values) ─────────────────────────
SWEEP_THRESHOLD_MULT = 0.10
SWEEP_LOOKBACK_BARS  = 12
SWEEP_HISTORY_BARS   = 8
SWEEP_WICK_PCT_MIN   = 0.25
SWEEP_BODY_ATR_MIN   = 0.20

# ── AMD gate constants (LOCKED values) ──────────────────────────────────────
DISP_BODY_ATR_MIN   = 0.6
DISP_MAX_WAIT       = 8      # displacement must occur within 8 bars from the sweep (incl.)
FVG_RETEST_MAX_WAIT = 12     # retest must occur within 12 bars after the FVG completes
SL_FVG_BUFFER       = 0.3
RETEST_RUNAWAY_ATR  = 0.2    # [P1-2] max distance of live price beyond the zone edge

# ── DXY ─────────────────────────────────────────────────────────────────────
DXY_CONFIRMS_BONUS  = 1.0
DXY_NEUTRAL_PENALTY = -1.0

# ── [P4-1] Shadow mode ──────────────────────────────────────────────────────
SHADOW_ENABLED     = os.environ.get("SHADOW_ENABLED", "1") == "1"
SHADOW_SCORE_MIN   = 6.5
SHADOW_MAX_OPEN    = 30
SHADOW_MAX_AGE_H   = 7 * 24    # close virtual trades still open after 7 days as EXPIRED


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 2 — PER-PAIR CONFIGURATION (LOCKED)
# ═════════════════════════════════════════════════════════════════════════════

YFINANCE_SYMBOLS: dict[str, str] = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "XAU/USD": "GC=F",        # [A-2] COMEX gold futures — basis-corrected, see [A-3]
    "DXY":     "DX-Y.NYB",
}

# [A-3] Pairs whose yfinance symbol is a proxy (futures) and must be basis-
# corrected to the Twelve Data spot price before use.
PRICE_BASIS_PAIRS = {"XAU/USD"}

_KILLZONES_STD = {
    "London Open":  {"start": 7,  "end": 10},
    "NY Open":      {"start": 13, "end": 16},
    "London Close": {"start": 15, "end": 17},
}

PAIRS_CFG: dict[str, dict] = {
    "EUR/USD": {
        "pair_name":            "EURUSD",
        "pair_flag":            "🇪🇺🇺🇸",
        "pip_mult":             10_000,
        "score_threshold":      7.0,     # [B-6] demo: 7.5 → 7.0
        "score_threshold_weak": 7.0,
        "tp1_multiplier":       1.8,
        "tp2_multiplier":       3.0,
        "cooldown":             SIGNAL_COOLDOWN,
        "base_risk":            1.0,
        "ob_body_pct":          0.50,
        "fvg_min_gap":          3,
        "eq_hl_tol":            0.0003,
        "sl_min_pips":          8,       # [D-13]
        "killzones":            copy.deepcopy(_KILLZONES_STD),
    },
    "GBP/USD": {
        "pair_name":            "GBPUSD",
        "pair_flag":            "🇬🇧🇺🇸",
        "pip_mult":             10_000,
        "score_threshold":      7.0,     # [B-6] demo: 7.5 → 7.0
        "score_threshold_weak": 7.0,
        "tp1_multiplier":       1.8,
        "tp2_multiplier":       3.0,
        "cooldown":             SIGNAL_COOLDOWN,
        "base_risk":            0.8,
        "ob_body_pct":          0.50,
        "fvg_min_gap":          4,
        "eq_hl_tol":            0.0004,
        "sl_min_pips":          8,       # [D-13]
        "killzones":            copy.deepcopy(_KILLZONES_STD),
    },
    "XAU/USD": {                         # [A-1]
        "pair_name":            "XAUUSD",
        "pair_flag":            "🥇🇺🇸",
        "pip_mult":             10,      # 1 pip = $0.10
        "score_threshold":      7.0,
        "score_threshold_weak": 7.0,
        "tp1_multiplier":       1.8,
        "tp2_multiplier":       3.0,
        "cooldown":             SIGNAL_COOLDOWN,
        "base_risk":            0.75,
        "ob_body_pct":          0.50,
        "fvg_min_gap":          20,      # $2.00 ≈ 0.2×H1 ATR — recalibrate after week 1
        "eq_hl_tol":            0.0003,  # relative, works at gold prices
        # [A-5] spread ≤ 10% of risk: SL_min ≥ spread / 0.10.
        # Typical gold spread ≈ $0.30 → $3.00 = 30 pips. Wider spread → raise.
        "sl_min_pips":          30,
        "killzones":            copy.deepcopy(_KILLZONES_STD),
    },
}

ALL_PAIR_KEYS = list(PAIRS_CFG.keys())

def _parse_enabled_pairs(raw: str | None) -> list[str]:
    """
    [G-18] ENABLED_PAIRS="EURUSD,GBPUSD,XAUUSD" (or "EUR/USD,..."). Unknown
    names are ignored with a warning; empty/invalid → all pairs.
    Note: on Railway, changing a variable restarts the service (same build,
    no code redeploy); state is restored from Supabase.
    """
    if not raw or not raw.strip():
        return list(ALL_PAIR_KEYS)
    by_name = {c["pair_name"]: k for k, c in PAIRS_CFG.items()}
    out = []
    for tok in raw.split(","):
        t = tok.strip().upper()
        if not t: continue
        key = by_name.get(t.replace("/", "")) or (t if t in PAIRS_CFG else None)
        if key is None:
            log.warning(f"[Config] ENABLED_PAIRS: unknown pair '{tok.strip()}' ignored")
        elif key not in out:
            out.append(key)
    if not out:
        log.warning("[Config] ENABLED_PAIRS had no valid pair — enabling all")
        return list(ALL_PAIR_KEYS)
    return out

# Pairs that are SCANNED. PAIRS_CFG keeps every pair so open trades of a
# disabled pair are still monitored and reported correctly.
PAIR_KEYS = _parse_enabled_pairs(os.environ.get("ENABLED_PAIRS"))


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 3 — SCORING CONSTANTS (LOCKED — unchanged)
# ═════════════════════════════════════════════════════════════════════════════

SCORE_BASE           = 3.5
SCORE_MARKET_STRONG  =  2.0
SCORE_MARKET_OK      =  1.0
SCORE_MARKET_WEAK    = -1.0
SCORE_VOL_OK         =  0.75
SCORE_VOL_HIGH       = -2.0
SCORE_FVG            =  0.75
SCORE_DISP_BONUS     =  0.5
SCORE_BOS_BONUS      =  1.5
SCORE_OB_BASE        =  2.0
SCORE_OB_FRESH       =  0.75
SCORE_OB_PATTERN     =  0.75
SCORE_OB_STALE       = -0.5
SCORE_SWEEP          =  2.0
SCORE_SWEEP_MISSING  = -2.0
SCORE_MSS            =  1.0
SCORE_CHOCH          =  0.5
SCORE_LIQ_STRONG     =  1.25
SCORE_LIQ_NORMAL     =  0.75
SCORE_WEEKLY_CONF    =  1.5
SCORE_KILLZONE       =  1.5
SCORE_HTF_STRONG     =  2.0
SCORE_HTF_MODERATE   =  1.25
SCORE_VP             =  1.0
SCORE_M15_CONFIRM    =  0.75
SCORE_REJECTION      =  0.75
SCORE_AGGRESSIVE     =  0.25
SCORE_DXY_CONFIRMS   =  1.0
SCORE_DXY_NEUTRAL    = -1.0
SCORE_ADX_LOW        = -1.5
SCORE_RANGE          = -1.5
SCORE_WEAK_TREND     = -0.75
SCORE_WEEKLY_CONFLICT = -1.5     # [G1-1] daily leads against an opposed weekly bias
SCORE_NEWS_HIGH      = -10.0
SCORE_NEWS_MEDIUM    = -2.0


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 4 — THREAD SAFETY & SHARED STATE
# ═════════════════════════════════════════════════════════════════════════════

state_lock        = threading.Lock()
scan_lock         = threading.Lock()
sent_signals_lock = threading.Lock()
cache_lock        = threading.Lock()
news_cache_lock   = threading.Lock()
yf_ticker_lock    = threading.Lock()
td_lock           = threading.Lock()

sent_signals:   dict[str, float] = {}
shadow_sent:    dict[str, float] = {}
_candle_cache:  dict[str, dict]  = {}
_yf_ticker_cache: dict[str, Any] = {}
_td_calls: deque = deque()
_dxy_cache = {"bias": "neutral", "ts": 0.0}
price_monitor_stop = threading.Event()

bot_state: dict[str, Any] = {
    "last_scan_time":    None,
    "next_scan_time":    None,
    "current_session":   "Unknown",
    "is_scanning":       False,
    "today_wins":        0,
    "today_losses":      0,
    "today_pips":        0.0,
    "week_wins":         0,
    "week_losses":       0,
    "week_pips":         0.0,
    "month_wins":        0,
    "month_losses":      0,
    "month_pips":        0.0,
    "total_win_pips":    0.0,
    "total_loss_pips":   0.0,
    "last_signal":       {k: 0 for k in PAIR_KEYS},
    "open_trades":       {},
    "shadow_open":       {},        # [P4-1]
    "pair_stats":        {},
    "daily_risk_used":   0.0,
    "daily_loss_limit":  MAX_DAILY_LOSS_PCT,
    "signal_count":      0,
    "last_regime":       {k: "Unknown" for k in PAIR_KEYS},
    "bad_conditions":    {},
    "trailing_sl":       {},
    "last_price":        {k: None for k in PAIR_KEYS},
    "price_monitor_ok":  False,
    "price_source":      "unknown",
    # [P2-3] persisted schedule markers (v9 kept these in module globals)
    "last_reset_day":          None,
    "last_reset_week":         None,
    "last_reset_month":        None,
    "last_summary_day":        None,
    "last_weekly_report_day":  None,
    "last_monthly_report_day": None,
}


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 5 — SIGNAL DEDUPLICATION (FVG-zone hash — LOCKED)
# ═════════════════════════════════════════════════════════════════════════════

def _make_signal_hash(pair, direction, fvg_top, fvg_bottom):
    """Hash keyed on the FVG zone (stable across scans). TTL 24h."""
    raw = f"{pair}|{direction}|{round(fvg_top,5)}|{round(fvg_bottom,5)}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]

def register_signal(sig_key, store=None):
    store = sent_signals if store is None else store
    with sent_signals_lock:
        store[sig_key] = time.time()

def is_duplicate_signal(sig_key, store=None):
    store = sent_signals if store is None else store
    with sent_signals_lock:
        ts = store.get(sig_key)
        if ts is None: return False
        if time.time() - ts > SENT_SIGNALS_TTL:
            del store[sig_key]; return False
        return True

def purge_expired_signals():
    cutoff = time.time() - SENT_SIGNALS_TTL
    n = 0
    with sent_signals_lock:
        for store in (sent_signals, shadow_sent):
            expired = [k for k, ts in store.items() if ts < cutoff]
            for k in expired: del store[k]
            n += len(expired)
    if n: log.info(f"[Dedup] Purged {n} expired entries")


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 6 — TIME HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _parse_ts(s) -> datetime | None:
    """Parse Twelve Data / ISO timestamps (with or without offset) → aware UTC."""
    if not s: return None
    try:
        dt = datetime.fromisoformat(str(s).strip().replace(" ", "T"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def split_closed(candles: list, interval: str) -> tuple[list, dict | None]:
    """
    [P1-2] Repainting prevention.
    Twelve Data returns the still-forming candle as the last element. Returns
    (closed_candles, forming_candle_or_None). If the provider has not yet
    published the new bar (just after a boundary), every candle is closed.
    """
    if not candles: return [], None
    mins = INTERVAL_MINUTES.get(interval)
    t = _parse_ts(candles[-1].get("time"))
    if mins is None or t is None:
        return candles[:-1], candles[-1]
    if t + timedelta(minutes=mins) <= datetime.now(timezone.utc):
        return list(candles), None
    return candles[:-1], candles[-1]

def _trade_open_ts(trade: dict) -> float:
    ts = trade.get("open_ts")
    if ts: return float(ts)
    dt = _parse_ts(trade.get("time"))
    return dt.timestamp() if dt else time.time()


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 7 — SUPABASE
# ═════════════════════════════════════════════════════════════════════════════

# New columns written only when the migration has been applied. If Supabase
# rejects them, the bot retries without them and alerts once (schema-safe).
# v9.2: two independent tiers, so a missing v9.2 column never drops the
# v9.1 columns (and vice versa).
EXT_TRADE_COLS_V91 = {"orig_sl", "r_multiple", "fvg_touch_count", "strategy_version"}
EXT_TRADE_COLS_V92 = {"weekly_conflict"}
EXT_TRADE_COLS     = EXT_TRADE_COLS_V91 | EXT_TRADE_COLS_V92
EXT_SHADOW_COLS_V92 = {"weekly_conflict"}
_sb_ext_ok = {"trades_v91": True, "trades_v92": True, "shadow_v92": True}
_shadow_table_ok = True
_sb_warned: set[str] = set()

def _sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}

def _sb_ready():
    return bool(SUPABASE_URL and SUPABASE_KEY)

def _sb_warn_once(key, msg):
    if key in _sb_warned: return
    _sb_warned.add(key)
    log.warning(f"[Supabase] {msg}")
    send_telegram(f"<b>VortexAlpha — Database Notice</b>\n{html.escape(msg)}")

def _is_missing_column_error(res) -> bool:
    txt = (res.text or "")
    return res.status_code == 400 and ("PGRST204" in txt or "column" in txt.lower())

def _is_missing_table_error(res) -> bool:
    txt = (res.text or "")
    return res.status_code == 404 or "PGRST205" in txt or "42P01" in txt

def _strip(row: dict, keys: set) -> dict:
    return {k: v for k, v in row.items() if k not in keys}

def _trades_excluded_cols() -> set:
    ex = set()
    if not _sb_ext_ok["trades_v91"]: ex |= EXT_TRADE_COLS_V91
    if not _sb_ext_ok["trades_v92"]: ex |= EXT_TRADE_COLS_V92
    return ex

def _sb_send_trades(method, url, row, headers):
    """
    POST/PATCH to `trades`. If Supabase reports a missing column, the tier
    that owns it (v9.2 first, then v9.1) is disabled and the request retried.
    """
    res = None
    for _ in range(3):
        body = _strip(row, _trades_excluded_cols())
        res = requests.request(method, url, headers=headers, json=body, timeout=10)
        if not _is_missing_column_error(res):
            return res
        txt = res.text or ""
        if _sb_ext_ok["trades_v92"] and (any(c in txt for c in EXT_TRADE_COLS_V92)
                                         or not any(c in txt for c in EXT_TRADE_COLS_V91)):
            _sb_ext_ok["trades_v92"] = False
            _sb_warn_once("ext_trades_v92",
                          "trades table is missing the v9.2 column weekly_conflict. Saving "
                          "without it — run the v9.2 migration SQL from the file header.")
        elif _sb_ext_ok["trades_v91"]:
            _sb_ext_ok["trades_v91"] = False
            _sb_warn_once("ext_trades",
                          "trades table is missing the v9.1 columns (orig_sl, r_multiple, "
                          "fvg_touch_count, strategy_version). Saving without them — run the "
                          "migration SQL from the file header.")
        else:
            return res
    return res

def sb_get_state(key):
    if not _sb_ready(): return None
    try:
        res = requests.get(f"{SUPABASE_URL}/rest/v1/bot_state?key=eq.{key}&select=value",
                           headers=_sb_headers(), timeout=10)
        if res.status_code == 200:
            data = res.json(); return data[0].get("value") if data else None
        return None
    except Exception as e: log.error(f"[Supabase] get_state error: {e}"); return None

def sb_get_state_bulk(keys: list[str]) -> dict:
    """[P3-5] Load every bot_state key in ONE request."""
    if not _sb_ready(): return {}
    try:
        in_list = ",".join(keys)
        res = requests.get(f"{SUPABASE_URL}/rest/v1/bot_state?key=in.({in_list})&select=key,value",
                           headers=_sb_headers(), timeout=15)
        if res.status_code == 200:
            return {r["key"]: r.get("value") for r in res.json()}
        log.warning(f"[Supabase] bulk get failed {res.status_code}, falling back per key")
    except Exception as e:
        log.warning(f"[Supabase] bulk get exception: {e}, falling back per key")
    out = {}
    for k in keys:
        v = sb_get_state(k)
        if v is not None: out[k] = v
    return out

def sb_set_state(key, value):
    if not _sb_ready(): return False
    try:
        row = {"key": key, "value": value,
               "updated_at": datetime.now(timezone.utc).isoformat()}
        res = requests.post(f"{SUPABASE_URL}/rest/v1/bot_state",
                            headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates"},
                            json=row, timeout=10)
        return res.status_code in [200, 201, 204]
    except Exception as e: log.error(f"[Supabase] set_state error: {e}"); return False

def sb_set_state_bulk(mapping: dict) -> bool:
    """[P3-5] Upsert all state keys in ONE request (fallback: per key)."""
    if not _sb_ready(): return False
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = [{"key": k, "value": v, "updated_at": now_iso} for k, v in mapping.items()]
    try:
        res = requests.post(f"{SUPABASE_URL}/rest/v1/bot_state",
                            headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates"},
                            json=rows, timeout=15)
        if res.status_code in [200, 201, 204]: return True
        log.warning(f"[Supabase] bulk upsert failed {res.status_code}: {res.text[:150]}")
    except Exception as e:
        log.warning(f"[Supabase] bulk upsert exception: {e}")
    ok = True
    for k, v in mapping.items():
        ok = sb_set_state(k, v) and ok
    return ok

def sb_insert_trade(trade_data):
    if not _sb_ready(): return None
    try:
        now = datetime.now(timezone.utc)
        pm  = PAIRS_CFG.get(trade_data.get("pair_key", PAIR_KEYS[0]), PAIRS_CFG[PAIR_KEYS[0]])["pip_mult"]

        entry   = trade_data.get("entry", 0)
        sl      = trade_data.get("sl", 0)
        sl_pips = round(abs(entry - sl) * pm, 1) if entry and sl else None

        fvg_top = trade_data.get("fvg_top")
        fvg_bot = trade_data.get("fvg_bottom")
        fvg_size_pips = round(abs(fvg_top - fvg_bot) * pm, 1) if fvg_top and fvg_bot else None

        row = {
            "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M UTC"),
            "pair": trade_data.get("pair_name",""), "direction": trade_data.get("direction",""),
            "session": trade_data.get("session",""), "regime": trade_data.get("regime",""),
            "bias": trade_data.get("bias",""), "signal_type": trade_data.get("signal_type",""),
            "score": trade_data.get("score",0), "confidence": trade_data.get("confidence","B"),
            "entry": entry, "sl": sl,
            "tp1": trade_data.get("tp1",0), "tp2": trade_data.get("tp2",0),
            "result": "OPEN", "exit_price": None, "pips": None, "rr": None,
            "duration_hrs": None, "tp_hit": None,
            "has_ob": trade_data.get("has_ob",False),
            "ob_age": trade_data.get("ob_age"),
            "ob_pattern": trade_data.get("ob_pattern","none"),
            "near_htf_ob": trade_data.get("near_htf_ob",False),
            "htf_strength": trade_data.get("htf_strength",""),
            "vp_confluence": trade_data.get("vp_confluence",False),
            "vp_label": trade_data.get("vp_label",""),
            "sweep_confirmed": trade_data.get("sweep_confirmed",False),
            "sweep_price": trade_data.get("sweep_price"),
            "sweep_pierce_atr": trade_data.get("sweep_pierce_atr"),
            "disp_confirmed": trade_data.get("disp_confirmed",False),
            "fvg_top": fvg_top,
            "fvg_bottom": fvg_bot,
            "fvg_size_pips": fvg_size_pips,
            "mss_confirmed": trade_data.get("mss_confirmed",False),
            "choch_confirmed": trade_data.get("choch_confirmed",False),
            "liq_zone": trade_data.get("liq_zone",False),
            "liq_strength": trade_data.get("liq_strength","normal"),
            "weekly_confluence": trade_data.get("weekly_confluence",False),
            "in_killzone": trade_data.get("in_killzone",False),
            "killzone_name": trade_data.get("killzone_name",""),
            "dxy_confirms": trade_data.get("dxy_confirms",False),
            "dxy_bias": trade_data.get("dxy_bias","neutral"),
            "entry_type": trade_data.get("entry_type",""),
            "risk_pct": trade_data.get("risk_pct",0),
            "position_size": trade_data.get("position_size",100),
            "news_impact": trade_data.get("news_impact","LOW"),
            "partial_tp_hit": False, "partial_tp_price": None,
            "trailing_sl_active": False,
            "sl_size_pips": sl_pips,
            "max_adverse_pips": 0.0,
            "max_favorable_pips": 0.0,
            "loss_reason": None,
            "close_note": None,
            # v9.1 extended columns (optional, see migration)
            "orig_sl": trade_data.get("orig_sl"),
            "r_multiple": None,
            "fvg_touch_count": trade_data.get("fvg_touch_count"),
            "strategy_version": STRATEGY_VERSION,
            "weekly_conflict": bool(trade_data.get("weekly_conflict", False)),   # [G1-1]
        }
        res = _sb_send_trades("POST", f"{SUPABASE_URL}/rest/v1/trades", row,
                              {**_sb_headers(), "Prefer": "return=representation"})
        if res.status_code in [200, 201]:
            data = res.json(); tid = data[0].get("id") if data else None
            log.info(f"[Supabase] Trade saved ID:{tid} sl={sl_pips}pips fvg={fvg_size_pips}pips")
            return tid
        log.error(f"[Supabase] Insert error: {res.text[:200]}"); return None
    except Exception as e: log.error(f"[Supabase] Insert exception: {e}"); return None

def _analyze_loss_reason(trade: dict, duration_hrs: float) -> str:
    """Loss reason code for ML training data (unchanged)."""
    sl_pips = trade.get("sl_size_pips", 0) or 0
    news    = trade.get("news_impact", "LOW")
    if sl_pips < 8:        return "SL_TOO_TIGHT"
    if duration_hrs < 2:   return "NOISE_HIT"
    if news == "MEDIUM":   return "NEWS_SPIKE"
    return "NORMAL_LOSS"

def sb_update_result(trade_id, result, exit_price, pips, rr, duration_hrs,
                     tp_hit, partial_tp_price=None, trailing_active=False,
                     trade_dict=None, max_adverse=None, max_favorable=None,
                     orig_sl=None):
    if not _sb_ready() or not trade_id: return False
    try:
        row = {
            "result":             result,
            "exit_price":         exit_price,
            "pips":               pips,          # [P2-1] BLENDED pips (whole position)
            "rr":                 rr,            # [P2-1] R-multiple vs ORIGINAL risk
            "duration_hrs":       round(duration_hrs, 1),
            "tp_hit":             tp_hit,
            "partial_tp_price":   partial_tp_price,
            "trailing_sl_active": trailing_active,
            "duration_to_sl_hrs": round(duration_hrs, 2) if result == "LOSS" else None,
            "r_multiple":         rr,
            "orig_sl":            orig_sl,
        }
        if max_adverse is not None:
            row["max_adverse_pips"]   = round(max_adverse, 1)
        if max_favorable is not None:
            row["max_favorable_pips"] = round(max_favorable, 1)
        if result == "LOSS" and trade_dict:
            row["loss_reason"] = _analyze_loss_reason(trade_dict, duration_hrs)
            log.info(f"[Supabase] Loss reason: {row['loss_reason']}")
        res = _sb_send_trades("PATCH", f"{SUPABASE_URL}/rest/v1/trades?id=eq.{trade_id}",
                              row, _sb_headers())
        if res.status_code in [200, 204]:
            log.info(f"[Supabase] Result updated ID:{trade_id} → {result} ({rr}R)"); return True
        log.error(f"[Supabase] Update error: {res.text[:200]}"); return False
    except Exception as e: log.error(f"[Supabase] Update exception: {e}"); return False

def sb_update_trailing_sl(trade_id, new_sl):
    if not _sb_ready() or not trade_id: return False
    try:
        res = requests.patch(f"{SUPABASE_URL}/rest/v1/trades?id=eq.{trade_id}",
                             headers=_sb_headers(),
                             json={"sl": new_sl, "trailing_sl_active": True}, timeout=10)
        return res.status_code in [200, 204]
    except Exception as e: log.error(f"[Supabase] Trailing SL exception: {e}"); return False

def sb_mark_partial_tp(trade_id, partial_price):
    if not _sb_ready() or not trade_id: return False
    try:
        res = requests.patch(f"{SUPABASE_URL}/rest/v1/trades?id=eq.{trade_id}",
                             headers=_sb_headers(),
                             json={"partial_tp_hit": True, "partial_tp_price": partial_price},
                             timeout=10)
        return res.status_code in [200, 204]
    except Exception as e: log.error(f"[Supabase] Partial TP exception: {e}"); return False

def sb_insert_journal(entry):
    if not _sb_ready(): return False
    try:
        res = requests.post(f"{SUPABASE_URL}/rest/v1/daily_journal",
                            headers=_sb_headers(), json=entry, timeout=10)
        return res.status_code in [200, 201]
    except Exception as e: log.error(f"[Supabase] Journal insert exception: {e}"); return False

def sb_fetch_weekly_losses():
    if not _sb_ready(): return []
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        url = (f"{SUPABASE_URL}/rest/v1/trades?result=eq.LOSS&date=gte.{since}"
               f"&select=pair,direction,regime,score,session,killzone_name,"
               f"news_impact,pips,rr,entry_type,in_killzone,"
               f"near_htf_ob,vp_confluence,sweep_confirmed,disp_confirmed,"
               f"mss_confirmed,weekly_confluence,confidence,ob_pattern,dxy_confirms"
               f"&order=date.desc")
        res = requests.get(url, headers=_sb_headers(), timeout=15)
        return res.json() if res.status_code == 200 else []
    except Exception as e: log.error(f"[Supabase] Weekly losses exception: {e}"); return []

def sb_fetch_monthly_trades():
    if not _sb_ready(): return []
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        url = (f"{SUPABASE_URL}/rest/v1/trades?date=gte.{since}"
               f"&select=pair,result,pips,rr,score,regime,session,"
               f"direction,risk_pct,confidence,partial_tp_hit,trailing_sl_active"
               f"&order=date.desc")
        res = requests.get(url, headers=_sb_headers(), timeout=15)
        return res.json() if res.status_code == 200 else []
    except Exception as e: log.error(f"[Supabase] Monthly trades exception: {e}"); return []

# ── [P4-1] Shadow signals table ─────────────────────────────────────────────

def sb_insert_shadow(row: dict):
    global _shadow_table_ok
    if not _sb_ready() or not _shadow_table_ok: return None
    if not _sb_ext_ok["shadow_v92"]:
        row = _strip(row, EXT_SHADOW_COLS_V92)
    try:
        res = requests.post(f"{SUPABASE_URL}/rest/v1/shadow_signals",
                            headers={**_sb_headers(), "Prefer": "return=representation"},
                            json=row, timeout=10)
        if res.status_code in [200, 201]:
            data = res.json(); return data[0].get("id") if data else None
        if _sb_ext_ok["shadow_v92"] and _is_missing_column_error(res):
            _sb_ext_ok["shadow_v92"] = False
            _sb_warn_once("ext_shadow_v92",
                          "shadow_signals is missing the v9.2 column weekly_conflict. Saving "
                          "without it — run the v9.2 migration SQL from the file header.")
            res = requests.post(f"{SUPABASE_URL}/rest/v1/shadow_signals",
                                headers={**_sb_headers(), "Prefer": "return=representation"},
                                json=_strip(row, EXT_SHADOW_COLS_V92), timeout=10)
            if res.status_code in [200, 201]:
                data = res.json(); return data[0].get("id") if data else None
        if _is_missing_table_error(res):
            _shadow_table_ok = False
            _sb_warn_once("shadow_table", "shadow_signals table not found — shadow mode is "
                                          "paused. Run the migration SQL from the file header "
                                          "and redeploy.")
            return None
        log.error(f"[Supabase] Shadow insert error: {res.text[:200]}"); return None
    except Exception as e: log.error(f"[Supabase] Shadow insert exception: {e}"); return None

def sb_update_shadow(shadow_id, fields: dict) -> bool:
    if not _sb_ready() or not shadow_id or not _shadow_table_ok: return False
    try:
        res = requests.patch(f"{SUPABASE_URL}/rest/v1/shadow_signals?id=eq.{shadow_id}",
                             headers=_sb_headers(), json=fields, timeout=10)
        return res.status_code in [200, 204]
    except Exception as e: log.error(f"[Supabase] Shadow update exception: {e}"); return False

def sb_fetch_shadow(days=30) -> list:
    if not _sb_ready() or not _shadow_table_ok: return []
    try:
        # 'Z' form avoids a '+' in the query string being decoded as a space
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = (f"{SUPABASE_URL}/rest/v1/shadow_signals?created_at=gte.{since}"
               f"&select=pair,score,result,r_multiple&order=created_at.desc")
        res = requests.get(url, headers=_sb_headers(), timeout=15)
        return res.json() if res.status_code == 200 else []
    except Exception as e: log.error(f"[Supabase] Shadow fetch exception: {e}"); return []

# ── State persistence ───────────────────────────────────────────────────────

_PERSIST_KEYS = [
    "open_trades","last_signal","pair_stats",
    "today_wins","today_losses","today_pips",
    "week_wins","week_losses","week_pips",
    "month_wins","month_losses","month_pips",
    "signal_count","daily_risk_used",
    "bad_conditions","total_win_pips","total_loss_pips",
    "trailing_sl","last_regime",
    # [P2-3] restart-safety: schedule markers + shadow trades
    "last_reset_day","last_reset_week","last_reset_month",
    "last_summary_day","last_weekly_report_day","last_monthly_report_day",
    "shadow_open",
]
# [P2-3] dedup stores persisted too (v9 lost them on restart → duplicate risk)
_PERSIST_EXTRA = ["sent_signals", "shadow_sent"]

def load_persisted_state():
    log.info("[Supabase] Loading persisted state...")
    data = sb_get_state_bulk(_PERSIST_KEYS + _PERSIST_EXTRA)
    with state_lock:
        for key in _PERSIST_KEYS:
            if data.get(key) is not None:
                bot_state[key] = data[key]
                log.info(f"  Loaded: {key}")
        # [P2-3] First run of v9.1 (markers missing): assume counters loaded from
        # v9 already belong to today. This is the conservative choice — it never
        # wipes a partially-used daily loss limit.
        today_bd = (datetime.now(timezone.utc) + BD_OFFSET).date().isoformat()
        for k in ("last_reset_day", "last_reset_week", "last_reset_month"):
            if not bot_state.get(k):
                bot_state[k] = today_bd
                log.info(f"  {k} missing → initialised to {today_bd} (no reset)")
        if not isinstance(bot_state.get("shadow_open"), dict):
            bot_state["shadow_open"] = {}
    with sent_signals_lock:
        for key, store in (("sent_signals", sent_signals), ("shadow_sent", shadow_sent)):
            v = data.get(key)
            if isinstance(v, dict):
                store.update({k: float(ts) for k, ts in v.items()})

def save_persisted_state():
    with state_lock:
        snapshot = copy.deepcopy({k: bot_state[k] for k in _PERSIST_KEYS})
    with sent_signals_lock:
        snapshot["sent_signals"] = dict(sent_signals)
        snapshot["shadow_sent"]  = dict(shadow_sent)
    sb_set_state_bulk(snapshot)


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 8 — TELEGRAM (unchanged transport layer)
# ═════════════════════════════════════════════════════════════════════════════

def send_telegram(message, reply_markup=None, retries=3):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("[Telegram] Not configured — message suppressed"); return False
    for attempt in range(retries):
        try:
            payload: dict[str, Any] = {"chat_id": TELEGRAM_CHAT_ID, "text": message,
                                        "parse_mode": "HTML"}
            if reply_markup: payload["reply_markup"] = json.dumps(reply_markup)
            res = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json=payload, timeout=10)
            if res.status_code == 200: log.info("[Telegram] Message sent"); return True
            if res.status_code == 429:
                wait = 2 ** attempt; log.warning(f"[Telegram] Rate limit – wait {wait}s")
                time.sleep(wait); continue
            log.error(f"[Telegram] Error {res.status_code}: {res.text[:100]}"); return False
        except Exception as e:
            log.error(f"[Telegram] Exception: {e}")
            if attempt < retries - 1: time.sleep(2 ** attempt)
    return False

def answer_callback(cid):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                      json={"callback_query_id": cid}, timeout=5)
    except Exception: pass

def main_menu_buttons():
    return {"inline_keyboard": [
        [{"text": "Status", "callback_data": "status"},
         {"text": "Signals", "callback_data": "signals"}],
        [{"text": "Performance", "callback_data": "performance"},
         {"text": "Journal", "callback_data": "journal"}],
        [{"text": "Risk", "callback_data": "risk"},
         {"text": "Shadow", "callback_data": "shadow"}],
        [{"text": "Help", "callback_data": "help"}],
    ]}


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 9 — MARKET DATA  [P3-2]
# ═════════════════════════════════════════════════════════════════════════════

def _td_acquire(block=True) -> bool:
    """
    Sliding-window limiter for Twelve Data credits. Replaces v9's fixed 60s
    `_wait_for_next_minute()` — waits only when the budget is actually spent.
    """
    while True:
        with td_lock:
            now = time.time()
            while _td_calls and now - _td_calls[0] >= 60.5:
                _td_calls.popleft()
            if len(_td_calls) < TD_CREDITS_PER_MIN:
                _td_calls.append(now); return True
            wait = 60.5 - (now - _td_calls[0])
        if not block: return False
        log.info(f"[API] Credit budget reached – waiting {wait:.1f}s")
        time.sleep(max(0.5, wait))

def fetch_candles(symbol, interval, count=100, acquire=True):
    if acquire: _td_acquire(block=True)
    # timezone=UTC makes candle timestamps unambiguous for age/closed checks
    url = (f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}"
           f"&outputsize={count}&timezone=UTC&apikey={TWELVE_DATA_API_KEY}&format=JSON")
    try:
        res = requests.get(url, timeout=15); data = res.json()
        if data.get("status") == "error":
            log.error(f"[API] {symbol} {interval}: {data.get('message')}"); return []
        candles = []
        for v in reversed(data.get("values",[])):
            candles.append({"time": v["datetime"], "open": float(v["open"]),
                             "high": float(v["high"]), "low": float(v["low"]),
                             "close": float(v["close"]),
                             "volume": float(v.get("volume",0) or 0)})
        return candles
    except Exception as e: log.error(f"[API] Fetch error {symbol} {interval}: {e}"); return []

def fetch_with_retry(symbol, interval, count=100, retries=3):
    for attempt in range(retries):
        result = fetch_candles(symbol, interval, count)
        if result: return result
        wait = 2 ** attempt
        log.warning(f"[API] Retry {attempt+1}/{retries} {symbol} {interval} – wait {wait}s")
        time.sleep(wait)
    return []

def _slot_id(interval, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    mins = REFRESH_MINUTES.get(interval, 15)
    return int(now.timestamp() // 60) // mins

def fetch_slot_cached(symbol, interval, count, critical=True):
    """
    [P3-2] Candle-slot cache. Data is refreshed once per epoch-aligned slot,
    so a new candle is picked up right at its boundary (v9's TTLs drifted
    against candle closes). Non-critical (HTF) data is served one slot
    stale instead of blocking the scan when the credit budget is spent.
    """
    key = f"{symbol}_{interval}"; slot = _slot_id(interval)
    with cache_lock:
        entry = _candle_cache.get(key)
    if entry and entry["slot"] == slot and entry["data"]:
        log.info(f"[Cache] HIT  {key}"); return entry["data"]
    if entry and entry["data"] and not critical:
        if not _td_acquire(block=False):
            log.info(f"[Cache] STALE {key} (credit budget) – using previous slot")
            return entry["data"]
        data = fetch_candles(symbol, interval, count, acquire=False)
    else:
        log.info(f"[Cache] MISS {key} – fetching")
        data = fetch_with_retry(symbol, interval, count)
    if data:
        with cache_lock:
            _candle_cache[key] = {"data": data, "slot": slot, "ts": time.time()}
        return data
    return entry["data"] if entry else []

def _get_yf_ticker(yf_sym):
    with yf_ticker_lock:
        if yf_sym in _yf_ticker_cache: return _yf_ticker_cache[yf_sym]
    try:
        import yfinance as yf
        ticker = yf.Ticker(yf_sym)
        with yf_ticker_lock: _yf_ticker_cache[yf_sym] = ticker
        return ticker
    except ImportError: log.warning("[Price] yfinance not installed"); return None
    except Exception as e: log.warning(f"[Price] yfinance Ticker creation failed {yf_sym}: {e}"); return None

def fetch_bars_yfinance(symbol) -> list[tuple] | None:
    """
    [P2-2] Returns recent 1m bars as (open_ts, high, low, close) so the monitor
    can see wicks between 30s polls (v9 saw only the last price).
    """
    yf_sym = YFINANCE_SYMBOLS.get(symbol, symbol)
    ticker = _get_yf_ticker(yf_sym)
    if ticker is None: return None
    try:
        data = ticker.history(period="1d", interval="1m")
        if data is None or data.empty: return None
        data = data.tail(180)
        bars = []
        for ts, row in data.iterrows():
            try:
                bars.append((ts.timestamp(), float(row["High"]), float(row["Low"]), float(row["Close"])))
            except Exception:
                continue
        return bars or None
    except Exception as e:
        log.warning(f"[Price] yfinance {symbol} failed: {e}")
        with yf_ticker_lock: _yf_ticker_cache.pop(yf_sym, None)
        return None

def fetch_price_twelvedata(symbol):
    if not TWELVE_DATA_API_KEY: return None
    if not _td_acquire(block=False):      # never block the monitor on credits
        return None
    try:
        res = requests.get(
            f"https://api.twelvedata.com/price?symbol={symbol}&apikey={TWELVE_DATA_API_KEY}",
            timeout=8)
        data = res.json()
        return float(data["price"]) if "price" in data else None
    except Exception as e: log.warning(f"[Price] TwelveData {symbol} failed: {e}"); return None

# ── [A-3][A-4] Basis-corrected price feed (XAU/USD: GC=F futures → spot) ────
BASIS_REFRESH_SEC     = 15 * 60     # monitor-side TD refresh cadence (≤ 4 credits/hour)
BASIS_STALE_WARN_SEC  = 6 * 3600    # older basis still used, but logged
BASIS_ALERT_USD       = 2.00        # [A-4] sanity threshold
BASIS_SCAN_MAX_AGE    = 120         # TD candle must be ≤ 2 min old to measure basis
basis_lock  = threading.Lock()
_basis: dict[str, dict] = {}        # pair → {"basis", "ts", "spot", "fut", "source"}
_basis_td_last: dict[str, float] = {}
_basis_alert_day: dict[str, str] = {}

def _basis_alert_once(pair_key: str, deviation: float, spot: float, corrected: float):
    today = datetime.now(timezone.utc).date().isoformat()
    with basis_lock:
        if _basis_alert_day.get(pair_key) == today: return
        _basis_alert_day[pair_key] = today
    send_telegram(f"<b>VortexAlpha — Price Basis Alert ({PAIRS_CFG[pair_key]['pair_name']})</b>\n"
                  f"Basis-corrected futures price deviates from spot by "
                  f"<b>${deviation:.2f}</b> (limit ${BASIS_ALERT_USD:.2f}).\n"
                  f"Spot {spot:.2f} vs corrected {corrected:.2f}.\n"
                  f"SL/TP monitoring may be less precise until the next scan re-measures it. "
                  f"(Possible causes: futures roll, delayed Yahoo feed, fast market.)")

def update_basis_value(pair_key: str, basis: float, source: str, spot: float = 0.0,
                       fut: float = 0.0, info: str = "", yahoo_delay_min: float | None = None
                       ) -> float | None:
    """
    Store a basis (spot − futures). Returns the sanity deviation vs the
    PREVIOUS basis (= |fut + old_basis − spot|), or None on first measurement.
    """
    now = time.time()
    with basis_lock:
        prev = _basis.get(pair_key)
        _basis[pair_key] = {"basis": basis, "ts": now, "spot": spot, "fut": fut,
                            "source": source, "yahoo_delay_min": yahoo_delay_min}
    tag = f" {info}" if info else ""
    if prev is None:
        log.info(f"[Basis] {pair_key} initial basis {basis:+.2f} [{source}]{tag}")
        return None
    deviation = abs(basis - prev["basis"])
    log.info(f"[Basis] {pair_key} basis {basis:+.2f} (prev {prev['basis']:+.2f}, "
             f"age {int((now - prev['ts'])/60)}m) deviation ${deviation:.2f} [{source}]{tag}")
    if deviation > BASIS_ALERT_USD:
        ref = spot if spot else 0.0
        _basis_alert_once(pair_key, deviation, ref, (fut + prev["basis"]) if fut else 0.0)
    return deviation

def update_basis(pair_key: str, spot: float, fut: float, source: str) -> float | None:
    """Unmatched fallback: basis = spot − futures from two 'current' prices."""
    if not spot or not fut or spot <= 0 or fut <= 0: return None
    return update_basis_value(pair_key, spot - fut, source, spot=spot, fut=fut)

# [A-3 fix, v9.2.1] Matched-minute basis. Yahoo's GC=F feed can lag real time,
# so "TD price now − GC=F price now" mixes the basis with the price move during
# the lag (live log 2026-09-29: basis jumped $1–12 every 15 min). Instead the
# basis is the MEDIAN of (TD close − GC=F close) over the most recent 1-minute
# bars that BOTH feeds have closed — same minute, so the lag cancels out.
BASIS_MATCH_MINUTES = 10
BASIS_MATCH_MIN     = 3

def _matched_basis(td_1m: list, yf_bars: list) -> tuple[float | None, int]:
    if not td_1m or not yf_bars: return None, 0
    td = {}
    for c in td_1m:
        t = _parse_ts(c.get("time"))
        if t: td[int(t.timestamp() // 60)] = c["close"]
    yf = {int(b[0] // 60): b[3] for b in yf_bars if b[3] > 0}
    if not td or not yf: return None, 0
    upto = min(max(td), max(yf)) - 1                  # minutes closed on BOTH feeds
    common = sorted(m for m in td if m in yf and m <= upto)[-BASIS_MATCH_MINUTES:]
    if len(common) < BASIS_MATCH_MIN: return None, len(common)
    diffs = sorted(td[m] - yf[m] for m in common)
    k = len(diffs)
    med = diffs[k // 2] if k % 2 else (diffs[k // 2 - 1] + diffs[k // 2]) / 2
    return med, k

def _yahoo_delay_min(yf_bars) -> float | None:
    if not yf_bars: return None
    return max(0.0, (time.time() - (yf_bars[-1][0] + 60)) / 60)   # bar close → now

def _measure_matched(pair_key: str, yf_bars: list, source: str) -> bool:
    """
    1 Twelve Data credit (1min candles), NON-blocking: if the per-minute budget
    is spent, the previous basis is kept (never delays the scan).
    Returns True if a matched basis was stored.
    """
    if not _td_acquire(block=False):
        log.info(f"[Basis] {pair_key} no credit free this minute – keeping previous basis")
        return False
    td_1m = fetch_candles(pair_key, "1min", 30, acquire=False)
    b, n = _matched_basis(td_1m, yf_bars)
    delay = _yahoo_delay_min(yf_bars)
    dtxt = f"yahoo_delay={delay:.1f}m" if delay is not None else "yahoo_delay=?"
    if b is None:
        log.warning(f"[Basis] {pair_key} matched basis unavailable (common minutes={n}, {dtxt})")
        return False
    spot = td_1m[-1]["close"] if td_1m else 0.0
    update_basis_value(pair_key, b, source, spot=spot, fut=yf_bars[-1][3],
                       info=f"n={n} {dtxt}", yahoo_delay_min=delay)
    return True

def get_basis(pair_key: str, yf_bars: list | None = None) -> float | None:
    """
    Current basis. If it is older than BASIS_REFRESH_SEC (e.g. an open trade
    outside the scan window), re-measure with the matched-minute method
    (1 Twelve Data credit, throttled per pair).
    """
    with basis_lock:
        entry = dict(_basis[pair_key]) if pair_key in _basis else None
        last_td = _basis_td_last.get(pair_key, 0.0)
    now = time.time()
    if (entry is None or now - entry["ts"] > BASIS_REFRESH_SEC) and yf_bars \
            and now - last_td >= BASIS_REFRESH_SEC and _td_acquire(block=False):
        with basis_lock:
            _basis_td_last[pair_key] = now
        td_1m = fetch_candles(pair_key, "1min", 30, acquire=False)
        b, n = _matched_basis(td_1m, yf_bars)
        if b is not None:
            d = _yahoo_delay_min(yf_bars)
            update_basis_value(pair_key, b, "monitor/matched",
                               spot=td_1m[-1]["close"], fut=yf_bars[-1][3],
                               info=f"n={n}", yahoo_delay_min=d)
            with basis_lock:
                entry = dict(_basis[pair_key])
    if entry is None: return None
    if now - entry["ts"] > BASIS_STALE_WARN_SEC:
        log.warning(f"[Basis] {pair_key} basis is {int((now-entry['ts'])/3600)}h old")
    return entry["basis"]

def _td_price_throttled(pair_key: str) -> float | None:
    """TD /price for basis pairs, at most once per BASIS_REFRESH_SEC (fallback path)."""
    now = time.time()
    with basis_lock:
        if now - _basis_td_last.get(pair_key, 0.0) < BASIS_REFRESH_SEC: return None
        _basis_td_last[pair_key] = now
    return fetch_price_twelvedata(pair_key)

def _basis_snapshot(symbol) -> tuple[float | None, list | None, str]:
    """[A-3] Spot-equivalent snapshot for a basis pair."""
    bars = fetch_bars_yfinance(symbol)
    if bars and bars[-1][3] > 0:
        b = get_basis(symbol, bars)
        if b is not None:
            adj = [(t, h + b, l + b, c + b) for (t, h, l, c) in bars]
            last = adj[-1][3]
            with state_lock:
                bot_state["last_price"][symbol] = last
                bot_state["price_monitor_ok"]   = True
                bot_state["price_source"]       = "yfinance+basis"
            return last, adj, "yfinance+basis"
    price = _td_price_throttled(symbol)          # never every 30 s
    if price and price > 0:
        with state_lock:
            bot_state["last_price"][symbol] = price
            bot_state["price_monitor_ok"]   = True
            bot_state["price_source"]       = "twelvedata"
        return price, None, "twelvedata"     # no bars → hi/lo = last (same as v9.1)
    return None, None, "none"

def measure_basis_at_scan(pair_key: str, ec: list) -> float | None:
    """
    [A-3] Called every scan for basis pairs, right after the 1H fetch.
    Spot = latest Twelve Data 1H candle close (forming candle if present).
    Returns the spot price to use as Gate-5 live price (or None).
    """
    if not ec: return None
    with cache_lock:
        entry = _candle_cache.get(f"{pair_key}_1h")
    age = time.time() - entry["ts"] if entry else 1e9
    spot = ec[-1]["close"]
    if age > BASIS_SCAN_MAX_AGE:
        log.info(f"[Basis] {pair_key} 1H data {int(age)}s old – basis not re-measured")
        return spot
    bars = fetch_bars_yfinance(pair_key)
    if bars and bars[-1][3] > 0:
        if not _measure_matched(pair_key, bars, "scan/matched"):
            log.warning(f"[Basis] {pair_key} keeping previous basis (no matched minutes)")
    else:
        log.warning(f"[Basis] {pair_key} GC=F unavailable – basis not re-measured")
    return spot

def get_price_snapshot(symbol) -> tuple[float | None, list | None, str]:
    """Returns (last_price, 1m bars or None, source)."""
    if symbol in PRICE_BASIS_PAIRS:               # [A-3] other pairs: unchanged v9.1 path
        return _basis_snapshot(symbol)
    bars = fetch_bars_yfinance(symbol)
    if bars:
        last = bars[-1][3]
        if last > 0:
            with state_lock:
                bot_state["last_price"][symbol] = last
                bot_state["price_monitor_ok"]   = True
                bot_state["price_source"]       = "yfinance"
            return last, bars, "yfinance"
    price = fetch_price_twelvedata(symbol)
    if price and price > 0:
        with state_lock:
            bot_state["last_price"][symbol] = price
            bot_state["price_monitor_ok"]   = True
            bot_state["price_source"]       = "twelvedata"
        return price, [(time.time(), price, price, price)], "twelvedata"
    return None, None, "none"

def get_current_price(symbol):
    price, _, src = get_price_snapshot(symbol)
    return price, src

def _window_hilo(bars, since_ts: float, last: float, strict: bool,
                 last_bar_ts: float | None = None) -> tuple[float, float]:
    """
    High/low of 1m bars since the last check. `strict` (first check of a new
    trade) excludes the bar containing the signal time so pre-entry wicks
    can't trigger SL/TP.
    [v9.2.1] With `last_bar_ts` (newest bar already processed) the window is
    bar-based: every bar from that one onward is included, so bars that a
    lagging feed delivers late are never skipped (the wall-clock window of
    v9.1 dropped them).
    """
    if not bars: return last, last
    if last_bar_ts is not None:
        cutoff = last_bar_ts
    else:
        cutoff = since_ts if strict else since_ts - 60
    sel = [b for b in bars if b[0] >= cutoff]
    if not sel: return last, last
    return max(max(b[1] for b in sel), last), min(min(b[2] for b in sel), last)

def _fetch_dxy_bias():
    try:
        import yfinance as yf
        data = yf.Ticker("DX-Y.NYB").history(period="3mo", interval="1d")
        if data is None or data.empty or len(data) < 20:
            log.warning("[DXY] yfinance: insufficient data"); return "neutral"
        candles = [{"time": str(idx), "open": row.Open, "high": row.High,
                    "low": row.Low, "close": row.Close, "volume": row.Volume}
                   for idx, row in data.iterrows()]
        return get_bias(candles, lookback=50)
    except Exception as e:
        log.warning(f"[DXY] yfinance fetch failed: {e}"); return "neutral"

def get_dxy_bias():
    """[P3-4] Cached for CACHE_TTL_DXY (v9 re-downloaded 3 months every scan)."""
    now = time.time()
    with cache_lock:
        if now - _dxy_cache["ts"] < CACHE_TTL_DXY:
            return _dxy_cache["bias"]
    bias = _fetch_dxy_bias()
    with cache_lock:
        _dxy_cache.update(bias=bias, ts=now)
    log.info(f"[DXY] bias: {bias}")
    return bias

def dxy_confirms_direction(dxy_bias, trade_direction):
    if trade_direction == "BUY"  and dxy_bias == "bearish": return True
    if trade_direction == "SELL" and dxy_bias == "bullish": return True
    return False


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 10 — PRICE MONITOR (live + shadow)
# ═════════════════════════════════════════════════════════════════════════════

def run_price_monitor():
    log.info(f"[PriceMonitor] Thread started (interval={PRICE_MONITOR_INTERVAL}s)")
    with state_lock: bot_state["price_monitor_ok"] = True
    consecutive_failures = 0
    while not price_monitor_stop.is_set():
        try:
            with state_lock:
                live_pairs   = {t["pair_key"] for t in bot_state.get("open_trades", {}).values() if "pair_key" in t}
                shadow_pairs = {t["pair_key"] for t in bot_state.get("shadow_open", {}).values() if "pair_key" in t}
            pairs = live_pairs | shadow_pairs
            if not pairs:
                time.sleep(PRICE_MONITOR_INTERVAL); continue
            any_price_ok = False
            for pair_key in pairs:
                last, bars, source = get_price_snapshot(pair_key)
                if last and last > 0:
                    any_price_ok = True
                    atr_4h = _get_cached_atr(pair_key)
                    if pair_key in live_pairs:
                        check_open_trades_for_pair(pair_key, last, atr_4h, bars)
                    if pair_key in shadow_pairs:
                        check_shadow_trades_for_pair(pair_key, last, atr_4h, bars)
            if not any_price_ok:
                consecutive_failures += 1
                if consecutive_failures == PRICE_MONITOR_MAX_FAILS:
                    send_telegram(f"<b>VortexAlpha {STRATEGY_VERSION} — Price Monitor Alert</b>\n"
                                  f"Price fetch failed {consecutive_failures} consecutive times.",
                                  main_menu_buttons())
            else:
                consecutive_failures = 0
        except Exception as e: log.error(f"[PriceMonitor] Error: {e}", exc_info=True)
        time.sleep(PRICE_MONITOR_INTERVAL)
    log.info("[PriceMonitor] Thread stopped.")

def _get_cached_atr(pair_key):
    with cache_lock: entry = _candle_cache.get(f"{pair_key}_4h")
    if entry and entry.get("data"): return calculate_atr(entry["data"])
    return 0.0

def start_price_monitor():
    t = threading.Thread(target=run_price_monitor, name="PriceMonitor", daemon=True)
    t.start(); return t


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 11 — MARKET HOURS & SESSION (unchanged)
# ═════════════════════════════════════════════════════════════════════════════

def is_market_open():
    now = datetime.now(timezone.utc); wd = now.weekday(); h = now.hour
    if wd == 5: return False
    if wd == 6 and h < 22: return False
    if wd == 4 and h >= 22: return False
    return True

def in_valid_session():
    h = datetime.now(timezone.utc).hour
    ok = SESSION_START_UTC <= h < SESSION_END_UTC
    log.info(f"[Session] {'Inside' if ok else 'Outside'} window (UTC {h}:00)")
    return ok

def session_name():
    h = datetime.now(timezone.utc).hour
    if 7  <= h < 10: return "London Open"
    if 10 <= h < 13: return "London Mid"
    if 13 <= h < 17: return "London/NY Overlap"
    if 17 <= h < 22: return "New York"
    if 0  <= h < 3:  return "Tokyo"
    return "Asian"

def in_killzone(pair_key):
    h = datetime.now(timezone.utc).hour
    for name, kz in PAIRS_CFG[pair_key]["killzones"].items():
        if kz["start"] <= h < kz["end"]: return True, name
    return False, None


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 12 — NEWS FILTER  [P3-3]
# ═════════════════════════════════════════════════════════════════════════════

_news_feed = {"events": [], "fetched_at": 0.0, "ok_at": 0.0}
_news_alert_day = {"day": None}

def _fetch_news_feed() -> list | None:
    """
    ForexFactory weekly JSON (single small request, explicit UTC offsets).
    Replaces v9's HTML scraping, which Cloudflare frequently blocks and whose
    page timezone was assumed rather than known.
    """
    try:
        res = requests.get(NEWS_FEED_URL, timeout=8,
                           headers={"User-Agent": f"VortexAlpha/{STRATEGY_VERSION}"})
        if res.status_code != 200:
            log.warning(f"[News] Feed HTTP {res.status_code}"); return None
        raw = res.json()
        if not isinstance(raw, list): return None
        events = []
        for ev in raw:
            cur = str(ev.get("country", "")).upper()
            if cur not in NEWS_CURRENCIES: continue
            imp = str(ev.get("impact", "")).upper()
            impact = "HIGH" if imp.startswith("HIGH") else ("MEDIUM" if imp.startswith("MED") else "LOW")
            events.append({"time_utc": _parse_ts(ev.get("date")), "currency": cur,
                           "impact": impact, "title": str(ev.get("title", ""))})
        log.info(f"[News] Feed OK – {len(events)} relevant events this week")
        return events
    except Exception as e:
        log.warning(f"[News] Feed exception: {e}"); return None

def _first_friday(d) -> bool:
    return d.weekday() == 4 and d.day <= 7

def _news_heuristic_fallback():
    """Improved fallback: blocks the NFP window, MEDIUM during US data hours."""
    now = datetime.now(timezone.utc)
    if _first_friday(now.date()) and 12 <= now.hour < 14:
        return {"impact": "HIGH", "events": [], "source": "heuristic", "is_high": True,
                "blocked": True, "block_reason": "Heuristic: NFP window (first Friday 12:00-14:00 UTC)"}
    if now.weekday() in (1, 2, 3, 4) and 12 <= now.hour < 17:
        return {"impact": "MEDIUM", "events": [], "source": "heuristic", "is_high": False,
                "blocked": False, "block_reason": ""}
    return {"impact": "LOW", "events": [], "source": "heuristic", "is_high": False,
            "blocked": False, "block_reason": ""}

def _is_blocked_by_event(events):
    now = datetime.now(timezone.utc); window = timedelta(minutes=NEWS_BLOCK_MINUTES)
    for ev in events:
        if ev.get("impact") != "HIGH": continue
        ev_time = ev.get("time_utc")
        if ev_time is None: continue
        if abs((now - ev_time).total_seconds()) <= window.total_seconds():
            return True, f"{ev['currency']} {ev['title']} @ {_to_bd(ev_time, '%H:%M')} BDT"
    return False, ""

def check_news_impact():
    now = time.time()
    with news_cache_lock:
        need = now - _news_feed["fetched_at"] >= NEWS_FEED_TTL
    if need:
        evs = _fetch_news_feed()
        with news_cache_lock:
            if evs is not None:
                _news_feed.update(events=evs, fetched_at=now, ok_at=now)
            else:
                _news_feed["fetched_at"] = now - NEWS_FEED_TTL + NEWS_RETRY_SEC
    with news_cache_lock:
        events = list(_news_feed["events"]); ok_at = _news_feed["ok_at"]

    if ok_at <= 0 or now - ok_at > NEWS_FEED_STALE_MAX:
        today = datetime.now(timezone.utc).date().isoformat()
        if _news_alert_day["day"] != today:
            _news_alert_day["day"] = today
            send_telegram("<b>VortexAlpha — News Feed Notice</b>\nEconomic calendar feed is "
                          "unavailable. Using time-based heuristic filter today.")
        return _news_heuristic_fallback()

    today = datetime.now(timezone.utc).date()
    today_ev = [e for e in events if e["time_utc"] and e["time_utc"].date() == today]
    blocked, reason = _is_blocked_by_event(events)
    high_today = any(e["impact"] == "HIGH" for e in today_ev)
    med_today  = any(e["impact"] == "MEDIUM" for e in today_ev)
    impact = "HIGH" if blocked else ("MEDIUM" if (high_today or med_today) else "LOW")
    return {"impact": impact, "events": today_ev, "source": "ff_json",
            "is_high": blocked, "blocked": blocked, "block_reason": reason}

def get_risk_for_news(ni, base_risk):
    if ni == "HIGH": return 0.0
    if ni == "MEDIUM": return min(0.5, base_risk)
    return base_risk


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 13 — TECHNICAL INDICATORS (unchanged)
# ═════════════════════════════════════════════════════════════════════════════

def calculate_atr(candles, period=14):
    if len(candles) < period + 1: return 0.0
    trs = [max(candles[i]["high"]-candles[i]["low"],
               abs(candles[i]["high"]-candles[i-1]["close"]),
               abs(candles[i]["low"]-candles[i-1]["close"]))
           for i in range(1,len(candles))]
    return sum(trs[-period:]) / period

def calculate_adx(candles, period=14):
    if len(candles) < period*2: return 20.0
    pdm, mdm, trs = [], [], []
    for i in range(1,len(candles)):
        h,l = candles[i]["high"],candles[i]["low"]
        ph,pl = candles[i-1]["high"],candles[i-1]["low"]
        pc = candles[i-1]["close"]
        um,dm = h-ph, pl-l
        pdm.append(um if um>dm and um>0 else 0)
        mdm.append(dm if dm>um and dm>0 else 0)
        trs.append(max(h-l,abs(h-pc),abs(l-pc)))
    def smooth(d,p):
        r = [sum(d[:p])/p]
        for i in range(p,len(d)): r.append(r[-1]-r[-1]/p+d[i])
        return r
    st=smooth(trs,period); sp=smooth(pdm,period); sm=smooth(mdm,period)
    dip=[100*x/t if t else 0 for x,t in zip(sp,st)]
    dim=[100*m/t if t else 0 for m,t in zip(sm,st)]
    dx=[100*abs(x-m)/(x+m) if (x+m) else 0 for x,m in zip(dip,dim)]
    return round(sum(dx[-period:])/period,2) if len(dx)>=period else 20.0

def count_clear_swings(candles, lb=5):
    if len(candles) < lb*3: return 0
    sh,sl = [],[]
    for i in range(lb, len(candles)-lb):
        w = candles[i-lb:i+lb+1]
        if candles[i]["high"]==max(x["high"] for x in w): sh.append(candles[i]["high"])
        if candles[i]["low"]==min(x["low"] for x in w): sl.append(candles[i]["low"])
    hh=sum(1 for i in range(1,len(sh)) if sh[i]>sh[i-1])
    hl=sum(1 for i in range(1,len(sl)) if sl[i]>sl[i-1])
    ll=sum(1 for i in range(1,len(sl)) if sl[i]<sl[i-1])
    lh=sum(1 for i in range(1,len(sh)) if sh[i]<sh[i-1])
    return max(min(hh,hl),min(ll,lh))

def get_disp(candles, atr):
    if len(candles)<10 or atr==0: return 0.0
    return max(abs(x["close"]-x["open"]) for x in candles[-10:]) / atr

def get_vol_ratio(candles, atr, period=5):
    if len(candles)<period or atr==0: return 1.0
    return calculate_atr(candles[-(period+1):], period) / atr


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 14 — VOLUME PROFILE (unchanged)
# ═════════════════════════════════════════════════════════════════════════════

def calculate_volume_profile(candles, bins=VP_BINS):
    if len(candles)<5: return {"poc":0.0,"vah":0.0,"val":0.0,"hvns":[]}
    lo=min(x["low"] for x in candles); hi=max(x["high"] for x in candles)
    if hi==lo: return {"poc":lo,"vah":hi,"val":lo,"hvns":[]}
    step=(hi-lo)/bins; vol_bins=defaultdict(float)
    price_mid=[lo+(i+0.5)*step for i in range(bins)]
    total_vol=sum(x.get("volume",0) or 0 for x in candles); use_vol=total_vol>0
    for c in candles:
        rng=c["high"]-c["low"]
        if rng==0: continue
        weight=(c.get("volume",0) or 0) if use_vol else rng
        for i in range(bins):
            bl=lo+i*step; bh=bl+step
            ov=max(0.0,min(c["high"],bh)-max(c["low"],bl))
            if ov>0: vol_bins[i]+=weight*(ov/rng)
    total=sum(vol_bins.values()) or 1.0
    poc_idx=max(range(bins),key=lambda i:vol_bins[i]); poc=price_mid[poc_idx]
    va_target=total*0.70; va_accum=vol_bins[poc_idx]; lo_p,hi_p=poc_idx,poc_idx
    while va_accum<va_target and (lo_p>0 or hi_p<bins-1):
        lv=vol_bins[lo_p-1] if lo_p>0 else 0
        hv=vol_bins[hi_p+1] if hi_p<bins-1 else 0
        if hv>=lv: hi_p+=1; va_accum+=hv
        else: lo_p-=1; va_accum+=lv
    vah=lo+(hi_p+1)*step; val=lo+lo_p*step
    hvns=[price_mid[i] for i in range(bins) if vol_bins[i]/total>=VP_HVN_THRESHOLD]
    return {"poc":round(poc,5),"vah":round(vah,5),"val":round(val,5),
            "hvns":[round(h,5) for h in hvns]}

def get_vp_confluence(price, vp, atr):
    if not vp or atr==0: return False,""
    dist=VP_DIST_MULT*atr
    for node,label in [(vp.get("poc"),"POC"),(vp.get("vah"),"VAH"),(vp.get("val"),"VAL")]:
        if node and abs(price-node)<=dist: return True,label
    for hvn in vp.get("hvns",[]):
        if abs(price-hvn)<=dist: return True,"HVN"
    return False,""


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 15 — BIAS DETECTION (unchanged)
# ═════════════════════════════════════════════════════════════════════════════

def get_bias(candles, lookback=50):
    if len(candles)<lookback: lookback=len(candles)
    if lookback<10: return "neutral"
    recent=candles[-lookback:]
    hh=sum(1 for i in range(1,len(recent)) if recent[i]["high"]>recent[i-1]["high"])
    hl=sum(1 for i in range(1,len(recent)) if recent[i]["low"]>recent[i-1]["low"])
    lh=sum(1 for i in range(1,len(recent)) if recent[i]["high"]<recent[i-1]["high"])
    ll=sum(1 for i in range(1,len(recent)) if recent[i]["low"]<recent[i-1]["low"])
    bull=hh+hl; bear=lh+ll
    if bull>bear*1.2: return "bullish"
    if bear>bull*1.2: return "bearish"
    return "neutral"

def get_weekly_bias(candles):
    # NOTE (not changed, locked): calculate_adx needs >=28 candles and only 20
    # weekly candles are fetched, so the ADX<18 check always passes. Fetching
    # 40 weekly candles would activate it but changes Gate 1 behaviour —
    # evaluate separately.
    if len(candles)<3: return "neutral"
    lookback=min(10,len(candles)); struct=get_bias(candles,lookback=lookback)
    adx=calculate_adx(candles)
    if adx<18: return "neutral"
    w_atr=calculate_atr(candles)
    displ=any(abs(x["close"]-x["open"])>0.5*w_atr for x in candles[-lookback:])
    if not displ: return "neutral"
    return struct


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 16 — MARKET REGIME (unchanged)
# ═════════════════════════════════════════════════════════════════════════════

def detect_regime(candles):
    if len(candles)<30: return "WEAK_TREND"
    atr=calculate_atr(candles); adx=calculate_adx(candles)
    sw=count_clear_swings(candles); disp=get_disp(candles,atr); vol=get_vol_ratio(candles,atr)
    log.info(f"  Regime – ADX:{adx:.1f} SW:{sw} Disp:{disp:.2f}x Vol:{vol:.2f}x")
    if vol>2.0: return "CHAOS"
    if adx>ADX_HIGH_THRESHOLD and sw>=SWING_MIN and disp>DISP_THRESHOLD: return "STRONG_TREND"
    if ADX_MID_THRESHOLD<=adx<=ADX_HIGH_THRESHOLD and sw>=SWING_OK: return "WEAK_TREND"
    if adx<ADX_MID_THRESHOLD and sw<SWING_OK: return "RANGE"
    return "WEAK_TREND"

def regime_label(r):
    return {"STRONG_TREND":"Strong Trend","WEAK_TREND":"Weak Trend","RANGE":"Range","CHAOS":"Chaos"}.get(r,r)


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 17 — CANDLE PATTERN (unchanged)
# ═════════════════════════════════════════════════════════════════════════════

def detect_candle_pattern(candle, atr):
    o,h,l,c=candle["open"],candle["high"],candle["low"],candle["close"]
    body=abs(c-o); rng=h-l
    if rng==0 or atr==0: return "none"
    bp=body/rng
    if bp>=0.85: return "marubozu"
    if bp>=0.70 and body>=1.0*atr: return "engulfing"
    upper=h-max(o,c); lower=min(o,c)-l
    if body>0:
        if lower>=2.0*body and upper<0.3*body: return "pinbar"
        if upper>=2.0*body and lower<0.3*body: return "pinbar"
    return "none"


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 18 — SMC / ICT DETECTION
# ═════════════════════════════════════════════════════════════════════════════

def detect_fvg(candles, pair_key):
    pm = PAIRS_CFG[pair_key]["pip_mult"]
    min_gap = PAIRS_CFG[pair_key]["fvg_min_gap"]
    signals = []
    for i in range(1, len(candles)-1):
        prev, nxt = candles[i-1], candles[i+1]
        bg = (nxt["low"]-prev["high"])*pm
        if bg>=min_gap: signals.append({"type":"FVG","dir":"BUY","gap":round(bg,1),
                                         "top":nxt["low"],"bottom":prev["high"]})
        sg = (prev["low"]-nxt["high"])*pm
        if sg>=min_gap: signals.append({"type":"FVG","dir":"SELL","gap":round(sg,1),
                                         "top":prev["low"],"bottom":nxt["high"]})
    return signals

def detect_bos(candles, lb=5):
    signals = []
    for i in range(lb, len(candles)):
        w=candles[i-lb:i]; sh=max(x["high"] for x in w); sl=min(x["low"] for x in w)
        cur,prev=candles[i],candles[i-1]
        if cur["close"]>sh and prev["close"]<=sh:
            signals.append({"type":"BOS","dir":"BUY","level":round(sh,5)})
        if cur["close"]<sl and prev["close"]>=sl:
            signals.append({"type":"BOS","dir":"SELL","level":round(sl,5)})
    return signals

def detect_obs(candles, direction, pair_key, max_age_hours=OB_EXPIRED_HOURS,
               fresh_hours=OB_FRESH_HOURS, check_mitigation=False):
    """
    [P1-6] Order Block detection.
      * Loop now runs to len-2 (v9: len-4). The impulse window is whatever
        closed candles follow (1–3), so recent OBs are no longer excluded.
      * max_age_hours is per timeframe (v9 applied 36h everywhere, which made
        every Daily OB "expired" → HTF bonus was dead code).
      * Impulse must be IN the trade direction (v9 accepted either direction).
      * check_mitigation: drop OBs that a later candle closed through.
    Callers must pass CLOSED candles.
    """
    obs=[]; atr=calculate_atr(candles); body_min=PAIRS_CFG[pair_key]["ob_body_pct"]
    if len(candles)<10 or atr==0: return obs
    now = datetime.now(timezone.utc)
    for i in range(2, len(candles)-1):
        x=candles[i]; bs=abs(x["close"]-x["open"]); ws=x["high"]-x["low"]
        bp=bs/ws if ws>0 else 0
        if direction=="BUY" and x["close"]>=x["open"]: continue    # BUY OB = last bearish candle
        if direction=="SELL" and x["close"]<=x["open"]: continue   # SELL OB = last bullish candle
        if bp<body_min: continue
        nc=candles[i+1:i+4]
        if direction=="BUY":
            impulse = any((n["close"]-n["open"]) >= OB_IMPULSE_MULT*atr for n in nc)
        else:
            impulse = any((n["open"]-n["close"]) >= OB_IMPULSE_MULT*atr for n in nc)
        if not impulse: continue
        ot=_parse_ts(x["time"])
        age=(now-ot).total_seconds()/3600 if ot else 0.0
        if age>max_age_hours: continue
        if check_mitigation:
            later=candles[i+1:]
            if direction=="BUY" and any(c["close"]<x["low"] for c in later): continue
            if direction=="SELL" and any(c["close"]>x["high"] for c in later): continue
        obs.append({"dir":direction,"high":x["high"],"low":x["low"],
                    "body_pct":round(bp,2),"age_hours":round(age,1),"time":x["time"],
                    "fresh":age<fresh_hours,"pattern":detect_candle_pattern(x,atr),
                    "mid":round((x["high"]+x["low"])/2,5)})
    return obs

def detect_htf_obs(daily_candles, weekly_candles, direction, pair_key):
    """[P1-6] Daily (30d) + Weekly (26w) unmitigated OBs with strength rating."""
    result=[]
    for tf, candles, max_age in (("Daily",  daily_candles,  HTF_OB_MAX_AGE_DAILY_H),
                                 ("Weekly", weekly_candles, HTF_OB_MAX_AGE_WEEKLY_H)):
        if not candles or len(candles)<10: continue
        atr_tf=calculate_atr(candles)
        if atr_tf==0: continue
        raw=detect_obs(candles, direction, pair_key, max_age_hours=max_age,
                       fresh_hours=max_age/3, check_mitigation=True)
        for ob in raw:
            ob_range=ob["high"]-ob["low"]; pattern=ob.get("pattern","none")
            if ob_range>1.5*atr_tf or pattern in ("engulfing","marubozu"): strength="strong"
            elif ob_range>0.8*atr_tf or pattern=="pinbar": strength="moderate"
            else: continue
            ob["strength"]=strength; ob["timeframe"]=tf; result.append(ob)
    return result

def near_htf_ob(price, htf_obs, atr):
    """Distance measured to the OB zone edge (0 when price is inside the zone)."""
    if not htf_obs or atr==0: return False,None
    def dist(ob):
        if ob["low"] <= price <= ob["high"]: return 0.0
        return min(abs(price-ob["low"]), abs(price-ob["high"]))
    cands=[ob for ob in htf_obs if dist(ob) <= atr*HTF_OB_DIST_MULT]
    if not cands: return False,None
    best=sorted(cands, key=lambda o:(0 if o["strength"]=="strong" else 1, dist(o)))[0]
    return True,best

def detect_sweeps(candles, direction, atr) -> list[dict]:
    """
    [P1-1][P1-5] All qualifying 1H liquidity sweeps in the lookback window,
    oldest first, each with its absolute candle index.
    Close back inside the swept level is MANDATORY; plus wick OR body.
    Pass CLOSED candles only.
    """
    out=[]
    if len(candles)<SWEEP_LOOKBACK_BARS+SWEEP_HISTORY_BARS or atr<=0: return out
    threshold=SWEEP_THRESHOLD_MULT*atr
    base=len(candles)-SWEEP_LOOKBACK_BARS
    history=candles[base-SWEEP_HISTORY_BARS:base]
    recent=candles[base:]
    if direction=="BUY":
        ref_low=min(x["low"] for x in history)
        for j,c in enumerate(recent):
            pierce=ref_low-c["low"]
            if pierce<threshold: continue
            rng=c["high"]-c["low"]; body=abs(c["close"]-c["open"])
            cc=c["close"]>ref_low
            cw=((c["close"]-c["low"])/rng>=SWEEP_WICK_PCT_MIN) if rng>0 else False
            cb=body>=SWEEP_BODY_ATR_MIN*atr
            if cc and (cw or cb):
                out.append({"level":round(ref_low,5),"dist":round(pierce/atr,2),
                            "type":"low_sweep","idx":base+j})
    else:
        ref_high=max(x["high"] for x in history)
        for j,c in enumerate(recent):
            pierce=c["high"]-ref_high
            if pierce<threshold: continue
            rng=c["high"]-c["low"]; body=abs(c["close"]-c["open"])
            cc=c["close"]<ref_high
            cw=((c["high"]-c["close"])/rng>=SWEEP_WICK_PCT_MIN) if rng>0 else False
            cb=body>=SWEEP_BODY_ATR_MIN*atr
            if cc and (cw or cb):
                out.append({"level":round(ref_high,5),"dist":round(pierce/atr,2),
                            "type":"high_sweep","idx":base+j})
    return out

def detect_sweep(candles, direction, atr):
    """Backward-compatible wrapper: (ok, first_sweep_info)."""
    s = detect_sweeps(candles, direction, atr)
    return (True, s[0]) if s else (False, None)

def find_fvg_amd(candles, disp_idx, direction, pair_key):
    """AMD Gate 4 — FVG in the 3-candle window around the displacement (LOCKED)."""
    if disp_idx < 1 or disp_idx + 1 >= len(candles):
        return None
    window = candles[disp_idx - 1: disp_idx + 2]
    for f in detect_fvg(window, pair_key):
        if f["dir"] == direction:
            return f
    return None

def find_amd_setup(closed, direction, atr, pair_key):
    """
    Gates 2–4 on CLOSED 1H candles, strictly chronological:
        sweep_idx  <=  disp_idx  <  fvg_idx(=disp_idx+1)  <  retest
    [P1-1] displacement can never precede the sweep.
    [P1-3] every sweep (freshest first) and every displacement candidate after
           it is tried until a valid FVG is found.
    [P1-4] FVG is rejected if a later closed candle closed beyond its far edge.
    Returns (setup_dict, "ok") or (None, reason).
    """
    sweeps = detect_sweeps(closed, direction, atr)
    if not sweeps: return None, "no_sweep"
    last = len(closed) - 1
    reason = "no_displacement"
    for sw in reversed(sweeps):
        s = sw["idx"]
        for d in range(max(s, 1), min(s + DISP_MAX_WAIT, last)):
            c = closed[d]
            body = abs(c["close"] - c["open"])
            is_dir = (c["close"] > c["open"]) if direction == "BUY" else (c["close"] < c["open"])
            if not is_dir or body < DISP_BODY_ATR_MIN * atr:
                continue
            fvg = find_fvg_amd(closed, d, direction, pair_key)
            if not fvg:
                if reason == "no_displacement": reason = "no_fvg"
                continue
            fvg_idx = d + 1
            if len(closed) - fvg_idx > FVG_RETEST_MAX_WAIT:
                reason = "retest_window_expired"; continue
            after = closed[fvg_idx + 1:]
            if direction == "BUY" and any(x["close"] < fvg["bottom"] for x in after):
                reason = "fvg_invalidated"; continue
            if direction == "SELL" and any(x["close"] > fvg["top"] for x in after):
                reason = "fvg_invalidated"; continue
            log.info(f"  [AMD] sweep@{s} disp@{d} FVG {fvg['bottom']:.5f}-{fvg['top']:.5f}")
            return {"sweep": sw, "disp_idx": d, "fvg": fvg, "fvg_idx": fvg_idx}, "ok"
    return None, reason

def check_fvg_retest(closed, live, fvg_idx, fvg, direction, atr, live_price):
    """
    AMD Gate 5 — retest trigger.
    [P1-2] Structure is fixed (closed candles). The trigger requires:
      * we are still inside the retest window,
      * price has touched the zone recently (forming candle or last closed
        candle after the FVG — never the FVG candle itself),
      * the LIVE price is inside the zone, or at most RETEST_RUNAWAY_ATR beyond
        the near edge (not chased), and not beyond the far edge.
    Entry = live price (executable). SL = FVG far edge ± SL_FVG_BUFFER×ATR.
    Returns (entry, sl, prior_touch_count) or (None, reason, 0).
    """
    top, bot = fvg["top"], fvg["bottom"]
    bars_since = len(closed) - fvg_idx
    if bars_since < 1 or bars_since > FVG_RETEST_MAX_WAIT:
        return None, "outside_retest_window", 0
    price = live_price if live_price else (live["close"] if live else None)
    if not price: return None, "no_live_price", 0

    recent = []
    if live: recent.append(live)
    if fvg_idx < len(closed) - 1: recent.append(closed[-1])
    prior_touches = sum(1 for c in closed[fvg_idx + 1:] if c["low"] <= top and c["high"] >= bot)

    runaway = RETEST_RUNAWAY_ATR * atr
    buf = SL_FVG_BUFFER * atr
    if direction == "BUY":
        in_zone = bot <= price <= top
        touched = in_zone or any(c["low"] <= top for c in recent)
        if not (bot <= price <= top + runaway): return None, "price_not_at_zone", prior_touches
        sl = bot - buf
    else:
        in_zone = bot <= price <= top
        touched = in_zone or any(c["high"] >= bot for c in recent)
        if not (bot - runaway <= price <= top): return None, "price_not_at_zone", prior_touches
        sl = top + buf
    if not touched: return None, "no_recent_touch", prior_touches
    log.info(f"  [Retest] live={price:.5f} zone={bot:.5f}-{top:.5f} SL={sl:.5f} priorTouches={prior_touches}")
    return round(price, 5), round(sl, 5), prior_touches

def detect_mss(candles, bias):
    if len(candles)<20: return False,"insufficient"
    lb=5; recent=candles[-lb*3:]; sh,sl=[],[]
    for i in range(lb,len(recent)-lb):
        w=recent[i-lb:i+lb+1]
        if recent[i]["high"]==max(x["high"] for x in w): sh.append((i,recent[i]["high"]))
        if recent[i]["low"]==min(x["low"] for x in w): sl.append((i,recent[i]["low"]))
    if bias=="bullish" and len(sl)>=2 and sl[-1][1]<sl[-2][1]: return True,"bearish_mss"
    if bias=="bearish" and len(sh)>=2 and sh[-1][1]>sh[-2][1]: return True,"bullish_mss"
    return False,"no_mss"

def detect_choch(candles, bias):
    if len(candles)<20: return False,"insufficient"
    lb=3; recent=candles[-lb*4:]; sh,sl=[],[]
    for i in range(lb,len(recent)-lb):
        w=recent[i-lb:i+lb+1]
        if recent[i]["high"]==max(x["high"] for x in w): sh.append((i,recent[i]["high"]))
        if recent[i]["low"]==min(x["low"] for x in w): sl.append((i,recent[i]["low"]))
    if bias=="bearish" and len(sh)>=2 and sh[-1][1]>sh[-2][1]: return True,"bullish_choch"
    if bias=="bullish" and len(sl)>=2 and sl[-1][1]<sl[-2][1]: return True,"bearish_choch"
    return False,"no_choch"

def detect_liq_zones(candles, direction, pair_key):
    if len(candles)<20: return []
    tol=PAIRS_CFG[pair_key]["eq_hl_tol"]; pm=PAIRS_CFG[pair_key]["pip_mult"]
    recent=candles[-20:]; zones=[]
    highs=[x["high"] for x in recent]; lows=[x["low"] for x in recent]
    if direction=="SELL":
        mh=max(highs); ehs=[h for h in highs if mh>0 and abs(h-mh)/mh<tol]
        if len(ehs)>=2: zones.append({"type":"equal_highs","level":round(mh,5),
                                       "strength":"strong" if len(ehs)>=3 else "normal"})
    if direction=="BUY":
        ml=min(lows); els=[l for l in lows if ml>0 and abs(l-ml)/ml<tol]
        if len(els)>=2: zones.append({"type":"equal_lows","level":round(ml,5),
                                       "strength":"strong" if len(els)>=3 else "normal"})
    if len(candles)>=PDH_PDL_LOOKBACK:
        pd_c=candles[-PDH_PDL_LOOKBACK:-PDH_PDL_LOOKBACK//2]
        if pd_c:
            if direction=="SELL": zones.append({"type":"pdh","level":round(max(x["high"] for x in pd_c),5),"strength":"normal"})
            if direction=="BUY": zones.append({"type":"pdl","level":round(min(x["low"] for x in pd_c),5),"strength":"normal"})
    if recent:
        price=recent[-1]["close"]; round_sz=50.0/pm; nearest=round(price/round_sz)*round_sz
        if abs(price-nearest)<=5*tol: zones.append({"type":"round_number","level":round(nearest,5),"strength":"normal"})
    return zones

def near_liq_zone(price, zones, atr):
    if not zones or atr==0: return False,None
    for z in sorted(zones,key=lambda x:0 if x.get("strength")=="strong" else 1):
        mult=LIQ_DIST_MULT if z.get("strength")=="strong" else 0.4
        if abs(price-z["level"])<=atr*mult: return True,z
    return False,None


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 19 — ENTRY TYPE & POSITION SIZING
# ═════════════════════════════════════════════════════════════════════════════

def get_entry_type(regime, m15, direction):
    if regime=="STRONG_TREND": return "aggressive"
    if regime=="WEAK_TREND":
        if m15 and len(m15)>=2:
            last=m15[-1]
            if direction=="BUY" and last["close"]>last["open"]: return "confirmation"
            if direction=="SELL" and last["close"]<last["open"]: return "confirmation"
        return "waiting"
    if regime=="RANGE":
        if m15:
            last=m15[-1]; wsz=last["high"]-last["low"]
            if wsz>0:
                if direction=="BUY" and (last["close"]-last["low"])/wsz>0.6: return "rejection"
                if direction=="SELL" and (last["high"]-last["close"])/wsz>0.6: return "rejection"
        return "waiting"
    return "confirmation"

def calc_levels(direction, entry, sl, pair_key):
    """TP1/TP2 as R multiples of the structural SL distance (LOCKED)."""
    cfg      = PAIRS_CFG[pair_key]
    risk = abs(entry - sl) or 0.0001
    sgn = 1 if direction == "BUY" else -1
    tp1 = round(entry + sgn * risk * cfg["tp1_multiplier"], 5)
    tp2 = round(entry + sgn * risk * cfg["tp2_multiplier"], 5)
    return round(sl, 5), tp1, tp2

def get_pos_size(regime, score):
    if regime=="STRONG_TREND": return 100
    if regime=="WEAK_TREND": return 75 if score>=8.5 else 50
    return 50

def get_dynamic_risk(base_risk, score, regime):
    if regime=="STRONG_TREND":
        if score>=9.0: return min(1.5,base_risk*1.5)
        if score>=8.5: return min(1.2,base_risk*1.2)
        return base_risk
    if regime=="WEAK_TREND": return min(0.75,base_risk*0.75)
    return min(0.5,base_risk*0.5)

def get_confidence(score, sweep, htf_ob, vp):
    if score>=9.0 and sweep and htf_ob and vp: return "A+"
    if score>=8.5 and sweep and (htf_ob or vp): return "A"
    return "B"

def _open_risk_pct() -> float:
    """[P2-4] Risk still at stake: trades not yet de-risked by TP1→BE."""
    with state_lock:
        return round(sum(float(t.get("risk_pct", 0) or 0)
                         for t in bot_state.get("open_trades", {}).values()
                         if not t.get("partial_tp_done")), 2)


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 20 — BAD CONDITION BLACKLIST (unchanged)
# ═════════════════════════════════════════════════════════════════════════════

def _cond_key(regime, session, news):
    return f"{regime}|{session[:8]}|{news}"

def update_bad_conditions(regime, session, news, is_win):
    key=_cond_key(regime,session,news)
    with state_lock:
        conds=bot_state["bad_conditions"]
        if key not in conds: conds[key]={"wins":0,"losses":0}
        if is_win: conds[key]["wins"]+=1
        else: conds[key]["losses"]+=1

def is_blacklisted(regime, session, news):
    key=_cond_key(regime,session,news)
    with state_lock: cond=bot_state["bad_conditions"].get(key,{})
    w=cond.get("wins",0); l=cond.get("losses",0); t=w+l
    if t==0: return False
    if l>=BLACKLIST_MIN_LOSSES and (w/t*100)<BLACKLIST_WR_CEIL:
        log.warning(f"  [Blacklist] BLOCKED: {key} ({w}W/{l}L)"); return True
    return False


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 21 — SCORING ENGINE (LOCKED — unchanged)
# ═════════════════════════════════════════════════════════════════════════════

def calc_score(regime, has_fvg, has_1h_bos, adx, swings, disp, vol, news_impact,
               has_ob=False, ob_age=None, ob_pattern="none", sweep=False,
               mss_confirmed=False, choch_confirmed=False, liq_zone=False,
               liq_strength="normal", entry_type="confirmation", weekly_confluence=False,
               in_kz=False, near_htf=False, htf_strength=None, vp_confluence=False,
               m15_confirmed=False, dxy_confirms=False, dxy_bias="neutral",
               weekly_conflict=False):
    s=SCORE_BASE
    if adx>ADX_HIGH_THRESHOLD and swings>=SWING_MIN: s+=SCORE_MARKET_STRONG
    elif adx>=ADX_MID_THRESHOLD and swings>=SWING_OK: s+=SCORE_MARKET_OK
    else: s+=SCORE_MARKET_WEAK
    if VOL_OK_LOW<=vol<=VOL_OK_HIGH: s+=SCORE_VOL_OK
    elif vol>VOL_HIGH_THRESHOLD: s+=SCORE_VOL_HIGH
    if has_fvg: s+=SCORE_FVG
    if disp>DISP_THRESHOLD: s+=SCORE_DISP_BONUS
    if has_1h_bos: s+=SCORE_BOS_BONUS
    if has_ob:
        s+=SCORE_OB_BASE
        if ob_age is not None:
            if ob_age<OB_FRESH_HOURS: s+=SCORE_OB_FRESH
            elif ob_age>OB_STALE_HOURS: s+=SCORE_OB_STALE
        if ob_pattern in ("engulfing","marubozu"): s+=SCORE_OB_PATTERN
    if sweep: s+=SCORE_SWEEP
    else: s+=SCORE_SWEEP_MISSING
    if mss_confirmed: s+=SCORE_MSS
    if choch_confirmed: s+=SCORE_CHOCH
    if liq_zone:
        s+=SCORE_LIQ_STRONG if liq_strength=="strong" else SCORE_LIQ_NORMAL
    if m15_confirmed: s+=SCORE_M15_CONFIRM
    if weekly_confluence: s+=SCORE_WEEKLY_CONF
    if in_kz: s+=SCORE_KILLZONE
    if near_htf:
        s+=SCORE_HTF_STRONG if htf_strength=="strong" else SCORE_HTF_MODERATE
    if vp_confluence: s+=SCORE_VP
    if dxy_bias!="neutral":
        if dxy_confirms: s+=SCORE_DXY_CONFIRMS
        else: s+=SCORE_DXY_NEUTRAL
    if entry_type=="rejection": s+=SCORE_REJECTION
    elif entry_type=="aggressive": s+=SCORE_AGGRESSIVE
    if adx<ADX_MID_THRESHOLD: s+=SCORE_ADX_LOW
    if news_impact=="HIGH": s+=SCORE_NEWS_HIGH
    elif news_impact=="MEDIUM": s+=SCORE_NEWS_MEDIUM
    if regime=="RANGE": s+=SCORE_RANGE
    elif regime=="WEAK_TREND": s+=SCORE_WEAK_TREND
    if weekly_conflict: s+=SCORE_WEEKLY_CONFLICT          # [G1-1]
    return round(max(0.0,min(10.0,s)),1)


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 22 — MAIN PAIR ANALYSIS (AMD 5 Gates)
# ═════════════════════════════════════════════════════════════════════════════

def analyze_pair(pair_key, daily_bias, weekly_bias, regime,
                 wc, dc, zc, ec, m15, atr, adx, sw, disp, vol,
                 news_info, sess, dxy_bias, live_price=None):
    """
    VortexAlpha v9.2 — AMD 5-Gate Strategy.
    Returns a signal dict (with "shadow": True/False) or None.
    """
    cfg = PAIRS_CFG[pair_key]

    # ── GATE 1: Bias ──────────────────────────────────────────────────────────
    if daily_bias == "neutral" and weekly_bias == "neutral":            # unchanged
        log.info(f"  {pair_key}: SKIP Gate 1 – both neutral"); return None
    # [G1-1] conflict no longer skips: DAILY leads, score penalty applied below
    weekly_conflict = (daily_bias != "neutral" and weekly_bias != "neutral"
                       and daily_bias != weekly_bias)
    effective_bias = daily_bias if daily_bias != "neutral" else weekly_bias   # unchanged
    direction      = "BUY" if effective_bias == "bullish" else "SELL"
    if weekly_conflict:
        log.info(f"  {pair_key}: Gate 1 CONFLICT daily={daily_bias} weekly={weekly_bias} "
                 f"-> daily leads {direction} ({SCORE_WEEKLY_CONFLICT:+} score)")
    else:
        log.info(f"  {pair_key}: Gate 1 OK -> {direction}")

    # [P1-2] closed-candle split for every structure timeframe
    ec_closed, ec_live = split_closed(ec, "1h")
    zc_closed, _       = split_closed(zc, "4h")
    dc_closed, _       = split_closed(dc, "1day")
    wc_closed, _       = split_closed(wc, "1week")
    m15_closed, _      = split_closed(m15, "15min")

    atr_1h = calculate_atr(ec_closed)
    if atr_1h == 0:
        log.info(f"  {pair_key}: SKIP – ATR_1H=0"); return None

    # ── GATES 2–4: Sweep → Displacement → FVG (chronological, closed) ────────
    setup, reason = find_amd_setup(ec_closed, direction, atr_1h, pair_key)
    if setup is None:
        log.info(f"  {pair_key}: SKIP Gates 2-4 – {reason}"); return None
    sweep_info = setup["sweep"]; fvg_zone = setup["fvg"]
    sweep_price = sweep_info["level"]
    log.info(f"  {pair_key}: Gates 2-4 OK sweep={sweep_price:.5f}")

    # ── GATE 5: FVG retest at live price ──────────────────────────────────────
    entry_price, sl_raw, touch_count = check_fvg_retest(
        ec_closed, ec_live, setup["fvg_idx"], fvg_zone, direction, atr_1h, live_price)
    if entry_price is None:
        log.info(f"  {pair_key}: SKIP Gate 5 – {sl_raw}"); return None
    log.info(f"  {pair_key}: Gate 5 OK entry={entry_price:.5f}")

    # ── Levels ────────────────────────────────────────────────────────────────
    sl, tp1, tp2 = calc_levels(direction, entry_price, sl_raw, pair_key)
    risk   = abs(entry_price - sl)
    reward = abs(tp2 - entry_price)
    # NOTE: with TP2 fixed at 3.0R this check is structural only (always 3.0).
    if risk == 0 or reward / risk < 2.5:
        log.info(f"  {pair_key}: SKIP – RR < 2.5"); return None

    # [D-13] Min-SL guard (structural SL too tight vs spread/noise)
    sl_pips   = abs(entry_price - sl) * cfg["pip_mult"]
    sl_min    = cfg.get("sl_min_pips", 0)
    if sl_pips < sl_min:
        log.info(f"  {pair_key}: SKIP – SL {sl_pips:.1f} pips < min {sl_min} pips"); return None

    # ── Bonus Scoring (closed candles) ────────────────────────────────────────
    weekly_confluence  = (weekly_bias != "neutral" and weekly_bias == effective_bias)
    dxy_confirms_flag  = dxy_confirms_direction(dxy_bias, direction)

    obs_list   = detect_obs(zc_closed, direction, pair_key)
    has_ob     = len(obs_list) > 0
    ob_age     = obs_list[-1]["age_hours"] if has_ob else None
    ob_pat     = obs_list[-1].get("pattern","none") if has_ob else "none"
    ob_mid     = obs_list[-1].get("mid", entry_price) if has_ob else entry_price

    has_1h_bos = any(b["dir"]==direction for b in detect_bos(ec_closed)[-6:])
    fvg_4h_ok  = any(f["dir"]==direction for f in detect_fvg(zc_closed, pair_key))
    mss_ok,  _ = detect_mss(zc_closed, effective_bias)
    choch_ok,_ = detect_choch(zc_closed, effective_bias)

    htf_list            = detect_htf_obs(dc_closed, wc_closed, direction, pair_key)   # [P1-6]
    near_htf_ok, htf_ob = near_htf_ob(entry_price, htf_list, atr)
    htf_str             = htf_ob.get("strength") if htf_ob else None
    if near_htf_ok:
        log.info(f"  {pair_key}: HTF OB {htf_ob['timeframe']} {htf_str} "
                 f"{htf_ob['low']:.5f}-{htf_ob['high']:.5f}")

    vp              = calculate_volume_profile(zc_closed)
    vp_ok, vp_label = get_vp_confluence(entry_price, vp, atr)

    in_kz, kz_name  = in_killzone(pair_key)
    m15_ok           = any(b["dir"]==direction for b in detect_bos(m15_closed)[-3:])

    liq_zones        = detect_liq_zones(zc_closed, direction, pair_key)
    liq_ok, liq_zone = near_liq_zone(entry_price, liq_zones, atr)
    liq_str          = liq_zone.get("strength","normal") if liq_zone else "normal"

    entry_type       = get_entry_type(regime, m15_closed, direction)

    score = calc_score(
        regime=regime, has_fvg=fvg_4h_ok, has_1h_bos=has_1h_bos,
        adx=adx, swings=sw, disp=disp, vol=vol, news_impact=news_info["impact"],
        has_ob=has_ob, ob_age=ob_age, ob_pattern=ob_pat,
        sweep=True, mss_confirmed=mss_ok, choch_confirmed=choch_ok,
        liq_zone=liq_ok, liq_strength=liq_str, entry_type=entry_type,
        weekly_confluence=weekly_confluence, in_kz=in_kz, weekly_conflict=weekly_conflict,
        near_htf=near_htf_ok, htf_strength=htf_str,
        vp_confluence=vp_ok, m15_confirmed=m15_ok,
        dxy_confirms=dxy_confirms_flag, dxy_bias=dxy_bias,
    )

    threshold = (cfg["score_threshold_weak"] if regime=="WEAK_TREND"
                 else cfg["score_threshold"])
    live_ok = score >= threshold
    # [P4-1] shadow band: passed every gate, score in [SHADOW_SCORE_MIN, threshold)
    shadow  = (not live_ok) and SHADOW_ENABLED and score >= SHADOW_SCORE_MIN
    if not live_ok and not shadow:
        log.info(f"  {pair_key}: SKIP – score {score:.1f} < {SHADOW_SCORE_MIN}"); return None
    if live_ok and is_blacklisted(regime, sess, news_info["impact"]):
        log.info(f"  {pair_key}: SKIP – blacklisted"); return None

    log.info(f"  {pair_key}: {'SHADOW' if shadow else 'Signal'} {direction} score={score:.1f} "
             f"(thr {threshold}) entry={entry_price} sl={sl} tp1={tp1} tp2={tp2}")

    return {
        "shadow": shadow, "threshold": threshold,
        "direction": direction, "signal_type": "AMD Sweep Reversal",
        "entry": entry_price, "sl": sl, "tp1": tp1, "tp2": tp2,
        "score": score, "confidence": get_confidence(score, True, near_htf_ok, vp_ok),
        "effective_bias": effective_bias, "atr": atr_1h,
        "sweep_confirmed": True, "sweep_price": sweep_price, "sweep_info": sweep_info,
        "disp_confirmed": True,
        "fvg_top": fvg_zone["top"], "fvg_bottom": fvg_zone["bottom"],
        "fvg_touch_count": touch_count,
        "has_ob": has_ob, "ob_age": ob_age, "ob_pattern": ob_pat, "ob_mid": ob_mid,
        "near_htf_ob": near_htf_ok, "htf_strength": htf_str or "",
        "vp_confluence": vp_ok, "vp_label": vp_label,
        "has_1h_bos": has_1h_bos, "mss_confirmed": mss_ok, "choch_confirmed": choch_ok,
        "dxy_confirms": dxy_confirms_flag, "dxy_bias": dxy_bias,
        "in_killzone": in_kz, "killzone_name": kz_name or "",
        "weekly_confluence": weekly_confluence, "m15_confirmed": m15_ok,
        "weekly_conflict": weekly_conflict,                                   # [G1-1]
        "liq_zone": liq_ok, "liq_strength": liq_str, "entry_type": entry_type,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 23 — MESSAGE BUILDERS (Professional English, Bangladesh Time)
# ═════════════════════════════════════════════════════════════════════════════

def _bd_now_str(fmt="%Y-%m-%d %H:%M"):
    return (datetime.now(timezone.utc) + BD_OFFSET).strftime(fmt)

def _to_bd(dt_utc: datetime, fmt="%Y-%m-%d %H:%M"):
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return (dt_utc.astimezone(timezone.utc) + BD_OFFSET).strftime(fmt)

def _to_bd_from_str(time_str: str) -> str:
    dt = _parse_ts(time_str)
    return _to_bd(dt) + " BDT" if dt else str(time_str)

def _confidence_label(c):
    return {"A+":"A+ (Elite)","A":"A (High Quality)","B":"B (Standard)"}.get(c,"B (Standard)")

def _strength_label(score):
    if score>=9.0: return "5/5"
    if score>=8.5: return "4/5"
    if score>=8.0: return "3/5"
    return "2/5"

def build_signal_message(sig, pair_key, regime, sess, risk_pct, pos_size, news_info):
    cfg=PAIRS_CFG[pair_key]; pm=cfg["pip_mult"]
    direction=sig["direction"]; entry=sig["entry"]
    sl=sig["sl"]; tp1=sig["tp1"]; tp2=sig["tp2"]; score=sig["score"]
    conf=sig.get("confidence","B")
    now_bd=_bd_now_str("%Y-%m-%d %H:%M")+" BDT"
    sl_pips=round(abs(entry-sl)*pm)
    tp1_pips=round(abs(tp1-entry)*pm)
    tp2_pips=round(abs(tp2-entry)*pm)
    rr1=round(tp1_pips/sl_pips,1) if sl_pips else 0
    rr2=round(tp2_pips/sl_pips,1) if sl_pips else 0
    ni_str={"HIGH":"High","MEDIUM":"Medium","LOW":"Low"}.get(news_info["impact"],"Low")
    with state_lock: sig_num=bot_state["signal_count"]
    htf = f"Yes ({sig.get('htf_strength')})" if sig.get("near_htf_ob") else "No"
    conflict_line = "\nWeekly           : opposed ⚠️ (daily leads)" if sig.get("weekly_conflict") else ""  # [G1-1]
    return f"""<b>VortexAlpha — Signal #{sig_num} — {cfg['pair_name']}</b>
━━━━━━━━━━━━━━━━━━━━━
Direction        : <b>{direction}</b>
Timeframe        : H1
Execution        : <b>MARKET</b> (at live price)
Confidence       : {_confidence_label(conf)}
Session          : {html.escape(sess)}
Generated Time   : {now_bd}
━━━━━━━━━━━━━━━━━━━━━
Entry Price      : <code>{entry}</code>
Stop Loss        : <code>{sl}</code>  (-{sl_pips} pips)
Take Profit 1    : <code>{tp1}</code>  (+{tp1_pips} pips | R:{rr1})
Take Profit 2    : <code>{tp2}</code>  (+{tp2_pips} pips | R:{rr2})
FVG Zone         : <code>{sig['fvg_bottom']}</code> – <code>{sig['fvg_top']}</code>
━━━━━━━━━━━━━━━━━━━━━
Regime           : {regime_label(regime)}{conflict_line}
Score            : {score}/10 ({_strength_label(score)})
HTF Order Block  : {html.escape(htf)}
Risk             : {risk_pct}%
Position Size    : {pos_size}%
News Impact      : {ni_str}
━━━━━━━━━━━━━━━━━━━━━
Management: Close {PARTIAL_TP_PCT}% at TP1, move Stop Loss to breakeven,
trail remaining position toward TP2.
Daily Loss Limit : {MAX_DAILY_LOSS_PCT}%""".strip()

def build_result_message(pair_key, direction, entry, exit_price, result, pips, r_mult, tp_hit,
                          partial=False, duration_hrs=None, loss_reason=None):
    cfg=PAIRS_CFG[pair_key]
    now_bd=_bd_now_str("%Y-%m-%d %H:%M")+" BDT"
    pp=f"+{pips}" if pips>0 else str(pips)
    rr=f"+{r_mult}R" if r_mult>0 else f"{r_mult}R"
    tp_str=f" ({html.escape(tp_hit)})" if tp_hit else ""
    p_note=(f"\nIncludes {PARTIAL_TP_PCT}% booked at TP1; figures are for the whole position."
            if partial else "")
    dur_str=f"\nTrade Duration   : {duration_hrs:.1f}h" if duration_hrs is not None else ""
    reason_str=f"\nLoss Reason      : {html.escape(loss_reason)}" if loss_reason else ""
    return (f"<b>VortexAlpha — Trade Closed — {cfg['pair_name']}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Time             : {now_bd}\n"
            f"Result           : <b>{html.escape(result)}{tp_str}</b>\n"
            f"Direction        : {html.escape(direction)}\n"
            f"Entry Price      : <code>{entry}</code>\n"
            f"Exit Price       : <code>{exit_price}</code>\n"
            f"Net Pips         : <code>{pp}</code>\n"
            f"Net R-Multiple   : <b>{rr}</b>{dur_str}{reason_str}{p_note}\n"
            f"━━━━━━━━━━━━━━━━━━━━━")

def build_partial_tp_message(pair_key, direction, entry, tp1, pips):
    cfg=PAIRS_CFG[pair_key]; pp=f"+{pips}" if pips>0 else str(pips)
    now_bd=_bd_now_str("%Y-%m-%d %H:%M")+" BDT"
    return (f"<b>VortexAlpha — Take Profit 1 Hit — {cfg['pair_name']}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Time             : {now_bd}\n"
            f"Direction        : {html.escape(direction)}\n"
            f"Entry Price      : <code>{entry}</code>\n"
            f"Take Profit 1    : <code>{tp1}</code>\n"
            f"Pips (TP1 leg)   : <code>{pp}</code>\n"
            f"Status           : {PARTIAL_TP_PCT}% closed. Stop Loss moved to breakeven.\n"
            f"━━━━━━━━━━━━━━━━━━━━━")

def build_trailing_sl_message(pair_key, direction, entry, new_sl):
    cfg=PAIRS_CFG[pair_key]
    now_bd=_bd_now_str("%Y-%m-%d %H:%M")+" BDT"
    return (f"<b>VortexAlpha — Trailing Stop Updated — {cfg['pair_name']}</b>\n"
            f"Time             : {now_bd}\n"
            f"Direction        : {html.escape(direction)}\n"
            f"Entry Price      : <code>{entry}</code>\n"
            f"New Stop Loss    : <code>{new_sl}</code>")


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 24 — PERFORMANCE TRACKING
# ═════════════════════════════════════════════════════════════════════════════

def update_performance(pair_key: str, result: str, pips: float) -> None:
    pair_name = PAIRS_CFG[pair_key]["pair_name"]
    with state_lock:
        is_win = "WIN" in result
        if is_win:
            bot_state["today_wins"]     += 1
            bot_state["week_wins"]      += 1
            bot_state["month_wins"]     += 1
            bot_state["total_win_pips"]  = round(bot_state["total_win_pips"]  + pips, 1)
        else:
            bot_state["today_losses"]    += 1
            bot_state["week_losses"]     += 1
            bot_state["month_losses"]    += 1
            bot_state["total_loss_pips"] = round(bot_state["total_loss_pips"] + abs(pips), 1)
        for k in ["today_pips", "week_pips", "month_pips"]:
            bot_state[k] = round(bot_state[k] + pips, 1)
        ps = bot_state["pair_stats"].setdefault(pair_name, {
            "wins": 0, "losses": 0, "pips": 0.0,
            "total_win_pips": 0.0, "total_loss_pips": 0.0,
        })
        if is_win:
            ps["wins"]           += 1
            ps["total_win_pips"]  = round(ps.get("total_win_pips", 0.0)  + pips, 1)
        else:
            ps["losses"]           += 1
            ps["total_loss_pips"]  = round(ps.get("total_loss_pips", 0.0) + abs(pips), 1)
        ps["pips"] = round(ps["pips"] + pips, 1)

def calc_expectancy(stats: dict) -> float:
    w = stats.get("wins", 0); l = stats.get("losses", 0); t = w + l
    if t == 0: return 0.0
    aw = stats.get("total_win_pips",  0.0) / w if w > 0 else 0.0
    al = stats.get("total_loss_pips", 0.0) / l if l > 0 else 0.0
    return round((w / t * aw) - (l / t * al), 2)

def calc_winrate(w: int, l: int) -> float:
    t = w + l
    return round(w / t * 100, 1) if t > 0 else 0.0


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 25 — TRADE MANAGEMENT ENGINE  [P2-1][P2-2]
#  One pure evaluator shared by live and shadow trades, so shadow results are
#  directly comparable with live results.
# ═════════════════════════════════════════════════════════════════════════════

def _initial_risk(trade: dict, pm: float) -> float:
    """Original risk distance in price. Never derived from a moved SL."""
    r = trade.get("initial_risk")
    if r: return float(r)
    if trade.get("orig_sl") is not None:
        return abs(trade["entry"] - trade["orig_sl"]) or 0.0001
    if trade.get("sl_size_pips"):                       # v9 trades loaded after upgrade
        return float(trade["sl_size_pips"]) / pm
    return abs(trade["entry"] - trade["sl"]) or 0.0001

def calc_trailing_sl(trade: dict, price: float, atr: float) -> float | None:
    if atr == 0: return None
    sl = trade.get("trailing_sl") or trade["sl"]
    if trade["direction"] == "BUY":
        new_sl = round(price - TRAILING_ATR_MULT * atr, 5)
        return new_sl if new_sl > sl else None
    new_sl = round(price + TRAILING_ATR_MULT * atr, 5)
    return new_sl if new_sl < sl else None

def evaluate_trade_tick(trade: dict, last: float, hi: float, lo: float, atr: float, pm: float) -> list:
    """
    Mutates `trade`, returns events:
      ("partial", tp1_price) | ("trail", new_sl, old_sl) | ("close", exit_price, tag)
    Ordering is conservative: before TP1, if the window touched both SL and TP1,
    SL is assumed first. On the window where TP1 fills, the BE stop is checked
    against the last price only (intra-window order is unknown).
    """
    events = []
    buy = trade["direction"] == "BUY"; sgn = 1 if buy else -1
    entry = trade["entry"]; tp1 = trade["tp1"]; tp2 = trade.get("tp2", tp1)
    sl = trade.get("trailing_sl") or trade["sl"]

    fav_px = hi if buy else lo; adv_px = lo if buy else hi
    adverse   = round(max(0.0, (entry - adv_px) * sgn * pm), 1)
    favorable = round(max(0.0, (fav_px - entry) * sgn * pm), 1)
    trade["max_adverse_pips"]   = max(trade.get("max_adverse_pips") or 0, adverse)
    trade["max_favorable_pips"] = max(trade.get("max_favorable_pips") or 0, favorable)

    partial_now = False
    if not trade.get("partial_tp_done"):
        if (lo <= sl) if buy else (hi >= sl):
            events.append(("close", sl, "SL")); return events
        if (hi >= tp1) if buy else (lo <= tp1):
            trade.update({"partial_tp_done": True, "partial_tp_price": tp1,
                          "sl": entry, "trailing_sl": entry})
            sl = entry; partial_now = True
            events.append(("partial", tp1))

    if trade.get("partial_tp_done"):
        if (hi >= tp2) if buy else (lo <= tp2):
            events.append(("close", tp2, "TP2")); return events
        c_hi, c_lo = (last, last) if partial_now else (hi, lo)
        if (c_lo <= sl) if buy else (c_hi >= sl):
            tag = "TP1+SL@Entry" if abs(sl - entry) * pm < 0.1 else "TP1+TrailingSL"
            events.append(("close", sl, tag)); return events
        new_sl = calc_trailing_sl(trade, last, atr)
        if new_sl is not None:
            old = sl
            trade["trailing_sl"] = new_sl; trade["sl"] = new_sl
            events.append(("trail", new_sl, old))
    return events

def compute_close(trade: dict, exit_price: float, tag: str, pm: float) -> tuple[str, float, float]:
    """
    [P2-1] Blended R-multiple over the WHOLE position:
      no partial : r = (exit-entry)/R
      partial    : r = f×(TP1-entry)/R + (1-f)×(exit-entry)/R ,  f = PARTIAL_TP_PCT
    Examples: SL → -1.0R | TP1 then BE → +0.9R | TP1 then TP2 → +2.4R
    Returns (result_label, r_multiple, blended_pips).
    """
    entry = trade["entry"]; sgn = 1 if trade["direction"] == "BUY" else -1
    R = _initial_risk(trade, pm)
    leg = (exit_price - entry) * sgn / R
    if trade.get("partial_tp_done"):
        f = PARTIAL_TP_PCT / 100.0
        tp1_r = abs(trade["tp1"] - entry) / R
        r = f * tp1_r + (1 - f) * leg
    else:
        r = leg
    r = round(r, 2)
    pips = round(r * R * pm, 1)
    if r < 0:
        result = "LOSS"
    elif tag == "TP2":
        result = "WIN_TP2"
    else:
        result = "WIN_TP1"
    return result, r, pips

def check_open_trades_for_pair(pair_key: str, last: float, atr_4h: float = 0.0, bars=None) -> None:
    if not last or last <= 0: return
    pm = PAIRS_CFG[pair_key]["pip_mult"]
    with state_lock:
        pair_trades = {k: copy.deepcopy(v) for k, v in bot_state.get("open_trades", {}).items()
                       if v.get("pair_key") == pair_key}
    if not pair_trades: return
    now_ts = time.time(); dirty = False
    for key, trade in pair_trades.items():
        direction = trade["direction"]; entry = trade["entry"]
        since = trade.get("last_check_ts"); strict = since is None
        if strict: since = _trade_open_ts(trade)
        hi, lo = _window_hilo(bars, since, last, strict, trade.get("last_bar_ts"))
        events = evaluate_trade_tick(trade, last, hi, lo, atr_4h, pm)
        trade["last_check_ts"] = now_ts
        if bars:   # never below the entry time → pre-entry bars stay excluded
            trade["last_bar_ts"] = max(trade.get("last_bar_ts") or _trade_open_ts(trade), bars[-1][0])
        closed_now = False
        for ev in events:
            if ev[0] == "partial":
                tp1_pips = round((ev[1] - entry) * pm * (1 if direction == "BUY" else -1), 1)
                send_telegram(build_partial_tp_message(pair_key, direction, entry, ev[1], tp1_pips),
                              main_menu_buttons())
                sb_mark_partial_tp(trade.get("db_id"), ev[1])
                dirty = True
            elif ev[0] == "trail":
                new_sl, old_sl = ev[1], ev[2]
                sb_update_trailing_sl(trade.get("db_id"), new_sl)
                if abs(new_sl - old_sl) * pm >= 50:
                    send_telegram(build_trailing_sl_message(pair_key, direction, entry, new_sl),
                                  main_menu_buttons())
                dirty = True
            elif ev[0] == "close":
                _finalize_live_trade(pair_key, trade, ev[1], ev[2], pm)
                closed_now = True; dirty = True
        with state_lock:
            if closed_now:
                bot_state["open_trades"].pop(key, None)
                bot_state["trailing_sl"].pop(key, None)
            elif key in bot_state["open_trades"]:
                bot_state["open_trades"][key] = trade
    if dirty:
        save_persisted_state()      # [P2-6] persist partial/trailing immediately

def _finalize_live_trade(pair_key, trade, exit_price, tag, pm):
    direction = trade["direction"]; entry = trade["entry"]
    result, r_mult, pips = compute_close(trade, exit_price, tag, pm)
    dur = (time.time() - _trade_open_ts(trade)) / 3600
    if r_mult < 0:
        # daily risk consumed in proportion to the realised loss
        with state_lock:
            bot_state["daily_risk_used"] = round(
                bot_state["daily_risk_used"] + float(trade.get("risk_pct", 1.0)) * (-r_mult), 2)
    update_bad_conditions(trade.get("regime","Unknown"), trade.get("session","Unknown"),
                          trade.get("news_impact","LOW"), is_win=(r_mult > 0))
    update_performance(pair_key, result, pips)
    loss_reason = _analyze_loss_reason(trade, dur) if result == "LOSS" else None
    sb_update_result(
        trade.get("db_id"), result, exit_price, pips, r_mult, dur, tag,
        partial_tp_price=trade.get("partial_tp_price"),
        trailing_active=trade.get("partial_tp_done", False),
        trade_dict=trade,
        max_adverse=trade.get("max_adverse_pips"),
        max_favorable=trade.get("max_favorable_pips"),
        orig_sl=trade.get("orig_sl"),
    )
    send_telegram(build_result_message(pair_key, direction, entry, exit_price, result, pips, r_mult,
                                       tag, partial=trade.get("partial_tp_done", False),
                                       duration_hrs=dur, loss_reason=loss_reason),
                  main_menu_buttons())
    log.info(f"[Trade] {pair_key} closed {result} {r_mult}R ({pips} pips)")

# ── [P4-1] Shadow trade tracking (no Telegram, no stats, no risk usage) ─────

def check_shadow_trades_for_pair(pair_key: str, last: float, atr_4h: float = 0.0, bars=None) -> None:
    if not last or last <= 0: return
    pm = PAIRS_CFG[pair_key]["pip_mult"]
    with state_lock:
        trades = {k: copy.deepcopy(v) for k, v in bot_state.get("shadow_open", {}).items()
                  if v.get("pair_key") == pair_key}
    if not trades: return
    now_ts = time.time(); dirty = False
    for key, trade in trades.items():
        since = trade.get("last_check_ts"); strict = since is None
        if strict: since = _trade_open_ts(trade)
        hi, lo = _window_hilo(bars, since, last, strict, trade.get("last_bar_ts"))
        events = evaluate_trade_tick(trade, last, hi, lo, atr_4h, pm)
        trade["last_check_ts"] = now_ts
        if bars:   # never below the entry time → pre-entry bars stay excluded
            trade["last_bar_ts"] = max(trade.get("last_bar_ts") or _trade_open_ts(trade), bars[-1][0])
        closed_now = False
        age_h = (now_ts - _trade_open_ts(trade)) / 3600
        if not any(e[0] == "close" for e in events) and age_h > SHADOW_MAX_AGE_H:
            events.append(("close", last, "EXPIRED"))
        for ev in events:
            if ev[0] == "partial":
                sb_update_shadow(trade.get("db_id"), {"partial_tp_hit": True}); dirty = True
            elif ev[0] == "trail":
                dirty = True
            elif ev[0] == "close":
                result, r_mult, pips = compute_close(trade, ev[1], ev[2], pm)
                if ev[2] == "EXPIRED": result = "EXPIRED"
                sb_update_shadow(trade.get("db_id"), {
                    "result": result, "exit_price": ev[1], "r_multiple": r_mult, "pips": pips,
                    "duration_hrs": round(age_h, 1),
                    "max_adverse_pips": trade.get("max_adverse_pips"),
                    "max_favorable_pips": trade.get("max_favorable_pips"),
                    "closed_at": datetime.now(timezone.utc).isoformat(),
                })
                log.info(f"[Shadow] {pair_key} closed {result} {r_mult}R")
                closed_now = True; dirty = True
        with state_lock:
            if closed_now: bot_state["shadow_open"].pop(key, None)
            elif key in bot_state["shadow_open"]: bot_state["shadow_open"][key] = trade
    if dirty:
        save_persisted_state()

def log_shadow_signal(pair_key, result, regime, sess, news_info):
    cfg = PAIRS_CFG[pair_key]; pm = cfg["pip_mult"]
    sig_hash = "SH" + _make_signal_hash(cfg["pair_name"], result["direction"],
                                        result["fvg_top"], result["fvg_bottom"])
    if is_duplicate_signal(sig_hash, shadow_sent):
        log.info(f"  {pair_key}: shadow duplicate – skip"); return
    with state_lock:
        n_open = len(bot_state.get("shadow_open", {}))
    if n_open >= SHADOW_MAX_OPEN:
        log.warning(f"  {pair_key}: shadow capacity reached ({n_open}) – skip"); return
    entry, sl = result["entry"], result["sl"]
    row = {
        "strategy_version": STRATEGY_VERSION,
        "pair": cfg["pair_name"], "direction": result["direction"],
        "session": sess, "regime": regime,
        "score": result["score"], "threshold": result["threshold"],
        "confidence": result["confidence"],
        "entry": entry, "sl": sl, "tp1": result["tp1"], "tp2": result["tp2"],
        "sl_size_pips": round(abs(entry - sl) * pm, 1),
        "fvg_top": result["fvg_top"], "fvg_bottom": result["fvg_bottom"],
        "fvg_touch_count": result.get("fvg_touch_count"),
        "sweep_price": result.get("sweep_price"),
        "has_ob": result["has_ob"], "near_htf_ob": result["near_htf_ob"],
        "htf_strength": result["htf_strength"], "vp_confluence": result["vp_confluence"],
        "in_killzone": result["in_killzone"], "killzone_name": result["killzone_name"],
        "weekly_confluence": result["weekly_confluence"],
        "weekly_conflict": bool(result.get("weekly_conflict", False)),        # [G1-1]
        "dxy_confirms": result["dxy_confirms"],
        "news_impact": news_info["impact"], "sig_hash": sig_hash,
        "result": "OPEN",
    }
    sid = sb_insert_shadow(row)
    register_signal(sig_hash, shadow_sent)
    if sid is None:
        log.info(f"  {pair_key}: shadow not stored (DB unavailable) – not tracked"); return
    trade = {
        "pair_key": pair_key, "direction": result["direction"],
        "entry": entry, "sl": sl, "orig_sl": sl, "initial_risk": abs(entry - sl),
        "tp1": result["tp1"], "tp2": result["tp2"], "score": result["score"],
        "open_ts": time.time(),
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "db_id": sid, "partial_tp_done": False, "trailing_sl": None,
        "max_adverse_pips": 0.0, "max_favorable_pips": 0.0,
    }
    with state_lock:
        bot_state["shadow_open"][f"SH_{cfg['pair_name']}_{int(time.time())}"] = trade
    save_persisted_state()
    log.info(f"  [Shadow] Logged {pair_key} {result['direction']} score={result['score']} id={sid}")


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 26 — REPORTS (Professional English, Bangladesh Time)
# ═════════════════════════════════════════════════════════════════════════════

def build_weekly_loss_report(losses: list) -> str:
    total = len(losses)
    if total == 0:
        return "<b>VortexAlpha — Weekly Loss Report</b>\n━━━━━━━━━━━━━━━━━━━━━\nNo losing trades this week."
    now_str = _bd_now_str("%Y-%m-%d %H:%M")+" BDT"
    pair_cnt = defaultdict(int); regime_cnt = defaultdict(int)
    score_ranges = {"<7.5": 0, "7.5-8": 0, "8-8.5": 0, ">=8.5": 0}
    conf_miss = {"HTF OB": 0, "VP": 0, "Sweep": 0, "Disp": 0, "MSS": 0, "Killzone": 0, "DXY": 0}
    total_pips = 0.0; scores = []
    for t in losses:
        pair_cnt[t.get("pair","-")]      += 1
        regime_cnt[t.get("regime","?")]   += 1
        sc = float(t.get("score",0) or 0); scores.append(sc)
        if sc<7.5: score_ranges["<7.5"]+=1
        elif sc<8.0: score_ranges["7.5-8"]+=1
        elif sc<8.5: score_ranges["8-8.5"]+=1
        else: score_ranges[">=8.5"]+=1
        if not t.get("near_htf_ob"): conf_miss["HTF OB"]+=1
        if not t.get("vp_confluence"): conf_miss["VP"]+=1
        if not t.get("sweep_confirmed"): conf_miss["Sweep"]+=1
        if not t.get("disp_confirmed"): conf_miss["Disp"]+=1
        if not t.get("mss_confirmed"): conf_miss["MSS"]+=1
        if not t.get("in_killzone"): conf_miss["Killzone"]+=1
        if not t.get("dxy_confirms"): conf_miss["DXY"]+=1
        total_pips += float(t.get("pips",0) or 0)
    def fmt(d): return "\n".join(f"  - {html.escape(str(k))}: {v}" for k,v in sorted(d.items(),key=lambda x:-x[1]) if v>0) or "  -"
    avg_score = round(sum(scores)/len(scores),1) if scores else 0.0
    avg_pips = round(total_pips/total,1)
    return f"""<b>VortexAlpha — Weekly Loss Analysis</b>
Time: {now_str}
━━━━━━━━━━━━━━━━━━━━━
Summary
  - Total Losses  : {total}
  - Average Pips  : {avg_pips}
  - Average Score : {avg_score}/10
━━━━━━━━━━━━━━━━━━━━━
By Pair
{fmt(pair_cnt)}
━━━━━━━━━━━━━━━━━━━━━
By Regime
{fmt(regime_cnt)}
━━━━━━━━━━━━━━━━━━━━━
Score Range
{chr(10).join(f"  - {k}: {v}" for k,v in score_ranges.items() if v>0) or "  -"}
━━━━━━━━━━━━━━━━━━━━━
Confluence Gaps
{chr(10).join(f'  - {k}: missing in {v} trades' for k,v in conf_miss.items() if v>0) or "  - None"}
━━━━━━━━━━━━━━━━━━━━━""".strip()

def build_monthly_report(trades: list) -> str:
    total = len(trades)
    if total == 0:
        return "<b>VortexAlpha — Monthly Report</b>\n━━━━━━━━━━━━━━━━━━━━━\nNo trades recorded this month."
    now_str = _bd_now_str("%Y-%m-%d")
    closed = [t for t in trades if (t.get("result") or "OPEN") != "OPEN"]
    wins    = sum(1 for t in closed if "WIN" in (t.get("result") or ""))
    losses  = len(closed) - wins
    all_pips  = sum(float(t.get("pips") or 0) for t in closed)
    win_pips  = sum(float(t.get("pips") or 0) for t in closed if "WIN" in (t.get("result") or ""))
    los_pips  = sum(abs(float(t.get("pips") or 0)) for t in closed if "WIN" not in (t.get("result") or ""))
    pf  = round(win_pips/los_pips,2) if los_pips else 0.0
    total_r = round(sum(float(t.get("rr") or 0) for t in closed), 2)
    avg_r = round(total_r/len(closed),2) if closed else 0.0
    pair_w: dict[str,int] = defaultdict(int)
    pair_l: dict[str,int] = defaultdict(int)
    pair_p: dict[str,float] = defaultdict(float)
    for t in closed:
        p = t.get("pair","?")
        if "WIN" in (t.get("result") or ""): pair_w[p]+=1
        else: pair_l[p]+=1
        pair_p[p]+=float(t.get("pips") or 0)
    pair_lines = "\n".join(
        f"  - {html.escape(p)}: {pair_w[p]}W/{pair_l[p]}L ({calc_winrate(pair_w[p],pair_l[p])}%) {'+' if pair_p[p]>=0 else ''}{pair_p[p]:.1f} pips"
        for p in sorted(set(list(pair_w)+list(pair_l)))
    ) or "  -"
    pp = f"+{all_pips:.1f}" if all_pips>=0 else f"{all_pips:.1f}"
    return f"""<b>VortexAlpha — Monthly Report</b>
Date: {now_str}
━━━━━━━━━━━━━━━━━━━━━
Overall (closed trades)
  - Trades         : {len(closed)} (open: {total-len(closed)})
  - Wins / Losses  : {wins} / {losses}
  - Win Rate       : {calc_winrate(wins,losses)}%
  - Net Pips       : {pp}
  - Profit Factor  : {pf}
  - Net R          : {total_r}R
  - Expectancy     : {avg_r}R / trade
━━━━━━━━━━━━━━━━━━━━━
By Pair
{pair_lines}
━━━━━━━━━━━━━━━━━━━━━
Note: rows before v9.1 used unblended pips/RR.""".strip()

def build_shadow_report(rows: list) -> str:
    """[P4-1] Shadow outcomes by score bucket."""
    with state_lock:
        n_open = len(bot_state.get("shadow_open", {}))
    if not rows:
        return (f"<b>VortexAlpha — Shadow Signals (30d)</b>\n━━━━━━━━━━━━━━━━━━━━━\n"
                f"No shadow signals recorded yet. Tracking now: {n_open}")
    buckets = {"6.5–6.9": [], "7.0–7.4": [], "other": []}
    for r in rows:
        sc = float(r.get("score") or 0)
        b = "6.5–6.9" if 6.5 <= sc < 7.0 else ("7.0–7.4" if 7.0 <= sc < 7.5 else "other")
        buckets[b].append(r)
    lines = []
    for name, lst in buckets.items():
        if not lst: continue
        closed = [x for x in lst if x.get("result") not in (None, "OPEN", "EXPIRED")]
        wins = sum(1 for x in closed if (x.get("r_multiple") or 0) > 0)
        rs = [float(x.get("r_multiple") or 0) for x in closed]
        exp = round(sum(rs)/len(rs), 2) if rs else 0.0
        lines.append(f"Score {name}\n  - Signals : {len(lst)} (closed {len(closed)})\n"
                     f"  - Win Rate: {calc_winrate(wins, len(closed)-wins)}%\n"
                     f"  - Net R   : {round(sum(rs),2)}R | Expectancy: {exp}R")
    return (f"<b>VortexAlpha — Shadow Signals (30d)</b>\n━━━━━━━━━━━━━━━━━━━━━\n"
            + "\n━━━━━━━━━━━━━━━━━━━━━\n".join(lines)
            + f"\n━━━━━━━━━━━━━━━━━━━━━\nTracking now: {n_open}\n"
              f"Decision rule: lower the threshold only for a bucket with ≥30 closed "
              f"signals and clearly positive expectancy.")


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 27 — TELEGRAM COMMAND HANDLERS
# ═════════════════════════════════════════════════════════════════════════════

def _basis_status_line() -> str:
    """[A-3] One line per basis pair for /status."""
    out = ""
    with basis_lock:
        items = {k: dict(v) for k, v in _basis.items()}
    for pk in PRICE_BASIS_PAIRS:
        if pk not in PAIR_KEYS: continue
        b = items.get(pk)
        if b:
            age = int((time.time() - b["ts"]) / 60)
            dl = b.get("yahoo_delay_min")
            dtxt = f", Yahoo lag {dl:.0f}m" if dl is not None else ""
            out += f"\n{PAIRS_CFG[pk]['pair_name']} basis     : {b['basis']:+.2f} (GC=F→spot, {age}m ago{dtxt})"
        else:
            out += f"\n{PAIRS_CFG[pk]['pair_name']} basis     : not measured yet"
    return out

def send_status() -> None:
    with state_lock:
        ot       = bot_state.get("open_trades",{})
        n_shadow = len(bot_state.get("shadow_open",{}))
        scanning = bot_state["is_scanning"]
        regimes  = copy.deepcopy(bot_state.get("last_regime",{}))
        tw=bot_state["today_wins"]; tl=bot_state["today_losses"]
        du=bot_state["daily_risk_used"]; dl=bot_state["daily_loss_limit"]
        tp=bot_state["today_pips"]
        ls=bot_state["last_scan_time"] or "-"; ns=bot_state["next_scan_time"] or "-"
        sess=bot_state["current_session"]
        bl_count=len(bot_state.get("bad_conditions",{}))
        last_px=copy.deepcopy(bot_state.get("last_price",{}))
        px_src=bot_state.get("price_source","unknown")
        pm_ok=bot_state.get("price_monitor_ok",False)
    db_ok   = "Connected" if _sb_ready() else "Not configured"
    pm_str  = "Running (30s interval, 1m high/low)" if pm_ok else "Starting..."
    with news_cache_lock:
        ok_at = _news_feed["ok_at"]
    news_str = (f"ForexFactory JSON (updated {_to_bd(datetime.fromtimestamp(ok_at, timezone.utc), '%H:%M')} BDT)"
                if ok_at else "Heuristic fallback")
    regime_lines = "\n".join(f"  {html.escape(k)}: {regime_label(r)}" for k,r in regimes.items()) if isinstance(regimes,dict) else f"  {html.escape(str(regimes))}"
    price_lines = "\n".join(f"  {html.escape(k)}: {f'{v:.5f}' if v else 'Fetching...'}" for k,v in last_px.items()) if isinstance(last_px,dict) else "  Fetching..."
    msg = f"""<b>VortexAlpha {STRATEGY_VERSION} — Status</b>
━━━━━━━━━━━━━━━━━━━━━
Bot Status       : Running
Scan Status      : {'Scanning' if scanning else 'Idle'}
Last Scan        : {html.escape(ls)}
Next Scan        : {html.escape(ns)}
Session          : {html.escape(sess)}
Market           : {'Open' if is_market_open() else 'Closed'}
Trading Window   : {'Active (07:00-17:00 UTC)' if in_valid_session() else 'Outside window'}
Database         : {db_ok}
Pairs            : {html.escape(', '.join(PAIRS_CFG[k]['pair_name'] for k in PAIR_KEYS))}
━━━━━━━━━━━━━━━━━━━━━
Regimes:
{regime_lines}
━━━━━━━━━━━━━━━━━━━━━
Prices ({px_src}):
{price_lines}
Price Monitor    : {pm_str}{_basis_status_line()}
News             : {html.escape(news_str)}
━━━━━━━━━━━━━━━━━━━━━
Open Trades      : {len(ot)}
Shadow Tracking  : {n_shadow}
Wins Today       : {tw}
Losses Today     : {tl}
Win Rate         : {calc_winrate(tw,tl)}%
Pips Today       : {'+' if tp>=0 else ''}{tp}
Risk Used        : {du}% / {dl}% (open: {_open_risk_pct()}%)
Blacklisted Conditions: {bl_count}
━━━━━━━━━━━━━━━━━━━━━
Strategy: AMD 5-Gate ({STRATEGY_VERSION})
Scans: every 15 min at :00/:15/:30/:45 +20s UTC | Cooldown: 4h per pair"""
    send_telegram(msg, main_menu_buttons())

def send_signals_msg() -> None:
    with state_lock:
        open_trades = copy.deepcopy(bot_state.get("open_trades",{}))
    if not open_trades:
        send_telegram("<b>VortexAlpha — Active Signals</b>\n━━━━━━━━━━━━━━━━━━━━━\nNo active signals.", main_menu_buttons())
        return
    msg = "<b>VortexAlpha — Active Signals</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n"
    for i,(key,trade) in enumerate(open_trades.items(),1):
        pk  = trade.get("pair_key", PAIR_KEYS[0])
        cfg = PAIRS_CFG.get(pk, PAIRS_CFG[PAIR_KEYS[0]])
        d   = html.escape(trade["direction"])
        score = trade.get("score","-"); conf = trade.get("confidence","B")
        size  = trade.get("position_size",100)
        partial = trade.get("partial_tp_done",False)
        t_sl = trade.get("trailing_sl")
        sl_disp = f"<code>{t_sl}</code> (trailing)" if t_sl else f"<code>{trade['sl']}</code>"
        try: strength = _strength_label(float(score))
        except Exception: strength = "-"
        msg += (f"<b>#{i}</b> — {d} {cfg['pair_name']}\n"
                f"Regime: {regime_label(trade.get('regime',''))} | Confidence: {html.escape(conf)} | Score: {score}/10\n"
                f"Entry : <code>{trade['entry']}</code>\nStop Loss   : {sl_disp}\n"
                f"Original SL : <code>{trade.get('orig_sl', '-')}</code>\n"
                f"TP1  : <code>{trade['tp1']}</code>\nTP2  : <code>{trade.get('tp2','-')}</code>\n"
                f"Position: {size}%{' | Partial closed' if partial else ''} | {strength}\n"
                f"Opened: {_to_bd_from_str(trade.get('time','-'))}\n━━━━━━━━━━━━━━━━━━━━━\n\n")
    send_telegram(msg, main_menu_buttons())

def send_performance() -> None:
    with state_lock:
        tw=bot_state["today_wins"]; tl=bot_state["today_losses"]
        ww=bot_state["week_wins"]; wl=bot_state["week_losses"]
        mw=bot_state["month_wins"]; ml=bot_state["month_losses"]
        tp=bot_state["today_pips"]; wp=bot_state["week_pips"]; mp=bot_state["month_pips"]
        ps=copy.deepcopy(bot_state.get("pair_stats",{}))
        twp=bot_state.get("total_win_pips",0.0); tlp=bot_state.get("total_loss_pips",0.0)
    def pf2(v): return f"+{v}" if v>=0 else str(v)
    pair_lines = ""
    for pk,cfg in PAIRS_CFG.items():
        pn=cfg["pair_name"]; s=ps.get(pn,{})
        if pk not in PAIR_KEYS and not s: continue
        w=s.get("wins",0); l=s.get("losses",0); p=s.get("pips",0.0)
        pair_lines += (f"\n  {pn}: {w}W/{l}L | WR:{calc_winrate(w,l)}% | {pf2(p)} pips | Expectancy:{calc_expectancy(s)}")
    pf = round(twp/tlp,2) if tlp else 0.0
    msg = f"""<b>VortexAlpha — Performance</b> (blended, whole position)
━━━━━━━━━━━━━━━━━━━━━
Today
  {tw}W / {tl}L | {calc_winrate(tw,tl)}% | {pf2(tp)} pips
━━━━━━━━━━━━━━━━━━━━━
This Week
  {ww}W / {wl}L | {calc_winrate(ww,wl)}% | {pf2(wp)} pips
━━━━━━━━━━━━━━━━━━━━━
This Month
  {mw}W / {ml}L | {calc_winrate(mw,ml)}% | {pf2(mp)} pips
━━━━━━━━━━━━━━━━━━━━━
All-Time (since last monthly reset)
  Profit Factor  : {pf}
  Total Win Pips : {pf2(twp)}
  Total Loss Pips: -{tlp}
━━━━━━━━━━━━━━━━━━━━━
By Pair{pair_lines}"""
    send_telegram(msg, main_menu_buttons())

def send_journal() -> None:
    with state_lock:
        tw=bot_state["today_wins"]; tl=bot_state["today_losses"]
        tp=bot_state["today_pips"]; sc=bot_state["signal_count"]
        du=bot_state["daily_risk_used"]
        regimes=copy.deepcopy(bot_state.get("last_regime",{}))
        bl=len(bot_state.get("bad_conditions",{}))
    now_bd=_bd_now_str("%Y-%m-%d %H:%M")+" BDT"
    regime_lines="\n".join(f"  {html.escape(k)}: {regime_label(r)}" for k,r in (regimes.items() if isinstance(regimes,dict) else {})) or "  -"
    msg = f"""<b>VortexAlpha — Daily Journal</b>
Date: {now_bd}
━━━━━━━━━━━━━━━━━━━━━
Regimes:
{regime_lines}
━━━━━━━━━━━━━━━━━━━━━
Wins    : {tw}
Losses  : {tl}
Win Rate: {calc_winrate(tw,tl)}%
Pips    : {'+' if tp>=0 else ''}{tp}
Signals : {sc}
Risk Used: {du}% / {MAX_DAILY_LOSS_PCT}%
Blacklisted Conditions: {bl}
━━━━━━━━━━━━━━━━━━━━━
Strategy: AMD 5-Gate ({STRATEGY_VERSION})
Session : 07:00-17:00 UTC | Cooldown: 4h per pair"""
    send_telegram(msg, main_menu_buttons())

def send_risk_status() -> None:
    with state_lock:
        du=bot_state["daily_risk_used"]; dl=bot_state["daily_loss_limit"]
        trades=copy.deepcopy(bot_state.get("open_trades",{})); tp=bot_state["today_pips"]
    open_risk=_open_risk_pct()
    remaining=round(dl-du-open_risk,2); pct_used=round(du/dl*100,1) if dl else 0
    pair_lines=""
    for pk in PAIR_KEYS:
        count=sum(1 for t in trades.values() if t.get("pair_key")==pk)
        if count: pair_lines+=f"\n  {PAIRS_CFG[pk]['pair_name']}: {count} open"
    msg = f"""<b>VortexAlpha — Risk Status</b>
━━━━━━━━━━━━━━━━━━━━━
Daily Limit      : {dl}%
Realised Used    : {du}% ({pct_used}% of limit)
Open Risk        : {open_risk}% (trades not yet at breakeven)
Available        : {max(0.0, remaining)}%
Today P/L        : {'+' if tp>=0 else ''}{tp} pips
━━━━━━━━━━━━━━━━━━━━━
Open Trades:{pair_lines or ' None'}
━━━━━━━━━━━━━━━━━━━━━
{'No capacity for new trades today.' if remaining < MIN_RISK_PCT else 'Within risk limits.'}"""
    send_telegram(msg, main_menu_buttons())

def send_shadow_stats() -> None:
    send_telegram(build_shadow_report(sb_fetch_shadow(30)), main_menu_buttons())

def send_help() -> None:
    pairs_txt=" | ".join(PAIRS_CFG[k]['pair_name'] for k in PAIR_KEYS)
    msg = f"""<b>VortexAlpha {STRATEGY_VERSION} — Help</b>
━━━━━━━━━━━━━━━━━━━━━
/status      - Bot status
/signals     - Active signals
/performance - Statistics (blended P&amp;L)
/journal     - Today's journal
/risk        - Risk status (realised + open)
/shadow      - Shadow-signal results by score bucket
/help        - This help message
━━━━━━━━━━━━━━━━━━━━━
Strategy: AMD 5-Gate Sweep Reversal
Pairs: {pairs_txt}
Session: 07:00-17:00 UTC

Gates (closed 1H candles, strictly in order):
  1. Bias: Daily leads (Weekly opposed = -1.5 score)
  2. Sweep: {SWEEP_THRESHOLD_MULT}xATR pierce, close back inside required
  3. Displacement: body >= {DISP_BODY_ATR_MIN}xATR, at/after the sweep
  4. FVG: from the displacement candle, not invalidated
  5. Retest: live price at the FVG within {FVG_RETEST_MAX_WAIT} candles

Stop Loss: FVG boundary +/- {SL_FVG_BUFFER}xATR
TP1: 1.8R (50% close, SL to breakeven) | TP2: 3.0R
Results are reported as blended R over the whole position.

Risk: Dynamic 0.5-1.5% | Daily limit {MAX_DAILY_LOSS_PCT}% (realised + open)
Shadow mode: scores {SHADOW_SCORE_MIN} to threshold are tracked, not sent.
All times shown in Bangladesh Time (UTC+6).
━━━━━━━━━━━━━━━━━━━━━"""
    send_telegram(msg, main_menu_buttons())


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 28 — SCHEDULED REPORTS & DAILY RESET  [P2-3]
#  Markers live in bot_state (persisted), not module globals.
# ═════════════════════════════════════════════════════════════════════════════

def _daily_reset() -> None:
    now_bd=datetime.now(timezone.utc)+BD_OFFSET; today=now_bd.date().isoformat()
    changed=False
    with state_lock:
        if bot_state.get("last_reset_day")!=today:
            bot_state.update({"today_wins":0,"today_losses":0,"today_pips":0.0,
                              "daily_risk_used":0.0,"signal_count":0,"last_reset_day":today})
            changed=True; log.info(f"[Reset] Daily reset (BD: {today})")
        if now_bd.weekday()==0 and bot_state.get("last_reset_week")!=today:
            bot_state.update({"week_wins":0,"week_losses":0,"week_pips":0.0,"last_reset_week":today})
            changed=True; log.info("[Reset] Weekly reset")
        if now_bd.day==1 and bot_state.get("last_reset_month")!=today:
            bot_state.update({"month_wins":0,"month_losses":0,"month_pips":0.0,"pair_stats":{},
                              "total_win_pips":0.0,"total_loss_pips":0.0,"bad_conditions":{},
                              "last_reset_month":today})
            changed=True; log.info("[Reset] Monthly reset")
    if changed: save_persisted_state()

def _maybe_send_daily_summary() -> None:
    """22:00 BDT, Monday–Friday only (weekend skipped)."""
    now_bd=datetime.now(timezone.utc)+BD_OFFSET; today=now_bd.date().isoformat()
    with state_lock:
        done = bot_state.get("last_summary_day")==today
    if done or now_bd.hour!=22: return
    with state_lock: bot_state["last_summary_day"]=today
    save_persisted_state()
    if now_bd.weekday() in (5, 6):
        log.info("[Schedule] Daily summary skipped (weekend)"); return
    with state_lock:
        tw=bot_state["today_wins"]; tl=bot_state["today_losses"]
        tp=bot_state["today_pips"]; sc=bot_state["signal_count"]
        du=bot_state["daily_risk_used"]; regimes=copy.deepcopy(bot_state.get("last_regime",{}))
    entry={"date":today,"pair":"+".join(PAIRS_CFG[k]["pair_name"] for k in PAIR_KEYS),
           "wins":tw,"losses":tl,"total_pips":tp,
           "signal_count":sc,"risk_used_pct":du,"win_rate":calc_winrate(tw,tl),
           "notes":f"{STRATEGY_VERSION} Regimes={regimes}","created_at":datetime.now(timezone.utc).isoformat()}
    sb_insert_journal(entry); send_performance(); log.info("[Schedule] Daily summary sent")

def _maybe_send_weekly_report() -> None:
    now_bd=datetime.now(timezone.utc)+BD_OFFSET
    if now_bd.weekday()!=6 or now_bd.hour!=22: return
    today=now_bd.date().isoformat()
    with state_lock:
        if bot_state.get("last_weekly_report_day")==today: return
        bot_state["last_weekly_report_day"]=today
    save_persisted_state()
    send_telegram(build_weekly_loss_report(sb_fetch_weekly_losses()), main_menu_buttons())
    if SHADOW_ENABLED:
        send_shadow_stats()

def _maybe_send_monthly_report() -> None:
    now_bd=datetime.now(timezone.utc)+BD_OFFSET
    if now_bd.day!=1 or now_bd.hour!=9: return
    today=now_bd.date().isoformat()
    with state_lock:
        if bot_state.get("last_monthly_report_day")==today: return
        bot_state["last_monthly_report_day"]=today
    save_persisted_state()
    send_telegram(build_monthly_report(sb_fetch_monthly_trades()), main_menu_buttons())


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 29 — COMMAND LISTENER
# ═════════════════════════════════════════════════════════════════════════════

_last_update_id = 0

def listen_commands() -> None:
    global _last_update_id
    log.info("[Telegram] Command listener started")
    command_map = {"/status":send_status,"/signals":send_signals_msg,
                   "/performance":send_performance,"/journal":send_journal,
                   "/risk":send_risk_status,"/shadow":send_shadow_stats,"/help":send_help}
    while True:
        try:
            res=requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={_last_update_id+1}&timeout=30",timeout=35)
            data=res.json()
            if not data.get("ok"): time.sleep(5); continue
            for update in data.get("result",[]):
                _last_update_id=update["update_id"]
                msg=update.get("message",{}); chat_id=str(msg.get("chat",{}).get("id",""))
                text=msg.get("text","")
                if text and chat_id and chat_id!=str(TELEGRAM_CHAT_ID):
                    log.warning(f"[Telegram] Unauthorized from chat_id={chat_id}"); continue
                for cmd,fn in command_map.items():
                    if text.startswith(cmd): fn(); break
                cb=update.get("callback_query",{})
                if cb:
                    cb_chat=str(cb.get("message",{}).get("chat",{}).get("id",""))
                    answer_callback(cb.get("id",""))
                    if cb_chat and cb_chat!=str(TELEGRAM_CHAT_ID):
                        log.warning(f"[Telegram] Unauthorized callback from chat_id={cb_chat}"); continue
                    fn=command_map.get(f"/{cb.get('data','')}")
                    if fn: fn()
        except Exception as e:
            log.error(f"[Telegram] Listener error: {e}"); time.sleep(5)


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 30 — HEALTH CHECK SERVER
# ═════════════════════════════════════════════════════════════════════════════

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(f"VortexAlpha {STRATEGY_VERSION} OK".encode())
    def log_message(self, *_args): pass

def _start_health_server():
    port=int(os.environ.get("PORT",8080))
    server=HTTPServer(("0.0.0.0",port),_HealthHandler)
    log.info(f"[Health] Server started on port {port}"); server.serve_forever()


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 31 — MAIN SCAN
# ═════════════════════════════════════════════════════════════════════════════

def scan_single_pair(pair_key, dxy_bias, news_info, sess, wc, dc, zc, ec, m15, live_price=None):
    cfg=PAIRS_CFG[pair_key]
    with state_lock:
        raw_ts=bot_state["last_signal"].get(pair_key,0)
    try: last_sig_t=float(raw_ts)
    except Exception: last_sig_t=0.0
    elapsed=time.time()-last_sig_t
    if elapsed<cfg["cooldown"]:
        log.info(f"  {pair_key}: Cooldown – {int((cfg['cooldown']-elapsed)/60)} min remaining"); return
    if not all([wc,dc,zc,ec,m15]):
        log.warning(f"  {pair_key}: Missing candle data – skip"); return

    weekly_bias=get_weekly_bias(wc); daily_bias=get_bias(dc)
    log.info(f"  {pair_key}: weekly={weekly_bias} daily={daily_bias}")
    if daily_bias=="neutral" and weekly_bias=="neutral":
        log.info(f"  {pair_key}: SKIP – both neutral"); return

    atr=calculate_atr(zc); adx=calculate_adx(zc)
    sw=count_clear_swings(zc); disp_val=get_disp(zc,atr); vol=get_vol_ratio(zc,atr)
    regime=detect_regime(dc)
    with state_lock:
        if isinstance(bot_state["last_regime"],dict): bot_state["last_regime"][pair_key]=regime
        else: bot_state["last_regime"]={pair_key:regime}
    if regime=="CHAOS":
        log.info(f"  {pair_key}: SKIP – CHAOS regime"); return

    result=analyze_pair(pair_key=pair_key, daily_bias=daily_bias, weekly_bias=weekly_bias,
                        regime=regime, wc=wc, dc=dc, zc=zc, ec=ec, m15=m15,
                        atr=atr, adx=adx, sw=sw, disp=disp_val, vol=vol,
                        news_info=news_info, sess=sess, dxy_bias=dxy_bias,
                        live_price=live_price)
    if not result: return

    # [P4-1] below-threshold setups → shadow table only
    if result["shadow"]:
        log_shadow_signal(pair_key, result, regime, sess, news_info); return

    ni=news_info["impact"]
    base=get_risk_for_news(ni,cfg["base_risk"])
    if base==0:
        log.info(f"  {pair_key}: SKIP – risk=0 (news)"); return
    risk_pct=get_dynamic_risk(base,result["score"],regime)
    if ni=="MEDIUM":
        risk_pct=min(risk_pct,0.5)            # [P2-5] news cap applied last
    pos_size=get_pos_size(regime,result["score"])

    # [P2-4] used + open + new <= daily limit (scale down, or skip)
    with state_lock:
        used=bot_state["daily_risk_used"]; limit=bot_state["daily_loss_limit"]
    open_risk=_open_risk_pct()
    available=round(limit-used-open_risk,2)
    if risk_pct>available:
        if available>=MIN_RISK_PCT:
            log.info(f"  {pair_key}: risk scaled {risk_pct}% → {available}% "
                     f"(used {used}% + open {open_risk}% of {limit}%)")
            risk_pct=available
        else:
            log.info(f"  {pair_key}: SKIP – no risk capacity (used {used}% + open {open_risk}% "
                     f"of {limit}%)"); return
    risk_pct=round(risk_pct,2)

    sig_hash=_make_signal_hash(cfg["pair_name"],result["direction"],
                               result["fvg_top"], result["fvg_bottom"])
    if is_duplicate_signal(sig_hash):
        log.warning(f"  {pair_key}: SKIP – duplicate (same FVG zone within 24h)"); return

    with state_lock: bot_state["signal_count"]+=1
    msg=build_signal_message(result,pair_key,regime,sess,risk_pct,pos_size,news_info)
    sent=send_telegram(msg,main_menu_buttons())
    if not sent: return

    register_signal(sig_hash)
    now_dt=datetime.now(timezone.utc)
    sl_pips_val=round(abs(result["entry"]-result["sl"])*cfg["pip_mult"],1)
    sweep_info=result.get("sweep_info") or {}
    trade_data={
        "pair_key": pair_key, "pair_name": cfg["pair_name"],
        "direction": result["direction"],
        "entry": result["entry"], "sl": result["sl"],
        "orig_sl": result["sl"],                                   # [P2-1] never overwritten
        "initial_risk": abs(result["entry"]-result["sl"]),         # [P2-1]
        "tp1": result["tp1"], "tp2": result["tp2"],
        "regime": regime, "bias": result.get("effective_bias",daily_bias),
        "score": result["score"], "confidence": result["confidence"],
        "risk_pct": risk_pct, "position_size": pos_size, "news_impact": ni,
        "has_ob": result["has_ob"], "ob_age": result["ob_age"],
        "ob_pattern": result["ob_pattern"], "near_htf_ob": result["near_htf_ob"],
        "htf_strength": result["htf_strength"], "vp_confluence": result["vp_confluence"],
        "vp_label": result["vp_label"], "sweep_confirmed": result["sweep_confirmed"],
        "sweep_price": result.get("sweep_price"),
        "sweep_pierce_atr": sweep_info.get("dist"),                # v9 always stored None
        "disp_confirmed": result["disp_confirmed"],
        "fvg_top": result.get("fvg_top"), "fvg_bottom": result.get("fvg_bottom"),
        "fvg_touch_count": result.get("fvg_touch_count"),
        "mss_confirmed": result["mss_confirmed"], "choch_confirmed": result["choch_confirmed"],
        "liq_zone": result["liq_zone"], "liq_strength": result["liq_strength"],
        "entry_type": result["entry_type"], "signal_type": result["signal_type"],
        "session": sess, "weekly_confluence": result["weekly_confluence"],
        "weekly_conflict": bool(result.get("weekly_conflict", False)),        # [G1-1]
        "in_killzone": result["in_killzone"], "killzone_name": result["killzone_name"],
        "dxy_confirms": result["dxy_confirms"], "dxy_bias": result["dxy_bias"],
        "time": now_dt.strftime("%Y-%m-%d %H:%M"), "open_ts": now_dt.timestamp(),
        "atr": result["atr"], "sig_hash": sig_hash,
        "partial_tp_done": False, "trailing_sl": None,
        "sl_size_pips": sl_pips_val,
        "max_adverse_pips": 0.0, "max_favorable_pips": 0.0,
    }
    db_id=sb_insert_trade(trade_data)
    if db_id is None:
        send_telegram(f"<b>VortexAlpha — Database Alert</b>\n{html.escape(cfg['pair_name'])} "
                      f"signal was NOT saved to the database.", main_menu_buttons())
    trade_data["db_id"]=db_id
    trade_key=f"{cfg['pair_name']}_{int(time.time())}"
    with state_lock:
        bot_state["last_signal"][pair_key]=time.time()
        bot_state["open_trades"][trade_key]=trade_data
    save_persisted_state()
    log.info(f"  [Scan] Signal sent – {pair_key} {result['direction']} score={result['score']} risk={risk_pct}%")

def scan() -> None:
    if not scan_lock.acquire(blocking=False):
        log.warning("[Scan] Already running – skip"); return
    try:
        log.info("="*60); log.info(f"[Scan] Started – {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC"); log.info("="*60)
        _daily_reset(); _maybe_send_daily_summary()
        _maybe_send_weekly_report(); _maybe_send_monthly_report()
        purge_expired_signals()
        with state_lock:
            bot_state["is_scanning"]=True
            bot_state["last_scan_time"]=_bd_now_str("%Y-%m-%d %H:%M")+" BDT"
            bot_state["current_session"]=session_name()
        if not is_market_open():
            log.info("[Scan] Weekend – market closed"); return
        if not in_valid_session(): return
        with state_lock:
            risk_used=bot_state["daily_risk_used"]; risk_limit=bot_state["daily_loss_limit"]
        if risk_used>=risk_limit:
            log.info(f"[Scan] Daily loss limit hit ({risk_used}%/{risk_limit}%)"); return

        sess=session_name(); news_info=check_news_impact(); ni=news_info["impact"]
        log.info(f"[Scan] Session={sess} News={ni} ({news_info.get('source')}) Blocked={news_info.get('blocked',False)}")
        if news_info.get("blocked",False):
            reason=news_info.get("block_reason","High impact event")
            send_telegram(f"<b>VortexAlpha — News Block</b>\n{html.escape(reason)}\n"
                          f"Trading paused for +/-{NEWS_BLOCK_MINUTES} minutes.", main_menu_buttons()); return
        if ni=="HIGH":
            send_telegram("<b>VortexAlpha — News Alert</b>\nHigh impact event detected. "
                          "No trades will be taken.", main_menu_buttons()); return

        # [P3-2] latency-critical data first (1H / 15M, blocking), then HTF
        # (served one slot stale rather than waiting on credits)
        candle_data: dict[str,dict[str,list]] = {pk: {} for pk in PAIR_KEYS}
        for pk in PAIR_KEYS:
            candle_data[pk]["ec"]=fetch_slot_cached(pk,"1h",100,critical=True)
            candle_data[pk]["m15"]=fetch_slot_cached(pk,"15min",20,critical=True)
        # [A-3a] gold basis right after the critical calls (6 + 1 ≤ 8 credits),
        # before HTF, so it never waits on the limiter.
        basis_live={pk: measure_basis_at_scan(pk, candle_data[pk].get("ec"))
                    for pk in PAIR_KEYS if pk in PRICE_BASIS_PAIRS}
        # [P3-6] interval-major order: every pair's 4H before any Daily/Weekly
        for key, iv, cnt in HTF_SPECS:
            for pk in PAIR_KEYS:
                candle_data[pk][key]=fetch_slot_cached(pk,iv,cnt,critical=False)
        for pk in PAIR_KEYS:
            log.info(f"  {pk}: " + " ".join(f"{k}={len(v)}" for k,v in candle_data[pk].items()))

        dxy_bias=get_dxy_bias()
        live_prices={}
        for pk in PAIR_KEYS:
            if pk in PRICE_BASIS_PAIRS:
                # [A-3] Gate-5 price = Twelve Data spot (forming 1H close), measured
                # above together with the GC=F basis used by the price monitor.
                live_prices[pk]=basis_live.get(pk)
            else:
                live_prices[pk]=get_current_price(pk)[0]      # unchanged v9.1 path

        with state_lock: pm_running=bot_state.get("price_monitor_ok",False)
        if not pm_running:
            for pk in PAIR_KEYS:
                px=live_prices.get(pk) or (candle_data[pk]["ec"][-1]["close"] if candle_data[pk].get("ec") else None)
                if px: check_open_trades_for_pair(pk, px, _get_cached_atr(pk), None)

        with state_lock: risk_used=bot_state["daily_risk_used"]
        if risk_used>=risk_limit:
            log.info("[Scan] Risk limit hit – stopping"); return

        for pk in PAIR_KEYS:
            log.info(f"\n── Analysing {pk} ──")
            scan_single_pair(pair_key=pk, dxy_bias=dxy_bias, news_info=news_info, sess=sess,
                             wc=candle_data[pk]["wc"], dc=candle_data[pk]["dc"],
                             zc=candle_data[pk]["zc"], ec=candle_data[pk]["ec"],
                             m15=candle_data[pk]["m15"], live_price=live_prices.get(pk))
        log.info("[Scan] Completed")
    except Exception as e:
        log.error(f"[Scan] Unexpected error: {e}", exc_info=True)
    finally:
        with state_lock: bot_state["is_scanning"]=False
        scan_lock.release()

# [P3-6] HTF fetch specs (same intervals/counts as v9.1) and catch-up pass.
HTF_SPECS = (("zc", "4h", 100), ("dc", "1day", 50), ("wc", "1week", 20))

def htf_catchup() -> None:
    """
    [P3-6] Between scans, refresh any 4H/Daily/Weekly slot the scan had to
    serve stale because the per-minute credit budget was used by critical
    1H/15M calls. Same fetches, same credits — only earlier. With 3 pairs this
    removes the 16:00 UTC backlog where a Weekly/Daily refresh was skipped.
    """
    if not is_market_open() or not (SESSION_START_UTC <= datetime.now(timezone.utc).hour < SESSION_END_UTC):
        return
    if not scan_lock.acquire(blocking=False):
        return
    try:
        for _key, iv, cnt in HTF_SPECS:
            for pk in PAIR_KEYS:
                fetch_slot_cached(pk, iv, cnt, critical=False)
    except Exception as e:
        log.error(f"[HTF] catch-up error: {e}", exc_info=True)
    finally:
        scan_lock.release()

def _next_event(now_ts: float) -> tuple[float, str]:
    """Next scan (:00/:15/:30/:45 +20s) or HTF catch-up (+7m20s), whichever is first."""
    base = (now_ts // SCAN_INTERVAL) * SCAN_INTERVAL
    cands = []
    for k in (0, 1):
        b = base + k * SCAN_INTERVAL
        cands += [(b + SCAN_OFFSET_SEC, "scan"), (b + HTF_CATCHUP_OFFSET_SEC, "htf")]
    return min((c for c in cands if c[0] > now_ts + 1), key=lambda c: c[0])

def _next_aligned_ts(now_ts: float) -> float:
    """[P3-1] Next :00/:15/:30/:45 + SCAN_OFFSET_SEC (UTC wall clock)."""
    base = (now_ts // SCAN_INTERVAL) * SCAN_INTERVAL + SCAN_OFFSET_SEC
    while base <= now_ts + 1:
        base += SCAN_INTERVAL
    return base


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 31b — [H-21] 3-MONTH DEMO REVIEW SQL (run in the Supabase SQL editor)
# ═════════════════════════════════════════════════════════════════════════════
# R per trade = blended R-multiple (r_multiple; falls back to rr). Breakeven
# trades (r = 0) count as neither win nor loss in PF. Adjust the date range.
REVIEW_SQL = """
with t as (
  select
    pair,
    case when score >= 7.5 then '>=7.5'
         when score >= 7.0 then '7.0-7.4'
         else '<7.0' end                               as score_bucket,
    coalesce(weekly_conflict, false)                   as weekly_conflict,
    coalesce(r_multiple, rr)::numeric                  as r
  from trades
  where strategy_version = 'v9.2'
    and result is not null and result <> 'OPEN'
    and coalesce(r_multiple, rr) is not null
    -- and date between '2026-10-01' and '2026-12-31'
)
select
  case when grouping(pair) = 1 then 'ALL' else pair end                       as pair,
  case when grouping(score_bucket) = 1 then 'ALL' else score_bucket end       as score_bucket,
  case when grouping(weekly_conflict) = 1 then 'ALL'
       else weekly_conflict::text end                                          as weekly_conflict,
  count(*)                                                                     as trades,
  round(100.0 * avg((r > 0)::int), 1)                                          as win_rate_pct,
  round(avg(r), 3)                                                             as avg_r,
  round(sum(r), 2)                                                             as total_r,
  round(sum(r) filter (where r > 0) / nullif(abs(sum(r) filter (where r < 0)), 0), 2)
                                                                               as profit_factor
from t
group by grouping sets (
  (),                                  -- overall
  (score_bucket),                      -- 7.0-7.4 vs >=7.5
  (pair),                              -- per pair
  (weekly_conflict),                   -- conflict vs aligned
  (pair, score_bucket),
  (score_bucket, weekly_conflict)
)
order by grouping(pair) desc, pair, score_bucket, weekly_conflict;

-- Shadow band (6.5–6.9) — same metrics from the virtual trades:
with sh as (
  select pair,
         coalesce(weekly_conflict, false) as weekly_conflict,
         r_multiple::numeric              as r
  from shadow_signals
  where strategy_version = 'v9.2'
    and result not in ('OPEN', 'EXPIRED') and r_multiple is not null
)
select
  case when grouping(pair) = 1 then 'ALL' else pair end                       as pair,
  case when grouping(weekly_conflict) = 1 then 'ALL'
       else weekly_conflict::text end                                          as weekly_conflict,
  count(*)                                                                     as trades,
  round(100.0 * avg((r > 0)::int), 1)                                          as win_rate_pct,
  round(avg(r), 3)                                                             as avg_r,
  round(sum(r) filter (where r > 0) / nullif(abs(sum(r) filter (where r < 0)), 0), 2)
                                                                               as profit_factor
from sh
group by grouping sets ((), (pair), (weekly_conflict))
order by 1, 2;
"""


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION 32 — ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log.info(f"VortexAlpha {STRATEGY_VERSION} – Starting")
    log.info(f"   API Key  : {'OK' if TWELVE_DATA_API_KEY else 'MISSING'} ({TD_CREDITS_PER_MIN} credits/min)")
    log.info(f"   Telegram : {'OK' if TELEGRAM_TOKEN     else 'MISSING'}")
    log.info(f"   Supabase : {'OK' if SUPABASE_URL       else 'MISSING'}")
    log.info(f"   Pairs    : {', '.join(PAIR_KEYS)}")
    log.info(f"   Session  : {SESSION_START_UTC}:00 – {SESSION_END_UTC}:00 UTC")
    log.info(f"   Gates    : Bias -> Sweep -> Displacement -> FVG -> Live retest (chronological, closed candles)")
    log.info(f"   Shadow   : {'ON' if SHADOW_ENABLED else 'OFF'} (score {SHADOW_SCORE_MIN} to threshold)")

    load_persisted_state()

    threading.Thread(target=_start_health_server, daemon=True, name="HealthServer").start()
    threading.Thread(target=listen_commands,       daemon=True, name="CmdListener").start()
    start_price_monitor()

    pairs_txt=" + ".join(PAIRS_CFG[k]['pair_name'] for k in PAIR_KEYS)
    send_telegram(
        f"<b>VortexAlpha — Bot Started ({STRATEGY_VERSION})</b>\n\n"
        f"Pairs: {pairs_txt}\n\n"
        f"Gates (closed 1H candles, strictly in order):\n"
        f"  1. Bias: Daily leads (Weekly opposed = {SCORE_WEEKLY_CONFLICT} score)\n"
        f"  2. Sweep: close back inside required\n"
        f"  3. Displacement: >= {DISP_BODY_ATR_MIN}xATR body, at/after the sweep\n"
        f"  4. FVG: from displacement, not invalidated\n"
        f"  5. Retest: live price at the FVG ({FVG_RETEST_MAX_WAIT} candle window)\n\n"
        f"Stop Loss: FVG boundary +/- {SL_FVG_BUFFER}xATR\n"
        f"Take Profit 1: 1.8R (50% close) | Take Profit 2: 3.0R\n"
        f"Score threshold: {PAIRS_CFG[PAIR_KEYS[0]]['score_threshold']} (demo test)\n"
        f"Shadow mode: {'ON' if SHADOW_ENABLED else 'OFF'} (score {SHADOW_SCORE_MIN} to threshold)\n\n"
        f"All times shown in Bangladesh Time (UTC+6).",
        main_menu_buttons(),
    )

    while True:
        try:
            target, kind = _next_event(time.time())
            with state_lock:
                bot_state["next_scan_time"]=_to_bd(datetime.fromtimestamp(
                    _next_aligned_ts(time.time()), timezone.utc), "%H:%M:%S")+" BDT"
            time.sleep(max(0.0, target-time.time()))
            if kind == "scan":
                scan()
            else:
                htf_catchup()                     # [P3-6]
        except KeyboardInterrupt:
            log.info("\n[VortexAlpha] Shutting down.")
            price_monitor_stop.set(); break
        except Exception as e:
            log.error(f"[Main] Loop error: {e}", exc_info=True)
            time.sleep(5)
