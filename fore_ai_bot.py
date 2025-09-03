from __future__ import annotations
import os
import sys
import time
import logging
import threading
from dataclasses import dataclass
from typing import Optional, List, Tuple
import re
import json

import MetaTrader5 as mt5
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, TypeHandler, filters, ContextTypes

# Ensure local folder on sys.path to support folder with spaces
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from signal_parser import parse_signal, ParsedSignal


# Ensure UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Always load .env from this module's directory
load_dotenv(os.path.join(_THIS_DIR, ".env"))


# --- Config ---
# Branding prefix for order comments
BRAND_PREFIX = "ForeAi"
SYMBOL_DEFAULT = os.getenv("SYMBOL", "XAUUSD")
SYMBOL_SUFFIX = os.getenv("SYMBOL_SUFFIX", "")  # e.g., 'm' to become XAUUSDm
LOT_FALLBACK = float(os.getenv("LOT_FALLBACK", 0.10))
SLIPPAGE = int(os.getenv("SLIPPAGE", 30))
# Support new brand var, fallback to legacy
MAGIC = int(os.getenv("FORE_AI_MAGIC", os.getenv("MAZHAR_MAGIC", 777001)))

TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN", "") or "").strip()
TELEGRAM_CHANNEL_ID = (os.getenv("TELEGRAM_CHANNEL_ID", "") or "").strip()

_LOGIN_RAW = os.getenv("MT5_LOGIN")
LOGIN = int(_LOGIN_RAW) if _LOGIN_RAW and _LOGIN_RAW.strip() else None
PASSWORD = os.getenv("MT5_PASSWORD") or None
SERVER = os.getenv("MT5_SERVER") or None


# --- Logging ---
log = logging.getLogger("fore_ai_bot")
log.setLevel(logging.INFO)
fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(fmt)
log.addHandler(_console)
log.propagate = False


# --- State ---
last_signal: Optional[ParsedSignal] = None
signal_lock = threading.Lock()
STOP_EVENT = threading.Event()

telegram_connected = False
telegram_channel_name: Optional[str] = None

# Fixed lot configured from dashboard when signal lacks its own lot size
USER_FIXED_LOT: Optional[float] = None

# Auto place on signal flag (controlled by dashboard)
AUTO_PLACE: bool = False

# Store last known numeric TP ladders per (symbol, direction)
# Key: (symbol_str, 'BUY'|'SELL') -> List[float]
TP_LADDERS: dict[tuple[str, str], List[float]] = {}

# Persistence for TP ladders
_TP_FILE = os.path.join(_THIS_DIR, "tp_ladders.json")

def _save_tp_ladders():
    try:
        data = {f"{k[0]}|{k[1]}": v for k, v in TP_LADDERS.items() if v}
        with open(_TP_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def _load_tp_ladders():
    try:
        if os.path.exists(_TP_FILE):
            with open(_TP_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in (data or {}).items():
                try:
                    sym, direction = k.split("|", 1)
                    vals = [float(x) for x in v]
                    if vals:
                        TP_LADDERS[(sym, direction)] = vals
                except Exception:
                    continue
    except Exception:
        pass

# Telegram lifecycle control
STOP_TG = threading.Event()


def mt5_login():
    log.info("MT5: Initializing...")
    ok = False
    try:
        if LOGIN is not None and PASSWORD and SERVER:
            ok = mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER, timeout=10000)
        else:
            ok = mt5.initialize(timeout=10000)
    except TypeError:
        if LOGIN is not None and PASSWORD and SERVER:
            ok = mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER)
        else:
            ok = mt5.initialize()
    if not ok:
        log.error(f"MT5: initialize() failed {mt5.last_error()}")
        sys.exit(1)
    acc = mt5.account_info()
    if acc is None:
        log.error(f"MT5: account_info failed {mt5.last_error()}")
        sys.exit(1)
    log.info(f"MT5: Logged in {acc.login} @ {acc.server}; Balance {acc.balance:.2f} {acc.currency}")


