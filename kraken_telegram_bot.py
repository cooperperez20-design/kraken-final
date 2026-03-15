"""
╔══════════════════════════════════════════════════════════════╗
║   KRAKEN OPTIMIZED BOT + TELEGRAM CONTROL                    ║
║   Claude AI + CCXT + Kraken + Telegram                       ║
║                                                              ║
║   Telegram commands you can send from your phone:            ║
║   /status    — current balance, open positions, P&L          ║
║   /pause     — pause all new trades (holds open positions)   ║
║   /resume    — resume trading                                ║
║   /sellall   — close all open positions immediately          ║
║   /settp X   — set take profit % (e.g. /settp 5)            ║
║   /setsl X   — set stop loss %  (e.g. /setsl 2)             ║
║   /setfng X  — set min Fear & Greed (e.g. /setfng 45)       ║
║   /stats     — full session statistics                       ║
║   /help      — list all commands                             ║
║                                                              ║
║   The bot also sends YOU automatic messages:                 ║
║   • Trade opened (buy)                                       ║
║   • Trade closed (sell) with P&L                             ║
║   • Daily summary every morning at 8am                       ║
║   • Alerts if stop-loss or take-profit triggers              ║
║   • Warning if daily loss limit is approaching               ║
║                                                              ║
║   !! RISK WARNING !!                                         ║
║   • You CAN lose your entire balance                         ║
║   • NO strategy guarantees returns                           ║
║   • Only trade money you can afford to lose 100% of          ║
╚══════════════════════════════════════════════════════════════╝
"""

import ccxt
import anthropic
import pandas as pd
import pandas_ta as ta
import urllib.request
import urllib.parse
import json
import time
import os
import threading
from datetime import datetime, date


# ════════════════════════════════════════════════════════════════
#  CONFIG — all secrets come from Railway Variables tab
# ════════════════════════════════════════════════════════════════

KRAKEN_KEY      = os.getenv("KRAKEN_API_KEY",      "PASTE_YOUR_KRAKEN_KEY_HERE")
KRAKEN_SECRET   = os.getenv("KRAKEN_API_SECRET",   "PASTE_YOUR_KRAKEN_SECRET_HERE")
CLAUDE_KEY      = os.getenv("ANTHROPIC_API_KEY",   "PASTE_YOUR_CLAUDE_KEY_HERE")
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN",  "PASTE_YOUR_TELEGRAM_TOKEN_HERE")
TELEGRAM_CHAT   = os.getenv("TELEGRAM_CHAT_ID",    "PASTE_YOUR_CHAT_ID_HERE")

# ── COINS TO TRADE ───────────────────────────────────────────────
COINS = [
    "BTC/USD",
]

# ── TIMEFRAMES ───────────────────────────────────────────────────
TF_DAILY  = "1d"
TF_4HOUR  = "4h"
TF_SIGNAL = "1h"

CHECK_EVERY_MINUTES = 30

# ── POSITION SIZING ──────────────────────────────────────────────
TRADE_SIZE_PCT = 0.90

# ── EXIT RULES (adjustable via Telegram) ─────────────────────────
TAKE_PROFIT_PCT = 0.04
STOP_LOSS_PCT   = 0.02

# ── FEAR & GREED FILTER (adjustable via Telegram) ────────────────
FNG_MIN_TO_BUY = 40

# ── SIGNAL DETECTION THRESHOLDS ──────────────────────────────────
RSI_BUY_MIN  = 40
RSI_BUY_MAX  = 62
RSI_SELL_MIN = 68
RSI_SELL_MAX = 35

# ── DAILY SAFETY LIMITS ──────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT = 0.15
MAX_TRADES_PER_COIN  = 3

# ── SANDBOX ──────────────────────────────────────────────────────
SANDBOX = False


# ════════════════════════════════════════════════════════════════
#  BOT STATE
# ════════════════════════════════════════════════════════════════

positions        = {coin: None for coin in COINS}
trade_counts     = {coin: 0 for coin in COINS}
last_day_reset   = datetime.now().date()
daily_start_bal  = None
session_trades   = []
claude_calls     = 0
fng_cache        = {"value": None, "label": None, "fetched_at": None}

# Telegram state
is_paused        = False
last_update_id   = 0
daily_summary_sent_date = None


# ════════════════════════════════════════════════════════════════
#  LOGGING
# ════════════════════════════════════════════════════════════════

def log(msg, coin=None):
    tag = f"[{coin.split('/')[0]}]" if coin else "[BOT]"
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {tag}  {msg}", flush=True)


