# Fore_Ai

Telegram-driven MT5 bot with a Tkinter dashboard. It reads trade signals from Telegram, places a ladder of pending orders across a price zone, and manages step-trailing stops based on TP levels.

> Important: Trading involves substantial risk. Always test on a demo account first. You are responsible for any financial outcomes.


## Features

- Signal parsing (example format):
  - `Lot Size 0.20`
  - `XAUUSD LOOKING BUY THIS ZONE`
  - `3463/3459`
  - `SL 3453`
  - `TP1 3467`, `TP2 3471`, `TP3 3476`, `TP4 3485`, `TP5 open`
- Zone ladder orders (no midpoint):
  - BUY: pending BUY LIMITs from zone high down to low inclusive (e.g., 3463, 3462, 3461, 3460, 3459)
  - SELL: pending SELL LIMITs from zone low up to high inclusive
- Lot per order (no splitting):
  - Uses the signal’s lot size per order if present
  - Else uses dashboard “Default Lot (when missing)”
  - Else uses `LOT_FALLBACK` from `.env`
  - Final volume respects the symbol’s `volume_step` and min/max
- TPs and trailing stops:
  - Orders are initially placed with TP1 (if numeric)
  - Trailing runs every second:
    - Hitting TP1 → move SL to entry (break-even)
    - Hitting TP2 → move SL to TP1
    - TP3 → move SL to TP2, and so on