def mt5_login_safe() -> bool:
    """Like mt5_login but returns False on failure instead of exiting the app."""
    log.info("MT5: Connecting (safe mode)...")
    try:
        ok = False
        try:
            if LOGIN is not None and PASSWORD and SERVER:
                ok = mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER, timeout=10000)
            else:
                ok = mt5.initialize(timeout=10000)
        except TypeError:
            if LOGIN is not None and PASSWORD and SERVER:
                ok = mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER)
            else:
                ok = mt5.initialize()
        if not ok:
            log.error(f"MT5: initialize() failed {mt5.last_error()}")
            mt5.shutdown()
            return False
        acc = mt5.account_info()
        if acc is None:
            log.error(f"MT5: account_info failed {mt5.last_error()}")
            mt5.shutdown()
            return False
        log.info(f"MT5: Logged in {acc.login} @ {acc.server}; Balance {acc.balance:.2f} {acc.currency}")
        # Ensure default/found symbol is selected lazily during order placement
        return True
    except Exception as e:
        log.error(f"MT5: Safe connect error: {e}")
        return False


def resolve_symbol(sig_symbol: str) -> str:
    # Try signal symbol + suffix, then raw signal symbol, then default
    cand = [sig_symbol + SYMBOL_SUFFIX if SYMBOL_SUFFIX else sig_symbol, sig_symbol, SYMBOL_DEFAULT]
    for s in cand:
        info = mt5.symbol_info(s)
        if info is not None:
            if not info.visible:
                mt5.symbol_select(s, True)
            if s != sig_symbol:
                log.info(f"Symbol resolved: requested {sig_symbol}, using {s}")
            return s
    return SYMBOL_DEFAULT


def resolve_symbol_strict(sig_symbol: str) -> Optional[str]:
    """Resolve a user-provided symbol without falling back to default.
    Tries with configured suffix, then raw. Returns None if not available.
    """
    try:
        base = (sig_symbol or "").strip().upper()
        if not base:
            return None
        cands = [f"{base}{SYMBOL_SUFFIX}" if SYMBOL_SUFFIX else base, base]
        for s in cands:
            info = mt5.symbol_info(s)
            if info is not None:
                if not info.visible:
                    mt5.symbol_select(s, True)
                return s
    except Exception:
        pass
    return None


def round_price(symbol: str, price: float) -> float:
    info = mt5.symbol_info(symbol)
    digits = getattr(info, 'digits', 2) if info else 2
    return round(price, digits)


def place_pending_limit(symbol: str, side: str, entry: float, sl: float, tp: Optional[float], volume: float) -> bool:
    info = mt5.symbol_info(symbol)
    if not info:
        log.error(f"MT5: symbol {symbol} not available")
        return False
    entry = round_price(symbol, entry)
    sl = round_price(symbol, sl)
    if tp is not None:
        tp = round_price(symbol, tp)

    order_type = mt5.ORDER_TYPE_BUY_LIMIT if side == 'BUY' else mt5.ORDER_TYPE_SELL_LIMIT
    req = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": symbol,
        "volume": float(volume),
        "type": order_type,
        "price": entry,
        "sl": sl,
        "tp": tp if tp is not None else 0.0,
        "deviation": SLIPPAGE,
        "magic": MAGIC,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_RETURN,
        "comment": f"{BRAND_PREFIX}-{side}",
    }
    res = mt5.order_send(req)
    if not res or res.retcode != mt5.TRADE_RETCODE_DONE:
        log.error(f"MT5: Failed pending {side} {symbol} @ {entry} vol {volume} (ret={getattr(res,'retcode',None)})")
        return False
    log.info(f"MT5: Pending {side} placed {symbol} @ {entry} TP {tp if tp else '-'} SL {sl} vol {volume}")
    return True