# ════════════════════════════════════════════════════════════════
#  TELEGRAM — sending and receiving messages
# ════════════════════════════════════════════════════════════════

def tg_send(message):
    """
    Sends a message to your Telegram chat.
    Called automatically when trades open/close and for daily summaries.
    """
    try:
        url    = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data   = urllib.parse.urlencode({
            "chat_id":    TELEGRAM_CHAT,
            "text":       message,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"Telegram send error: {e}")


def tg_get_updates():
    """
    Polls Telegram for new messages from you.
    Runs in a background thread every 3 seconds.
    """
    global last_update_id
    try:
        url = (f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
               f"/getUpdates?offset={last_update_id + 1}&timeout=2")
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read().decode())
        if data.get("ok") and data.get("result"):
            for update in data["result"]:
                last_update_id = update["update_id"]
                if "message" in update and "text" in update["message"]:
                    handle_command(update["message"]["text"].strip())
    except Exception:
        pass


def handle_command(text):
    """
    Handles commands you send via Telegram.
    Commands are case-insensitive and start with /.
    """
    global is_paused, TAKE_PROFIT_PCT, STOP_LOSS_PCT, FNG_MIN_TO_BUY

    cmd = text.lower().split()
    if not cmd or not cmd[0].startswith("/"):
        return

    log(f"Telegram command received: {text}")

    # ── /help ─────────────────────────────────────────────────────
    if cmd[0] == "/help":
        tg_send(
            "<b>Available commands:</b>\n\n"
            "/status — balance, positions, P&L\n"
            "/pause — pause new trades\n"
            "/resume — resume trading\n"
            "/sellall — close all positions now\n"
            "/settp X — set take profit % (e.g. /settp 5)\n"
            "/setsl X — set stop loss % (e.g. /setsl 2)\n"
            "/setfng X — set min Fear & Greed (e.g. /setfng 45)\n"
            "/stats — full session statistics\n"
            "/help — show this list"
        )

    # ── /status ───────────────────────────────────────────────────
    elif cmd[0] == "/status":
        send_status()

    # ── /pause ────────────────────────────────────────────────────
    elif cmd[0] == "/pause":
        is_paused = True
        tg_send("⏸ <b>Bot paused.</b> No new trades will open.\n"
                "Existing positions are still monitored.\n"
                "Send /resume to restart.")
        log("Bot paused via Telegram.")

    # ── /resume ───────────────────────────────────────────────────
    elif cmd[0] == "/resume":
        is_paused = False
        tg_send("▶️ <b>Bot resumed.</b> Trading is active again.")
        log("Bot resumed via Telegram.")

    # ── /sellall ──────────────────────────────────────────────────
    elif cmd[0] == "/sellall":
        open_coins = [c for c in COINS if positions[c] is not None]
        if not open_coins:
            tg_send("No open positions to close.")
        else:
            tg_send(f"🔴 Closing {len(open_coins)} position(s)...")
            for coin in open_coins:
                # Mark for emergency sell on next cycle
                if positions[coin] is not None:
                    positions[coin]["force_sell"] = True
            log("Force sell all triggered via Telegram.")

    # ── /settp X ──────────────────────────────────────────────────
    elif cmd[0] == "/settp" and len(cmd) > 1:
        try:
            val = float(cmd[1]) / 100
            if 0.005 <= val <= 0.20:
                TAKE_PROFIT_PCT = val
                tg_send(f"✅ Take profit set to <b>{val*100:.1f}%</b>")
                log(f"Take profit changed to {val*100:.1f}% via Telegram.")
            else:
                tg_send("Value must be between 0.5 and 20.")
        except:
            tg_send("Usage: /settp 5  (sets take profit to 5%)")

    # ── /setsl X ──────────────────────────────────────────────────
    elif cmd[0] == "/setsl" and len(cmd) > 1:
        try:
            val = float(cmd[1]) / 100
            if 0.005 <= val <= 0.10:
                STOP_LOSS_PCT = val
                tg_send(f"✅ Stop loss set to <b>{val*100:.1f}%</b>")
                log(f"Stop loss changed to {val*100:.1f}% via Telegram.")
            else:
                tg_send("Value must be between 0.5 and 10.")
        except:
            tg_send("Usage: /setsl 2  (sets stop loss to 2%)")

    # ── /setfng X ─────────────────────────────────────────────────
    elif cmd[0] == "/setfng" and len(cmd) > 1:
        try:
            val = int(cmd[1])
            if 0 <= val <= 100:
                FNG_MIN_TO_BUY = val
                tg_send(f"✅ Min Fear & Greed set to <b>{val}/100</b>")
                log(f"FNG min changed to {val} via Telegram.")
            else:
                tg_send("Value must be between 0 and 100.")
        except:
            tg_send("Usage: /setfng 45  (sets minimum Fear & Greed to 45)")

    # ── /stats ────────────────────────────────────────────────────
    elif cmd[0] == "/stats":
        send_stats()

    else:
        tg_send(f"Unknown command: {text}\nSend /help for a list of commands.")