- Telegram commands (in chat with the bot):
  - `/cancel [all|SYMBOL]` → cancel pending orders
  - `/delete pending [all|SYMBOL]` → alias for cancel pending
  - `/delete pending <SYMBOL> <BUY|SELL>` → cancel only that side’s LIMIT orders
  - `/delete order <price> <SYMBOL> <BUY|SELL>` → delete specific LIMIT order by price
  - `/close [all|profit|loss|SYMBOL]` → close positions
  - `/kill [all|SYMBOL]` → close positions and cancel pending together
  - `/be SYMBOL [BUY|SELL]` or `/be ALL` → set SL to break-even
  - `/sl move <from> to <to> <SYMBOL|ALL> [BUY|SELL]` → move SL for matching positions
  - `/ch buylimit <from> to <to> <SYMBOL>` → change BUY LIMIT price
  - `/ch selllimit <from> to <to> <SYMBOL>` → change SELL LIMIT price
  - `/id` → returns the current chat id/title (useful for configuration)
 
 ### Telegram Commands (Full list)
 
 - `/getid`: returns the current chat ID and title (useful for configuration)
 - Cancel pending orders:
   - `/cancel [all|SYMBOL]`
   - `/cancelall` (alias)
   - `/delete pending [SYMBOL]`
   - `/delete pending <SYMBOL> <BUY|SELL>` (cancel only that side's LIMIT orders)
   - `/delete order <price> <SYMBOL> <BUY|SELL>` (delete a specific LIMIT order by price)
 - Close positions:
   - `/close [all|profit|loss|SYMBOL]`
   - `/closeall` (alias)
   - `/closeprofit` (close only profitable positions)
   - `/closeloss` (close only losing positions)
 - Bulk kill (positions + pending):
   - `/kill ALL` or `/kill SYMBOL`
 - Risk management:
   - `/be SYMBOL [BUY|SELL]` or `/be ALL` (set SL to break-even for matching positions)
   - `/sl move <from> to <to> <SYMBOL|ALL> [BUY|SELL]` (move SL for positions whose current SL matches <from>)
 - Modify pending LIMIT prices:
   - `/ch buylimit <from> to <to> <SYMBOL>`
   - `/ch selllimit <from> to <to> <SYMBOL>`
 
 - Dashboard actions: Start Telegram, Place Orders (last parsed signal), Close All Pending, Close All Positions
- Dashboard toggles & prefs: “Auto place on signal”, “Default Lot (when missing)”; persisted in `dashboard_prefs.json`
- Updater & releases (Dashboard → Updates tab):
  - Check for updates from GitHub and install selected versions
  - Publish a release (requires `GITHUB_TOKEN`/`GH_TOKEN` in env or `Fore_Ai/.env`)


## Requirements

- Windows (tested) with MetaTrader 5 terminal installed
- Python 3.10+
- MT5 terminal logged in (or provide credentials in `.env`)

Python packages (already listed at repo root `requirements.txt`):

```
MetaTrader5
python-dotenv
python-telegram-bot>=20,<21
```

Install:

```
python -m pip install -r requirements.txt
```


## Configuration (.env)

Create a `.env` in the project root (same folder as `requirements.txt`). Example:

```
# MT5 login (optional if your terminal is already logged in)
MT5_LOGIN=1234567
MT5_PASSWORD=your_password
MT5_SERVER=YourBroker-Server

# Telegram
TELEGRAM_TOKEN=123456789:ABCDEF_your_bot_token
TELEGRAM_CHANNEL_ID=-1001234567890

# Symbol mapping
SYMBOL=XAUUSD            # default fallback
SYMBOL_SUFFIX=m          # set to m if your broker uses XAUUSDm; leave empty otherwise

# Bot params
LOT_FALLBACK=0.10        # used if signal lot is missing and dashboard lot is empty
FORE_AI_MAGIC=777001
SLIPPAGE=30

# Optional: GitHub token to increase API limits for updater/publisher
# GITHUB_TOKEN=ghp_your_token_here
```

Notes:
- If your broker uses `XAUUSDm`, set `SYMBOL_SUFFIX=m`. The bot tries signal symbol + suffix, then raw symbol, then `SYMBOL` as fallback.
- Values are used by `Fore_Ai/fore_ai_bot.py` at runtime. For backward compatibility, the bot also reads `MAZHAR_MAGIC` if `FORE_AI_MAGIC` is not set.


## Run The Dashboard

- Double-click `Fore_Ai/START FORE_AI.vbs` (no console), or
- Double-click `Fore_Ai/run_dashboard.bat`, or
- From a terminal:
  - `python "Fore_Ai/fore_ai_dashboard.py"`

In the dashboard:
- Click “Start Telegram” to begin reading messages
- When a valid signal is parsed, details appear under “Last Parsed Signal”
- Set “Default Lot (when missing)” in “Trade Settings” and click “Save Lot”
- Click “Place Orders” to place the zone ladder of pending LIMIT orders
- Use “Close All Pending” to remove pending orders placed by this bot
- Use “Close All Positions” to close open positions from this bot (by magic)

### Flexible Signal Formats (extras)

In addition to the standard format above, the bot can auto-detect a few convenient templates:

- Format 1 (repeat same-price orders):
  - `Lot size=0.10`
  - `Lots=10`
  - `Buy limit=3410`
  - `TP 3420`
  - `SL 3400`

- Format 2 (multiple entries, optional TP per entry, common lot/SL):
  - `Buy limit 3410 -> TP 3420`
  - `Buy limit 3408 -> TP 3418`
  - `SL 3400`

- Format 3 (market orders — Buy Now / Sell Now):
  - Example:
    - `BTCUSD`
    - `Lot Size=0.10`
    - `Lots=6`
    - `Buy Now` (or `Sell Now`)
    - `TP1->110000`, `TP2->110500`, `TP3->111000`, `TP4->112000`, `TP5->114000`, `TP6->open`
    - `SL 109000`
  - Places N market orders immediately, each with the specified lot size (adjusted to `volume_step`).
  - TPs map across orders in order; extra orders use open TP.

- Format 4 (pending STOP orders with entry→TP pairs):
  - Example Sell Stop:
    - `XAUUSD`
    - `Lot Size=0.10`
    - `Lots=5`
    - `Sell Stop`
    - `3647 -> TP 3646`
    - `3646 -> TP 3645`
    - `3645 -> TP 3644`
    - `3644 -> TP 3643`
    - `3643 -> TP 3642`
  - Example Buy Stop (same structure, with `Buy Stop`)
  - Places up to N STOP orders (BUY/SELL) using given entry and per-order TP.
- Format 3 (market orders — Buy Now / Sell Now):
  - Example:
    - `BTCUSD`
    - `Lot Size=0.10`
    - `Lots=6`
    - `Buy Now` (or `Sell Now`)
    - `TP1->110000`, `TP2->110500`, `TP3->111000`, `TP4->112000`, `TP5->114000`, `TP6->open`
    - `SL 109000`
  - Places N market orders immediately, each with the specified lot size (adjusted to `volume_step`).
  - TPs map across orders in order; extra orders use open TP.


## How Trailing Works

- The bot caches the numeric TP ladder from the most recent parsed signal per (symbol, direction)
- Every second, it checks open positions (by magic):
  - BUY: if price ≥ TP1 → SL = entry; if ≥ TP2 → SL = TP1; if ≥ TP3 → SL = TP2; etc.
  - SELL: if price ≤ TP1 → SL = entry; if ≤ TP2 → SL = TP1; etc.
- SL only moves in a favorable direction and stays on the safe side of current price.
- TP ladders persist in `Fore_Ai/tp_ladders.json` and load on startup so trailing can resume after restart (if a ladder was previously recorded for that symbol/direction).


## Troubleshooting

- MT5 initialize() failed: ensure MT5 terminal is installed and running, and credentials or a logged-in session exist.
- Symbol not found: check `SYMBOL` and `SYMBOL_SUFFIX` and that the symbol is enabled in the terminal.
- No signals received: verify `TELEGRAM_TOKEN`, `TELEGRAM_CHANNEL_ID`, and that the bot is a member of the group/channel sending messages.
- Orders not placed: check lot/volume min/step and that SL/TP are within valid distances for your broker.
- Trailing not moving SL: ensure a numeric TP ladder exists for that symbol/direction (it persists once seen).


## Disclaimer

This software is provided “as is” without warranties. Trading is risky; use at your own risk and test thoroughly on a demo account.