def cancel_all_pending(symbol: Optional[str] = None, include_all_magics: bool = False) -> int:
    """Cancel all pending orders.
    - When include_all_magics is False (default), only cancels orders placed by this bot's MAGIC.
    - Returns the number of orders successfully cancelled.
    """
    try:
        orders = mt5.orders_get(symbol=symbol) or []
        if not orders:
            # Fallback: some terminals require a group wildcard to enumerate
            try:
                orders = mt5.orders_get(group="*") or []
            except Exception:
                pass
        log.info(f"MT5: Found {len(orders)} pending order(s) total.")
        # Broader matching: some brokers may not preserve magic on pending orders; also match by our comment prefix
        if include_all_magics:
            to_cancel = list(orders)
        else:
            to_cancel = []
            for o in orders:
                try:
                    mg = getattr(o, 'magic', None)
                except Exception:
                    mg = None
                try:
                    cm = str(getattr(o, 'comment', '') or '')
                except Exception:
                    cm = ''
                if mg == MAGIC or cm.startswith(f'{BRAND_PREFIX}-') or cm.startswith('MazharBot-'):
                    to_cancel.append(o)
        total = len(to_cancel)
        if total == 0:
            log.info("MT5: No pending orders to cancel.")
            return 0
        log.info(f"MT5: Cancelling {total} pending order(s){' (all magics)' if include_all_magics else ''}...")
        ok = 0
        for o in to_cancel:
            # For removing pending orders, only 'action' and 'order' are required
            req = {"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket}
            res = mt5.order_send(req)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                ok += 1
            else:
                log.error(f"MT5: Failed to remove pending {o.ticket} (ret={getattr(res,'retcode',None)})")
        log.info(f"MT5: Cancelled {ok}/{total} pending order(s).")
        return ok
    except Exception as e:
        log.error(f"MT5: Cancel pending failed: {e}")
        return 0


def close_all_positions(symbol: Optional[str] = None, include_all_magics: bool = False, only_profit: Optional[bool] = None) -> int:
    """Close open positions.
    - include_all_magics: when True, closes positions regardless of magic.
    - only_profit: True -> close profitable only; False -> close losing only; None -> close all.
    - Returns the number of positions successfully closed.
    """
    try:
        positions = mt5.positions_get(symbol=symbol) or []
        to_close = []
        for p in positions:
            mg = getattr(p, 'magic', None)
            if not include_all_magics and mg != MAGIC:
                continue
            pr = getattr(p, 'profit', 0.0)
            if only_profit is True and not (pr > 0):
                continue
            if only_profit is False and not (pr < 0):
                continue
            to_close.append(p)
        total = len(to_close)
        if total == 0:
            log.info("MT5: No open positions to close with current filters.")
            return 0
        log.warning(f"MT5: Closing {total} position(s){' (all magics)' if include_all_magics else ''}{' (profit only)' if only_profit is True else (' (loss only)' if only_profit is False else '')}...")
        ok = 0
        for p in to_close:
            tick = mt5.symbol_info_tick(p.symbol)
            if not tick:
                continue
            price = tick.bid if p.type == mt5.ORDER_TYPE_BUY else tick.ask
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": p.symbol,
                "volume": p.volume,
                "type": mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                "position": p.ticket,
                "price": price,
                "deviation": SLIPPAGE,
                "magic": MAGIC,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_FOK,
                "comment": f"{BRAND_PREFIX}-Close",
            }
            res = mt5.order_send(req)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                ok += 1
            else:
                log.error(f"MT5: Failed to close position {getattr(p,'ticket',None)} (ret={getattr(res,'retcode',None)})")
        log.info(f"MT5: Closed {ok}/{total} position(s).")
        return ok
    except Exception as e:
        log.error(f"MT5: Close positions failed: {e}")
        return 0


def place_orders_from_signal(sig: ParsedSignal) -> bool:
    """Place pending LIMIT orders laddered across the zone.
    Example zone 3463/3459 BUY -> orders at 3463, 3462, 3461, 3460, 3459 (equal lots).
    For SELL -> 3459, 3460, 3461, 3462, 3463.
    TP assignment: uses first numeric TP if provided (can be enhanced later).
    """
    try:
        symbol = resolve_symbol(sig.symbol)
        # Build ladder prices across the zone with step of 1.0 price unit
        hi = sig.zone_high
        lo = sig.zone_low
        step = 1.0
        prices: List[float] = []
        if sig.direction == 'BUY':
            p = hi
            while p >= lo - 1e-9:
                prices.append(round_price(symbol, p))
                p -= step
        else:  # SELL
            p = lo
            while p <= hi + 1e-9:
                prices.append(round_price(symbol, p))
                p += step
        log.info(f"Placing {len(prices)} pending {sig.direction} LIMIT orders for {symbol} across zone {lo}..{hi} (step {step}).")

        # Lot selection per order (do not split): use signal lot if present; else fixed; else fallback
        # Per-order lot selection (do NOT split). Use signal lot for each order when present.
        if sig.lot_size and sig.lot_size > 0:
            vol_each_raw = sig.lot_size
        elif USER_FIXED_LOT and USER_FIXED_LOT > 0:
            vol_each_raw = USER_FIXED_LOT
        else:
            vol_each_raw = LOT_FALLBACK
        # Adjust to symbol volume step
        vol_each = adjust_lot_for_symbol(symbol, vol_each_raw)
        if vol_each <= 0:
            vol_each = vol_each_raw  # fallback

        # Build numeric TP ladder for mapping across orders and trailing
        numeric_tps: List[float] = [tp for tp in sig.take_profits if tp is not None]
        try:
            if numeric_tps:
                TP_LADDERS[(symbol, sig.direction)] = sorted(set(numeric_tps))
                _save_tp_ladders()
        except Exception:
            pass
        # Distribute TPs across ladder: first order -> TP1, second -> TP2, ...; if more orders than TPs, leave TP empty

        ok_any = False
        placed = 0
        for idx, price in enumerate(prices):
            tp_val = numeric_tps[idx] if idx < len(numeric_tps) else None
            ok = place_pending_limit(symbol, sig.direction, price, sig.stop_loss, tp_val, vol_each)
            if ok:
                placed += 1
                ok_any = True
        log.info(f"Order placement finished: requested {len(prices)}, placed {placed}.")
        return ok_any
    except Exception as e:
        log.error(f"Place orders failed: {e}")
        return False