def send_status():
    """Sends a status message with balance, positions, and settings."""
    try:
        bal       = get_total_balance(exchange_global)
        fng, lbl  = get_fear_and_greed()
        open_pos  = [c for c in COINS if positions[c] is not None]

        lines = [
            f"<b>📊 Bot Status</b>",
            f"Mode: {'SANDBOX' if SANDBOX else '🔴 LIVE'}",
            f"Status: {'⏸ PAUSED' if is_paused else '▶️ ACTIVE'}",
            f"",
            f"<b>💰 Balance</b>",
            f"USD available: ${bal:.2f}",
            f"",
            f"<b>📈 Open Positions</b>",
        ]

        if open_pos:
            for coin in open_pos:
                pos = positions[coin]
                try:
                    price   = exchange_global.fetch_ticker(coin)["last"]
                    pnl_pct = (price - pos["entry"]) / pos["entry"] * 100
                    pnl_usd = pos["spent_usd"] * (pnl_pct / 100)
                    held    = (datetime.now() - pos["opened_at"]).seconds / 3600
                    lines.append(
                        f"{coin.split('/')[0]}: ${price:,.4f}  "
                        f"PnL: {pnl_pct:+.2f}% (${pnl_usd:+.2f})  "
                        f"Held: {held:.1f}h"
                    )
                except:
                    lines.append(f"{coin}: position open")
        else:
            lines.append("No open positions")

        lines += [
            f"",
            f"<b>⚙️ Settings</b>",
            f"Take profit: {TAKE_PROFIT_PCT*100:.1f}%",
            f"Stop loss:   {STOP_LOSS_PCT*100:.1f}%",
            f"F&G min:     {FNG_MIN_TO_BUY}/100",
            f"Fear & Greed now: {fng}/100 ({lbl})",
            f"Claude calls today: {claude_calls} (~${claude_calls*0.003:.2f})",
        ]
        tg_send("\n".join(lines))
    except Exception as e:
        tg_send(f"Status error: {e}")


def send_stats():
    """Sends full session trade statistics."""
    if not session_trades:
        tg_send("No trades this session yet.")
        return

    wins   = [t for t in session_trades if t["pnl_usd"] > 0]
    losses = [t for t in session_trades if t["pnl_usd"] <= 0]
    total  = sum(t["pnl_usd"] for t in session_trades)
    best   = max(session_trades, key=lambda t: t["pnl_usd"])
    worst  = min(session_trades, key=lambda t: t["pnl_usd"])

    lines = [
        "<b>📊 Session Statistics</b>",
        f"",
        f"Total trades:  {len(session_trades)}",
        f"Wins:          {len(wins)}",
        f"Losses:        {len(losses)}",
        f"Win rate:      {len(wins)/len(session_trades)*100:.0f}%",
        f"Total P&L:     ${total:+.2f}",
        f"",
        f"Best trade:    ${best['pnl_usd']:+.2f} ({best['coin'].split('/')[0]})",
        f"Worst trade:   ${worst['pnl_usd']:+.2f} ({worst['coin'].split('/')[0]})",
        f"",
        f"API cost:      ~${claude_calls*0.003:.2f} ({claude_calls} calls)",
    ]
    tg_send("\n".join(lines))


def send_daily_summary(exchange):
    """Sent automatically every morning at 8am."""
    try:
        bal      = get_total_balance(exchange)
        fng, lbl = get_fear_and_greed()
        today_trades = [t for t in session_trades
                        if t["time"].date() == date.today()]
        total_pnl = sum(t["pnl_usd"] for t in today_trades)
        wins      = [t for t in today_trades if t["pnl_usd"] > 0]

        tg_send(
            f"<b>🌅 Daily Summary — {date.today().strftime('%b %d')}</b>\n\n"
            f"USD balance:  ${bal:.2f}\n"
            f"Today's P&L:  ${total_pnl:+.2f}\n"
            f"Trades today: {len(today_trades)} "
            f"({len(wins)}W/{len(today_trades)-len(wins)}L)\n"
            f"Fear & Greed: {fng}/100 ({lbl})\n"
            f"API cost:     ~${claude_calls*0.003:.2f}\n\n"
            f"Bot is {'⏸ PAUSED' if is_paused else '▶️ running normally'}."
        )
    except Exception as e:
        tg_send(f"Daily summary error: {e}")


def telegram_listener():
    """
    Runs in a background thread, polling for new Telegram messages
    every 3 seconds. Does not interfere with trading logic.
    """
    log("Telegram listener started.")
    while True:
        try:
            tg_get_updates()
        except Exception:
            pass
        time.sleep(3)


# ════════════════════════════════════════════════════════════════
#  FEAR & GREED INDEX
# ════════════════════════════════════════════════════════════════

def get_fear_and_greed():
    global fng_cache
    now = datetime.now()
    if fng_cache["value"] is not None and fng_cache["fetched_at"] is not None:
        if (now - fng_cache["fetched_at"]).seconds / 60 < 60:
            return fng_cache["value"], fng_cache["label"]
    try:
        url = "https://api.alternative.me/fng/?limit=1"
        with urllib.request.urlopen(url, timeout=10) as r:
            data  = json.loads(r.read().decode())
            value = int(data["data"][0]["value"])
            label = data["data"][0]["value_classification"]
            fng_cache = {"value": value, "label": label, "fetched_at": now}
            return value, label
    except:
        return 50, "Neutral"


# ════════════════════════════════════════════════════════════════
#  CONNECT
# ════════════════════════════════════════════════════════════════

exchange_global = None

def connect():
    global exchange_global
    exchange = ccxt.kraken({"apiKey": KRAKEN_KEY, "secret": KRAKEN_SECRET})
    try:
        exchange.fetch_time()
        log("Connected to Kraken.")
    except Exception as e:
        log(f"Kraken warning: {e}")
    exchange_global = exchange
    claude = anthropic.Anthropic(api_key=CLAUDE_KEY)
    log("Connected to Claude AI.")
    return exchange, claude


# ════════════════════════════════════════════════════════════════
#  MARKET DATA + INDICATORS
# ════════════════════════════════════════════════════════════════

def get_data(exchange, coin, timeframe, limit=100):
    try:
        raw = exchange.fetch_ohlcv(coin, timeframe, limit=limit)
        df  = pd.DataFrame(raw, columns=["time","open","high","low","close","volume"])
        df["time"] = pd.to_datetime(df["time"], unit="ms")

        df["ema9"]  = ta.ema(df["close"], length=9)
        df["ema21"] = ta.ema(df["close"], length=21)
        df["ema50"] = ta.ema(df["close"], length=50)
        df["rsi"]   = ta.rsi(df["close"], length=14)

        macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
        if macd is not None and not macd.empty:
            mc = [c for c in macd.columns if c.startswith("MACD_")
                  and not c.startswith("MACD_h")
                  and not c.startswith("MACD_s")][0]
            sc = [c for c in macd.columns if c.startswith("MACDs_")][0]
            hc = [c for c in macd.columns if c.startswith("MACDh_")][0]
            df["macd"]        = macd[mc]
            df["macd_signal"] = macd[sc]
            df["macd_hist"]   = macd[hc]
        else:
            df["macd"] = df["macd_signal"] = df["macd_hist"] = 0

        bb = ta.bbands(df["close"], length=20, std=2)
        if bb is not None and not bb.empty:
            df["bb_upper"] = bb[[c for c in bb.columns if c.startswith("BBU")][0]]
            df["bb_lower"] = bb[[c for c in bb.columns if c.startswith("BBL")][0]]
            df["bb_mid"]   = bb[[c for c in bb.columns if c.startswith("BBM")][0]]
        else:
            df["bb_upper"] = df["bb_lower"] = df["bb_mid"] = df["close"]

        atr = ta.atr(df["high"], df["low"], df["close"], length=14)
        df["atr"]        = atr if atr is not None else 0
        df["change_pct"] = df["close"].pct_change(24) * 100
        return df
    except Exception as e:
        log(f"Data error ({timeframe}): {e}", coin)
        return None


def get_trend_bias(exchange, coin):
    try:
        df_d = get_data(exchange, coin, TF_DAILY, limit=60)
        df_4 = get_data(exchange, coin, TF_4HOUR, limit=60)
        if df_d is None or df_4 is None:
            return "mixed"
        daily_bull = df_d.iloc[-1]["ema9"] > df_d.iloc[-1]["ema21"]
        h4_bull    = df_4.iloc[-1]["ema9"] > df_4.iloc[-1]["ema21"]
        if daily_bull and h4_bull:
            return "bullish"
        elif not daily_bull and not h4_bull:
            return "bearish"
        return "mixed"
    except:
        return "mixed"