def adjust_lot_for_symbol(symbol: str, lot: float) -> float:
    info = mt5.symbol_info(symbol)
    if not info:
        return round(lot, 2)
    step = getattr(info, 'volume_step', 0.01) or 0.01
    min_vol = getattr(info, 'volume_min', 0.01) or 0.01
    max_vol = getattr(info, 'volume_max', 100.0) or 100.0
    # round down to step
    steps = int(lot / step)
    adj = steps * step
    if adj < min_vol:
        adj = min_vol
    if adj > max_vol:
        adj = max_vol
    # Limit to 2 decimals typical
    return float(f"{adj:.2f}")


def set_fixed_lot(value: Optional[float]):
    """Set a user fixed lot used when signal lot is missing or zero."""
    global USER_FIXED_LOT
    try:
        if value is None:
            USER_FIXED_LOT = None
        else:
            v = float(value)
            USER_FIXED_LOT = v if v > 0 else None
        log.info(f"Fixed lot updated: {USER_FIXED_LOT}")
    except Exception:
        pass


def set_auto_place(flag: bool):
    global AUTO_PLACE
    AUTO_PLACE = bool(flag)
    log.info(f"Auto-place on signal set to: {AUTO_PLACE}")


def maintain_trailing_stops():
    """Adjust SL per trailing rules using TP ladder: TP1 -> SL=BE, TP2 -> SL=TP1, TP3 -> SL=TP2, ..."""
    try:
        positions = mt5.positions_get() or []
        if not positions:
            return
        my_positions = [p for p in positions if getattr(p, 'magic', None) == MAGIC]
        if not my_positions:
            return
        tick_cache: dict[str, object] = {}
        info_cache: dict[str, object] = {}
        for p in my_positions:
            if p.symbol not in tick_cache:
                tick_cache[p.symbol] = mt5.symbol_info_tick(p.symbol)
            if p.symbol not in info_cache:
                info_cache[p.symbol] = mt5.symbol_info(p.symbol)
        for p in my_positions:
            sym = p.symbol
            tick = tick_cache.get(sym)
            if not tick:
                continue
            if p.type == mt5.POSITION_TYPE_BUY:
                current = getattr(tick, 'bid', None)
                direction = 'BUY'
            else:
                current = getattr(tick, 'ask', None)
                direction = 'SELL'
            if current is None:
                continue
            ladder = TP_LADDERS.get((sym, direction))
            if not ladder:
                continue
            ladder_sorted = sorted(set(ladder))
            if direction == 'BUY':
                achieved = sum(1 for tp in ladder_sorted if current >= tp)
            else:
                achieved = sum(1 for tp in ladder_sorted if current <= tp)
            if achieved <= 0:
                continue
            if achieved == 1:
                new_sl = p.price_open
            else:
                new_sl = ladder_sorted[achieved - 2]
            new_sl = round_price(sym, float(new_sl))
            cur_sl = float(getattr(p, 'sl', 0.0) or 0.0)
            eps = 1e-7
            info = info_cache.get(sym)
            point = getattr(info, 'point', 0.01) if info else 0.01
            if direction == 'BUY':
                if new_sl <= cur_sl + eps:
                    continue
                if new_sl >= current:
                    new_sl = round_price(sym, current - (2 * point))
            else:
                if cur_sl > 0 and new_sl >= cur_sl - eps:
                    continue
                if new_sl <= current:
                    new_sl = round_price(sym, current + (2 * point))
            req = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": p.ticket,
                "symbol": sym,
                "sl": new_sl,
                "tp": getattr(p, 'tp', 0.0) or 0.0,
                "magic": MAGIC,
            }
            res = mt5.order_send(req)
            if not res or res.retcode != mt5.TRADE_RETCODE_DONE:
                log.warning(f"Trailing: failed SL modify pos {p.ticket} -> {new_sl} (ret={getattr(res,'retcode',None)})")
            else:
                log.info(f"Trailing: updated SL pos {p.ticket} -> {new_sl}")
    except Exception as e:
        log.error(f"Trailing error: {e}")