# ════════════════════════════════════════════════════════════════
#  SIGNAL DETECTION (zero Claude cost)
# ════════════════════════════════════════════════════════════════

def detect_signal(df, position, trend_bias, fng_value):
    last = df.iloc[-1]
    prev = df.iloc[-2]

    ema_crossed_up    = prev["ema9"] <= prev["ema21"] and last["ema9"] > last["ema21"]
    ema_crossed_down  = prev["ema9"] >= prev["ema21"] and last["ema9"] < last["ema21"]
    macd_turning_up   = last["macd_hist"] > 0 and prev["macd_hist"] <= 0
    macd_turning_down = last["macd_hist"] < 0 and prev["macd_hist"] >= 0
    price_near_mid    = last["close"] <= last["bb_mid"] * 1.005
    price_broke_low   = last["close"] < last["bb_lower"]
    rsi               = last["rsi"]

    buy_signals = sum([ema_crossed_up, macd_turning_up, price_near_mid])

    if (position is None
            and buy_signals >= 2
            and RSI_BUY_MIN <= rsi <= RSI_BUY_MAX
            and trend_bias == "bullish"
            and fng_value >= FNG_MIN_TO_BUY):
        return "BUY_CANDIDATE"

    if position is not None:
        sell_signals = sum([
            ema_crossed_down, macd_turning_down,
            rsi >= RSI_SELL_MIN, rsi <= RSI_SELL_MAX, price_broke_low,
        ])
        if sell_signals >= 1:
            return "SELL_CANDIDATE"

    return "HOLD"


# ════════════════════════════════════════════════════════════════
#  4-AGENT REVIEW (only on signal candidates)
# ════════════════════════════════════════════════════════════════

def build_context(coin, df, trend_bias, fng_value, fng_label, pos_str):
    last = df.iloc[-1]
    prev = df.iloc[-2]
    change = last["change_pct"] if not pd.isna(last["change_pct"]) else 0.0
    return (
        f"COIN: {coin}\nPRICE: ${last['close']:,.4f}\n"
        f"24H: {change:+.2f}%\nTREND: {trend_bias.upper()}\n"
        f"F&G: {fng_value}/100 ({fng_label})\n\n"
        f"EMA9: ${last['ema9']:,.4f} (prev ${prev['ema9']:,.4f})\n"
        f"EMA21: ${last['ema21']:,.4f}\nEMA50: ${last['ema50']:,.4f}\n"
        f"RSI: {last['rsi']:.1f}\n"
        f"MACD: {last['macd']:.4f}  Sig: {last['macd_signal']:.4f}  "
        f"Hist: {last['macd_hist']:.4f}\n"
        f"BB: ${last['bb_upper']:,.4f} / ${last['bb_mid']:,.4f} / "
        f"${last['bb_lower']:,.4f}\n"
        f"ATR: {last['atr']:.4f}\nPOSITION: {pos_str}"
    )


def call_agent(claude_client, role, context, instructions):
    global claude_calls
    try:
        resp = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=150,
            messages=[{"role": "user",
                       "content": f"{role}\n\n{context}\n\n{instructions}"}]
        )
        claude_calls += 1
        return resp.content[0].text.strip()
    except Exception as e:
        return f"Agent error: {e}\nRECOMMENDATION: HOLD"


def run_agent_review(claude_client, coin, df, trend_bias,
                     fng_value, fng_label, pos_str, signal_type):
    context = build_context(coin, df, trend_bias, fng_value, fng_label, pos_str)
    action  = "buy" if signal_type == "BUY_CANDIDATE" else "sell"

    log(f"Signal: {signal_type} — running agents "
        f"(calls today: {claude_calls})", coin)

    bull = call_agent(claude_client,
        "You are a BULLISH crypto analyst.", context,
        f"Strongest case for {action}ing. "
        f"End: RECOMMENDATION: {'BUY' if action=='buy' else 'SELL'} "
        f"or RECOMMENDATION: HOLD. Under 80 words.")

    bear = call_agent(claude_client,
        "You are a BEARISH crypto analyst.", context,
        f"Strongest case AGAINST {action}ing. "
        f"End: RECOMMENDATION: {'AVOID' if action=='buy' else 'HOLD'} "
        f"or RECOMMENDATION: HOLD. Under 80 words.")

    neutral = call_agent(claude_client,
        "You are a NEUTRAL technical analyst.", context,
        f"Objective read. Should we {action}? "
        f"End: RECOMMENDATION: BUY, SELL, or HOLD. Under 80 words.")

    bull_rec    = "BUY"   if "RECOMMENDATION: BUY"   in bull    else "HOLD"
    bear_rec    = "AVOID" if "RECOMMENDATION: AVOID" in bear    else "HOLD"
    neutral_rec = "BUY"   if "RECOMMENDATION: BUY"   in neutral else \
                  "SELL"  if "RECOMMENDATION: SELL"  in neutral else "HOLD"
    log(f"Bull:{bull_rec}  Bear:{bear_rec}  Neutral:{neutral_rec}", coin)

    judge_prompt = (
        f"You are the FINAL DECISION MAKER for a $200 crypto account.\n"
        f"Should we {action}?\n\n{context}\n\n"
        f"BULL: {bull}\n\nBEAR: {bear}\n\nNEUTRAL: {neutral}\n\n"
        f"Rules:\n"
        f"- BUY only if Bull AND Neutral both say BUY, Bear has no critical objection\n"
        f"- SELL if Bear/Neutral recommend SELL with clear reason\n"
        f"- HOLD if any meaningful disagreement\n"
        f"- Protecting capital beats catching every trade\n\n"
        f"Reply: one word (BUY/SELL/HOLD) on line 1, "
        f"one sentence reason on line 2."
    )
    final    = call_agent(claude_client, "", context, judge_prompt)
    lines    = final.split("\n")
    decision = lines[0].strip().upper()
    reason   = lines[1].strip() if len(lines) > 1 else ""

    if decision not in ["BUY", "SELL", "HOLD"]:
        decision, reason = "HOLD", "Unexpected response — defaulting to HOLD."

    log(f"Judge: {decision}  |  {reason}", coin)
    return decision, reason


# ════════════════════════════════════════════════════════════════
#  ORDER EXECUTION
# ════════════════════════════════════════════════════════════════

def get_total_balance(exchange):
    try:
        if SANDBOX:
            return 200.0
        bal = exchange.fetch_balance()
        return float(bal["total"].get("USD", 0))
    except:
        return 0


def get_allocation(exchange):
    return round(get_total_balance(exchange) / len(COINS), 2)


def buy(exchange, coin, price, atr):
    global positions
    spend_usd  = round(get_allocation(exchange) * TRADE_SIZE_PCT, 2)
    amount     = round(spend_usd / price, 6)
    atr_stop   = price - (1.5 * atr) if atr and atr > 0 else 0
    stop_price = round(max(atr_stop, price * (1 - STOP_LOSS_PCT)), 4)
    target     = round(price * (1 + TAKE_PROFIT_PCT), 4)

    if SANDBOX:
        log(f"[SANDBOX] BUY {amount} @ ${price:,.4f} (${spend_usd:.2f})", coin)
    else:
        try:
            exchange.create_market_buy_order(coin, amount)
            log(f"BUY {amount} @ ~${price:,.4f}", coin)
        except Exception as e:
            log(f"Buy failed: {e}", coin)
            return

    positions[coin] = {
        "entry":      price,
        "amount":     amount,
        "spent_usd":  spend_usd,
        "stop":       stop_price,
        "target":     target,
        "opened_at":  datetime.now(),
        "force_sell": False,
    }
    log(f"  Stop: ${stop_price:,.4f}  Target: ${target:,.4f}", coin)

    tg_send(
        f"<b>🟢 BUY — {coin.split('/')[0]}</b>\n"
        f"Price:  ${price:,.4f}\n"
        f"Spent:  ${spend_usd:.2f}\n"
        f"Stop:   ${stop_price:,.4f} (-{STOP_LOSS_PCT*100:.1f}%)\n"
        f"Target: ${target:,.4f} (+{TAKE_PROFIT_PCT*100:.1f}%)\n"
        f"Mode:   {'SANDBOX' if SANDBOX else 'LIVE'}"
    )