# --- Flexible order parsers ---
def _parse_flexible_format1(text: str):
    """Parse format:
    Lot size=0.10
    Lots=10
    Buy limit= 3410
    TP 3420
    SL 3400
    Returns dict or None.
    """
    try:
        if not isinstance(text, str):
            return None
        t = text.strip()
        m_lots = re.search(r"\blots?\s*[:=]\s*(\d+)", t, re.I)
        m_lsize = re.search(r"\blot\s*size\s*[:=]\s*([0-9]*\.?[0-9]+)", t, re.I)
        m_dir_entry = re.search(r"\b(buy|sell)\s*limit\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)", t, re.I)
        m_sl = re.search(r"\bSL\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)", t, re.I)
        if not (m_lots and m_lsize and m_dir_entry and m_sl):
            return None
        lots = int(m_lots.group(1))
        lot_size = float(m_lsize.group(1))
        direction = 'BUY' if m_dir_entry.group(1).strip().lower() == 'buy' else 'SELL'
        entry = float(m_dir_entry.group(2))
        sl = float(m_sl.group(1))
        # TP can be 'open' or price; optional
        tp = None
        m_tp_open = re.search(r"\bTP\s*[:=]?\s*open\b", t, re.I)
        if not m_tp_open:
            m_tp = re.search(r"\bTP\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)", t, re.I)
            if m_tp:
                tp = float(m_tp.group(1))
        return {"direction": direction, "entry": entry, "sl": sl, "tp": tp, "lots": lots, "lot_size": lot_size}
    except Exception:
        return None


def _parse_flexible_format2(text: str):
    """Parse format:
    Lot size=0.20
    Buy limit
    3410 ->Tp 3412
    3408 ->Tp 3414
    ...
    SL 3350
    Returns dict or None.
    """
    try:
        if not isinstance(text, str):
            return None
        t = text.strip()
        m_lsize = re.search(r"\blot\s*size\s*[:=]\s*([0-9]*\.?[0-9]+)", t, re.I)
        m_dir = re.search(r"\b(buy|sell)\s*limit\b", t, re.I)
        m_sl = re.search(r"\bSL\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)", t, re.I)
        if not (m_lsize and m_dir and m_sl):
            return None
        direction = 'BUY' if m_dir.group(1).strip().lower() == 'buy' else 'SELL'
        lot_size = float(m_lsize.group(1))
        sl = float(m_sl.group(1))
        # Find entry -> TP pairs; 'open' allowed
        pairs = re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*[-=]*>\s*(?:tp\s*)?((?:open)|(?:[0-9]+(?:\.[0-9]+)?))", t, re.I)
        if not pairs:
            return None
        entries: List[Tuple[float, Optional[float]]] = []
        for entry_s, tp_s in pairs:
            entry = float(entry_s)
            tp_val = None if tp_s.strip().lower() == 'open' else float(tp_s)
            entries.append((entry, tp_val))
        return {"direction": direction, "lot_size": lot_size, "sl": sl, "entries": entries}
    except Exception:
        return None


# --- Telegram integration ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global last_signal, telegram_connected, telegram_channel_name
    msg = update.effective_message
    if not msg or not msg.text:
        return

    # Accept only from configured chat if provided
    if TELEGRAM_CHANNEL_ID and str(msg.chat_id) != str(TELEGRAM_CHANNEL_ID):
        log.info(f"Ignored message from non-target chat (got {msg.chat_id}, want {TELEGRAM_CHANNEL_ID}).")
        return

    telegram_connected = True
    try:
        telegram_channel_name = msg.chat.title or str(msg.chat.id)
    except Exception:
        pass
    text = (msg.text or '').strip()
    # Inline /getid support without CommandHandler for broader compatibility
    if text.startswith('/getid'):
        try:
            await msg.reply_text(
                f"Chat ID: {msg.chat.id}\nTitle: {telegram_channel_name or '-'}\n\nSet this ID in Configuration → Telegram Chat ID.")
        except Exception:
            pass
        return
    # Inline Telegram control commands
    lt = text.strip()
    parts = [p for p in lt.split() if p]
    if parts and parts[0].startswith('/'):
        cmd = parts[0].lower()
        args = [a for a in parts[1:]]

        # Helpers for symbol argument
        def _arg_symbol_or_none() -> Optional[str]:
            if not args:
                return None
            a0 = args[0].strip().upper()
            if a0 in ("ALL", "PENDING", "ORDERS", "POSITIONS", "PROFIT", "LOSS"):
                return None
            return a0

        # /cancel [all|SYMBOL]
        if cmd == '/cancel' or cmd == '/cancelall' or (cmd == '/cancel' and args):
            try:
                mt5_login_safe()
                sym_token = _arg_symbol_or_none()
                if sym_token:
                    rs = resolve_symbol_strict(sym_token)
                    if not rs:
                        await msg.reply_text(f"Symbol '{sym_token}' not found.")
                        return True
                    n = cancel_all_pending(symbol=rs, include_all_magics=True)
                    await msg.reply_text(f"Cancelled {n} pending order(s) for {rs}.")
                else:
                    # treat no args or 'all' as all symbols
                    n = cancel_all_pending(include_all_magics=True)
                    await msg.reply_text(f"Cancelled {n} pending order(s) (all symbols).")
            except Exception as e:
                await msg.reply_text(f"Cancel failed: {e}")
            return

        # /delete [pending] [SYMBOL|all]
        if cmd == '/delete':
            try:
                mt5_login_safe()
                target = None
                sym_token = None
                if args:
                    if args[0].lower() in ('pending', 'orders'):
                        target = 'pending'
                        if len(args) > 1:
                            sym_token = args[1].strip().upper()
                    else:
                        sym_token = args[0].strip().upper()
                if sym_token and sym_token != 'ALL':
                    rs = resolve_symbol_strict(sym_token)
                    if not rs:
                        await msg.reply_text(f"Symbol '{sym_token}' not found.")
                        return True
                    n = cancel_all_pending(symbol=rs, include_all_magics=True)
                    await msg.reply_text(f"Cancelled {n} pending order(s) for {rs}.")
                else:
                    n = cancel_all_pending(include_all_magics=True)
                    await msg.reply_text(f"Cancelled {n} pending order(s) (all symbols).")
            except Exception as e:
                await msg.reply_text(f"Delete failed: {e}")
            return

        # /close [all|profit|loss|SYMBOL]
        if cmd == '/close' or cmd == '/closeall':
            try:
                mt5_login_safe()
                only_profit = None
                if args:
                    a0 = args[0].lower()
                    if a0 in ('all',):
                        n = close_all_positions(include_all_magics=True)
                        await msg.reply_text(f"Closed {n} open position(s) (all symbols).")
                        return
                    if a0 in ('profit', 'profits'):
                        only_profit = True
                        n = close_all_positions(include_all_magics=True, only_profit=True)
                        await msg.reply_text(f"Closed {n} profitable position(s) (all symbols).")
                        return
                    if a0 in ('loss', 'losing', 'losses'):
                        only_profit = False
                        n = close_all_positions(include_all_magics=True, only_profit=False)
                        await msg.reply_text(f"Closed {n} losing position(s) (all symbols).")
                        return
                    # else treat as symbol
                    sym_token = args[0].strip().upper()
                    rs = resolve_symbol_strict(sym_token)
                    if not rs:
                        await msg.reply_text(f"Symbol '{sym_token}' not found.")
                        return True
                    n = close_all_positions(symbol=rs, include_all_magics=True)
                    await msg.reply_text(f"Closed {n} open position(s) for {rs}.")
                    return
                # no args -> all
                n = close_all_positions(include_all_magics=True)
                await msg.reply_text(f"Closed {n} open position(s) (all symbols).")
            except Exception as e:
                await msg.reply_text(f"Close failed: {e}")
            return

        # /close profit and /close loss explicit (aliases)
        if cmd in ('/closeprofit', '/closeloss'):
            try:
                mt5_login_safe()
                only_profit = True if cmd == '/closeprofit' else False
                n = close_all_positions(include_all_magics=True, only_profit=only_profit)
                await msg.reply_text(
                    f"Closed {n} {'profitable' if only_profit else 'losing'} position(s) (all symbols)."
                )
            except Exception as e:
                await msg.reply_text(f"Close failed: {e}")
            return

        # /kill [SYMBOL|all]
        if cmd == '/kill':
            try:
                mt5_login_safe()
                if args and args[0].strip().lower() != 'all':
                    sym_token = args[0].strip().upper()
                    rs = resolve_symbol_strict(sym_token)
                    if not rs:
                        await msg.reply_text(f"Symbol '{sym_token}' not found.")
                        return True
                    n_pos = close_all_positions(symbol=rs, include_all_magics=True)
                    n_pend = cancel_all_pending(symbol=rs, include_all_magics=True)
                    await msg.reply_text(f"Kill {rs}: closed {n_pos} positions, cancelled {n_pend} pending.")
                else:
                    n_pos = close_all_positions(include_all_magics=True)
                    n_pend = cancel_all_pending(include_all_magics=True)
                    await msg.reply_text(f"Kill all: closed {n_pos} positions, cancelled {n_pend} pending.")
            except Exception as e:
                await msg.reply_text(f"Kill failed: {e}")
            return

    # Flexible order formats (auto-sense):
    # 1) Single entry with Lots=N (repeat same-price orders)
    # 2) Multiple entry->TP pairs under a Buy/Sell limit list
    fmt1 = _parse_flexible_format1(text)
    if fmt1 is not None:
        try:
            mt5_login_safe()
            sym = resolve_symbol(SYMBOL_DEFAULT)
            direction = fmt1['direction']
            entry = fmt1['entry']
            sl = fmt1['sl']
            tp = fmt1['tp']
            lots = fmt1['lots']
            vol_each = adjust_lot_for_symbol(sym, fmt1['lot_size'])
            placed = 0
            for _ in range(lots):
                if place_pending_limit(sym, direction, entry, sl, tp, vol_each):
                    placed += 1
            await msg.reply_text(f"Placed {placed}/{lots} {direction} LIMIT @ {entry}, lot {vol_each}, SL {sl}, TP {tp if tp is not None else 'open'}.")
        except Exception as e:
            await msg.reply_text(f"Order placement failed: {e}")
        return

    fmt2 = _parse_flexible_format2(text)
    if fmt2 is not None:
        try:
            mt5_login_safe()
            sym = resolve_symbol(SYMBOL_DEFAULT)
            direction = fmt2['direction']
            lot_size = fmt2['lot_size']
            sl = fmt2['sl']
            vol_each = adjust_lot_for_symbol(sym, lot_size)
            entries: List[Tuple[float, Optional[float]]] = fmt2['entries']
            placed = 0
            for entry, tp in entries:
                if place_pending_limit(sym, direction, entry, sl, tp, vol_each):
                    placed += 1
            await msg.reply_text(f"Placed {placed}/{len(entries)} {direction} LIMIT orders, lot {vol_each}, SL {sl}.")
        except Exception as e:
            await msg.reply_text(f"Order placement failed: {e}")
        return

    parsed = parse_signal(text)
    if parsed:
        with signal_lock:
            last_signal = parsed
        log.info(f"Parsed signal: {parsed.direction} {parsed.symbol} lot {parsed.lot_size} zone {parsed.zone_low}/{parsed.zone_high} SL {parsed.stop_loss} TPs {parsed.take_profits}")
        # Update TP ladder cache for symbol (raw and with suffix)
        try:
            numeric = [tp for tp in parsed.take_profits if tp is not None]
            numeric = sorted(set(numeric))
            if numeric:
                TP_LADDERS[(parsed.symbol.upper(), parsed.direction)] = numeric
                if SYMBOL_SUFFIX:
                    TP_LADDERS[(f"{parsed.symbol.upper()}{SYMBOL_SUFFIX}", parsed.direction)] = numeric
                _save_tp_ladders()
        except Exception:
            pass
        # Auto-place pending orders if enabled
        if AUTO_PLACE:
            try:
                ok = place_orders_from_signal(parsed)
                if ok:
                    log.info("Auto-place: orders placed successfully.")
                else:
                    log.warning("Auto-place: no orders were placed.")
            except Exception as e:
                log.error(f"Auto-place failed: {e}")
    else:
        log.info("Message ignored (did not match signal format)")