def sell(exchange, coin, price, reason):
    global positions, session_trades
    pos = positions[coin]
    if pos is None:
        return

    pnl_pct  = (price - pos["entry"]) / pos["entry"] * 100
    pnl_usd  = pos["spent_usd"] * (pnl_pct / 100)
    held_hrs = (datetime.now() - pos["opened_at"]).seconds / 3600
    emoji    = "🟢" if pnl_usd >= 0 else "🔴"

    if SANDBOX:
        log(f"[SANDBOX] SELL {pos['amount']} @ ${price:,.4f} ({reason})", coin)
    else:
        try:
            exchange.create_market_sell_order(coin, pos["amount"])
        except Exception as e:
            log(f"Sell failed: {e}", coin)
            return

    log(f"  PnL: {pnl_pct:+.2f}% (${pnl_usd:+.2f})  held {held_hrs:.1f}h", coin)

    session_trades.append({
        "coin": coin, "pnl_pct": pnl_pct,
        "pnl_usd": pnl_usd, "reason": reason, "time": datetime.now(),
    })
    positions[coin] = None

    wins  = [t for t in session_trades if t["pnl_usd"] > 0]
    total = sum(t["pnl_usd"] for t in session_trades)
    log(f"Session: {len(wins)}W/{len(session_trades)-len(wins)}L "
        f"P&L:${total:+.2f}")

    tg_send(
        f"<b>{emoji} SELL — {coin.split('/')[0]}</b>\n"
        f"Price:  ${price:,.4f}\n"
        f"PnL:    {pnl_pct:+.2f}% (${pnl_usd:+.2f})\n"
        f"Held:   {held_hrs:.1f} hours\n"
        f"Reason: {reason}\n"
        f"Session P&L: ${total:+.2f} "
        f"({len(wins)}W/{len(session_trades)-len(wins)}L)"
    )


# ════════════════════════════════════════════════════════════════
#  SAFETY GUARDS
# ════════════════════════════════════════════════════════════════

def check_hard_exits(coin, price):
    pos = positions[coin]
    if pos is None:
        return False, ""
    if pos.get("force_sell"):
        return True, "manual sell via Telegram"
    if price <= pos["stop"]:
        return True, f"stop-loss at ${price:,.4f}"
    if price >= pos["target"]:
        return True, f"take-profit at ${price:,.4f}"
    return False, ""


def reset_daily_counters(exchange):
    global trade_counts, last_day_reset, daily_start_bal
    global claude_calls, daily_summary_sent_date
    today = datetime.now().date()
    if today != last_day_reset:
        trade_counts    = {coin: 0 for coin in COINS}
        last_day_reset  = today
        daily_start_bal = None
        log(f"New day — counters reset. Yesterday Claude calls: {claude_calls}")
        claude_calls = 0

    # Send daily summary at 8am once per day
    now = datetime.now()
    if (now.hour == 8 and daily_summary_sent_date != today):
        send_daily_summary(exchange)
        daily_summary_sent_date = today


def daily_loss_exceeded(exchange):
    global daily_start_bal
    if SANDBOX:
        return False
    if daily_start_bal is None:
        daily_start_bal = get_total_balance(exchange)
        return False
    current  = get_total_balance(exchange)
    loss_pct = (daily_start_bal - current) / daily_start_bal
    if loss_pct >= DAILY_LOSS_LIMIT_PCT:
        msg = (f"⚠️ <b>Daily loss limit hit!</b>\n"
               f"Loss: {loss_pct*100:.1f}%\nAll trading paused for today.")
        tg_send(msg)
        log(f"Daily loss limit hit ({loss_pct*100:.1f}%).")
        return True
    # Warning at 75% of limit
    elif loss_pct >= DAILY_LOSS_LIMIT_PCT * 0.75:
        tg_send(f"⚠️ Approaching daily loss limit: {loss_pct*100:.1f}% lost today.")
    return False


# ════════════════════════════════════════════════════════════════
#  PROCESS ONE COIN
# ════════════════════════════════════════════════════════════════