async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    m = update.effective_message
    if not m:
        return
    c = m.chat
    cid = getattr(c, 'id', None)
    title = getattr(c, 'title', None)
    await m.reply_text(f"Chat ID: {cid}\nTitle: {title or '-'}\n\nSet this ID in Configuration → Telegram Chat ID.")


def run_telegram_bot():
    token = (TELEGRAM_TOKEN or "").strip()
    if not token:
        log.error("TELEGRAM_TOKEN not set; cannot start Telegram bot")
        return
    if any(c.isspace() for c in token):
        log.error("TELEGRAM_TOKEN contains whitespace/newline. Please paste a clean token without spaces or newlines.")
        return

    async def main():
        global telegram_connected, telegram_channel_name
        app = Application.builder().token(token).build()
        # Handle normal chat messages
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        # Fallback to catch channel posts and any text via effective_message
        app.add_handler(TypeHandler(Update, handle_message))
        await app.initialize()
        # Try to fetch chat info if configured
        try:
            if TELEGRAM_CHANNEL_ID:
                try:
                    chat_id = int(TELEGRAM_CHANNEL_ID)
                except Exception:
                    chat_id = TELEGRAM_CHANNEL_ID
                chat = await app.bot.get_chat(chat_id)
                telegram_channel_name = getattr(chat, 'title', None) or str(getattr(chat, 'id', TELEGRAM_CHANNEL_ID))
                telegram_connected = True
                log.info(f"Telegram: connected to chat '{telegram_channel_name}'")
        except Exception as e:
            log.warning(f"Telegram: could not resolve chat info: {e}")
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        import asyncio as _asyncio
        await _asyncio.Event().wait()  # keep running

    import asyncio
    STOP_TG.clear()
    try:
        asyncio.run(main())
    except Exception as e:
        log.error(f"Telegram loop error: {e}")


def get_last_signal() -> Optional[ParsedSignal]:
    with signal_lock:
        return last_signal


def get_status() -> dict:
    """Lightweight status for UI."""
    try:
        term = mt5.terminal_info()
    except Exception:
        term = None
    return {
        "mt5_connected": term is not None,
        "telegram_connected": telegram_connected,
        "telegram_channel_name": telegram_channel_name,
    }


def stop_telegram():
    """Signal the Telegram runner to stop."""
    try:
        STOP_TG.set()
    except Exception:
        pass


def stop():
    STOP_EVENT.set()


def main_cli():
    mt5_login()
    log.info("Fore_Ai CLI: waiting for signals...")
    run_telegram_bot()


if __name__ == "__main__":
    main_cli()
def apply_config(updates: dict):
    """Update selected configuration values at runtime from a dict of {name: value}."""
    global TELEGRAM_TOKEN, TELEGRAM_CHANNEL_ID
    global LOGIN, PASSWORD, SERVER
    global SYMBOL_DEFAULT, SYMBOL_SUFFIX, LOT_FALLBACK, SLIPPAGE, MAGIC
    for k, v in (updates or {}).items():
        try:
            if k == "TELEGRAM_TOKEN":
                TELEGRAM_TOKEN = str(v or "")
            elif k == "TELEGRAM_CHANNEL_ID":
                TELEGRAM_CHANNEL_ID = str(v or "")
            elif k == "LOGIN" or k == "MT5_LOGIN":
                LOGIN = int(v) if (v is not None and str(v).strip() != "") else None
            elif k == "PASSWORD" or k == "MT5_PASSWORD":
                PASSWORD = str(v) if v is not None else None
            elif k == "SERVER" or k == "MT5_SERVER":
                SERVER = str(v) if v is not None else None
            elif k == "SYMBOL":
                SYMBOL_DEFAULT = str(v or SYMBOL_DEFAULT)
            elif k == "SYMBOL_SUFFIX":
                SYMBOL_SUFFIX = str(v or "")
            elif k == "LOT_FALLBACK":
                LOT_FALLBACK = float(v)
            elif k == "SLIPPAGE":
                SLIPPAGE = int(v)
            elif k == "MAZHAR_MAGIC" or k == "FORE_AI_MAGIC":
                MAGIC = int(v)
            else:
                continue
            log.info(f"CONFIG UPDATED: {k} -> {v}")
        except Exception as e:
            log.warning(f"CONFIG UPDATE FAILED: {k} ({e})")