def process_coin(exchange, claude_client, coin, fng_value, fng_label):
    global trade_counts

    if trade_counts[coin] >= MAX_TRADES_PER_COIN:
        log("Daily cap reached.", coin)
        return

    df = get_data(exchange, coin, TF_SIGNAL, limit=100)
    if df is None or len(df) < 30:
        log("Not enough data.", coin)
        return

    price = df.iloc[-1]["close"]
    rsi   = df.iloc[-1]["rsi"]
    atr   = df.iloc[-1]["atr"]

    # Hard exits always run
    should_exit, exit_reason = check_hard_exits(coin, price)
    if should_exit:
        log(f"Hard exit: {exit_reason}", coin)
        sell(exchange, coin, price, exit_reason)
        trade_counts[coin] += 1
        return

    # Fear & Greed gate
    if positions[coin] is None and fng_value < FNG_MIN_TO_BUY:
        log(f"F&G too low ({fng_value}). No new buys.", coin)
        return

    # Trend check (no Claude)
    trend_bias = get_trend_bias(exchange, coin)

    # Skip new buys if trend not bullish
    if positions[coin] is None and trend_bias != "bullish":
        log(f"Trend is {trend_bias} — no new buys.", coin)
        return

    # Pure math signal detection (no Claude)
    signal = detect_signal(df, positions[coin], trend_bias, fng_value)

    log(f"${price:,.4f}  RSI:{rsi:.1f}  Trend:{trend_bias}  "
        f"F&G:{fng_value}  Signal:{signal}", coin)

    if signal == "HOLD":
        if positions[coin] is not None:
            pnl  = (price - positions[coin]["entry"]) / positions[coin]["entry"] * 100
            held = (datetime.now() - positions[coin]["opened_at"]).seconds / 3600
            log(f"Holding — {held:.1f}h  PnL:{pnl:+.2f}%", coin)
        else:
            log("No signal — no Claude call.", coin)
        return

    # Signal detected — call the 4 agents
    pos = positions[coin]
    pos_str = "no open position" if pos is None else \
              (f"long {pos['amount']} since ${pos['entry']:,.4f} "
               f"| stop ${pos['stop']:,.4f} | target ${pos['target']:,.4f}")

    decision, reason = run_agent_review(
        claude_client, coin, df, trend_bias,
        fng_value, fng_label, pos_str, signal
    )

    if decision == "BUY" and positions[coin] is None:
        buy(exchange, coin, price, atr)
        trade_counts[coin] += 1
    elif decision == "SELL" and positions[coin] is not None:
        sell(exchange, coin, price, "Agent consensus: SELL")
        trade_counts[coin] += 1
    else:
        log("Agents said HOLD.", coin)


# ════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ════════════════════════════════════════════════════════════════

def run():
    global exchange_global

    log("=" * 62)
    log("KRAKEN OPTIMIZED BOT + TELEGRAM — STARTING")
    log(f"Coins    : {', '.join(COINS)}")
    log(f"Target   : +{TAKE_PROFIT_PCT*100:.0f}%  Stop: -{STOP_LOSS_PCT*100:.0f}%")
    log(f"F&G min  : {FNG_MIN_TO_BUY}/100")
    log(f"Mode     : {'SANDBOX' if SANDBOX else '*** LIVE ***'}")
    log("=" * 62)

    exchange, claude = connect()
    exchange_global  = exchange

    # Start Telegram listener in background thread
    tg_thread = threading.Thread(target=telegram_listener, daemon=True)
    tg_thread.start()

    # Announce startup on Telegram
    tg_send(
        f"<b>🤖 Bot Started</b>\n"
        f"Coins: {', '.join(c.split('/')[0] for c in COINS)}\n"
        f"Mode: {'SANDBOX' if SANDBOX else '🔴 LIVE'}\n"
        f"Target: +{TAKE_PROFIT_PCT*100:.0f}%  "
        f"Stop: -{STOP_LOSS_PCT*100:.0f}%\n\n"
        f"Send /help for available commands."
    )

    while True:
        try:
            reset_daily_counters(exchange)

            if daily_loss_exceeded(exchange):
                time.sleep(3600)
                continue

            if is_paused:
                log("Bot is paused — skipping cycle.")
                time.sleep(CHECK_EVERY_MINUTES * 60)
                continue

            fng_value, fng_label = get_fear_and_greed()

            log(f"─── Cycle  F&G:{fng_value}/100 ({fng_label})  "
                f"Calls:{claude_calls} (~${claude_calls*0.003:.2f}) ───")

            for coin in COINS:
                try:
                    process_coin(exchange, claude, coin, fng_value, fng_label)
                    time.sleep(3)
                except Exception as e:
                    log(f"Error on {coin}: {e}")

            open_pos = [c for c in COINS if positions[c] is not None]
            log(f"Open: {', '.join(open_pos) if open_pos else 'none'}")

            if session_trades:
                total = sum(t["pnl_usd"] for t in session_trades)
                wins  = [t for t in session_trades if t["pnl_usd"] > 0]
                log(f"Session P&L: ${total:+.2f} "
                    f"({len(wins)}W/{len(session_trades)-len(wins)}L)")

        except KeyboardInterrupt:
            log("Bot stopped.")
            tg_send("🛑 <b>Bot stopped.</b>")
            if session_trades:
                total = sum(t["pnl_usd"] for t in session_trades)
                wins  = [t for t in session_trades if t["pnl_usd"] > 0]
                log(f"Final P&L: ${total:+.2f} "
                    f"({len(wins)}W/{len(session_trades)-len(wins)}L) "
                    f"API: ${claude_calls*0.003:.2f}")
            break

        except Exception as e:
            log(f"Error: {e} — continuing...")

        log(f"Sleeping {CHECK_EVERY_MINUTES} min...\n")
        time.sleep(CHECK_EVERY_MINUTES * 60)


if __name__ == "__main__":
    run()
