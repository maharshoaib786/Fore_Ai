# Fore_Ai

Telegramâ€‘driven MT5 bot with a Tkinter dashboard. It parses trade signals from your Telegram group, places a ladder of pending orders across a price zone, and manages stepâ€‘trailing stops based on TP levels.

> Important: Trading involves substantial risk. Always test on a demo account first. You are responsible for any financial outcomes.


## Features

- Signal parsing (example format):
  - `Lot Size 0.20`
  - `XAUUSD LOOKING BUY THIS ZONE`
  - `3463/3459`
  - `SL 3453`
  - `TP1 3467`, `TP2 3471`, `TP3 3476`, `TP4 3485`, `TP5 open`
- Zone ladder orders (no midpoint):
  - BUY â†’ pending BUY LIMITs from zone high down to low inclusive (e.g., 3463, 3462, 3461, 3460, 3459)
  - SELL â†’ pending SELL LIMITs from zone low up to high inclusive
  - Equal lot split across all ladder orders (adjusted to symbolâ€™s volume step)
- Lot selection priority:
  - Lot from signal if present
  - Else â€œDefault Lot (when missing)â€ from dashboard Trade Settings
  - Else `LOT_FALLBACK` from `.env`
- TPs and trailing stops:
  - Orders are initially placed with TP1 (if numeric)
  - Trailing logic runs every second:
    - When price reaches TP1 â†’ set SL to breakâ€‘even (entry)
    - Reaches TP2 â†’ set SL to TP1
    - TP3 â†’ set SL to TP2 â€¦ and so on
- Dashboard actions: Start Telegram, Place Orders (for last parsed signal), Cancel Pending, Close All
- Persists dashboard layout and default lot in `dashboard_prefs.json`


## Requirements

- Windows (tested) with MetaTrader 5 terminal installed
- Python 3.10+
- MT5 terminal logged in (or provide credentials in `.env`)

Python packages (already listed at repo root `requirements.txt`):

```
MetaTrader5
python-dotenv
python-telegram-bot>=20.7
```

Install:

```
python -m pip install -r requirements.txt
```


## Configuration (.env)

Create a `.env` at the project root (same folder as `requirements.txt`). Example:

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
```

Notes:
- If your broker uses `XAUUSDm`, set `SYMBOL_SUFFIX=m`. The bot tries signal symbol + suffix, then raw symbol, then `SYMBOL` as fallback.
- Values are used by `Fore_Ai/fore_ai_bot.py` at runtime. Note: for backward compatibility, the bot also reads `MAZHAR_MAGIC` if `FORE_AI_MAGIC` is not set.


## Run The Dashboard

- Doubleâ€‘click `Fore_Ai/START FORE_AI.vbs` (no console), or
- Doubleâ€‘click `Fore_Ai/run_dashboard.bat`, or
- From a terminal:
  - `python "Fore_Ai/fore_ai_dashboard.py"`

In the dashboard:
- Click â€œStart Telegramâ€ to begin reading messages
- When a valid signal is parsed, details appear under â€œLast Parsed Signalâ€
- Set â€œDefault Lot (when missing)â€ in â€œTrade Settingsâ€ and click â€œSave Lotâ€
- Click â€œPlace Ordersâ€ to place the zone ladder of pending LIMIT orders
- Use â€œCancel Pendingâ€ to remove pending orders placed by this bot
- Use â€œClose Allâ€ to close open positions from this bot (by magic)


## Signal Format Details

- Lot Size is optional. If omitted, the dashboardâ€™s Default Lot (or `LOT_FALLBACK`) is used.
- Zone must be two numbers separated by `/`, e.g., `3463/3459`.
- TPs can include the word `open` (ignored for numeric TP targeting). Trailing uses numeric TP levels only.

Example message:

```
Lot Size 0.20
XAUUSD LOOKING BUY THIS ZONE
3463/3459
SL 3453
TP1 3467
TP2 3471
TP3 3476
TP4 3485
TP5 open
```


## How Trailing Works

- The bot caches the numeric TP ladder from the most recent parsed signal per (symbol, direction)
- Every second, it checks open positions (by magic):
  - BUY: if price â‰¥ TP1 â†’ SL = entry; if â‰¥ TP2 â†’ SL = TP1; if â‰¥ TP3 â†’ SL = TP2; etc.
  - SELL: if price â‰¤ TP1 â†’ SL = entry; if â‰¤ TP2 â†’ SL = TP1; â€¦
- SL only moves in a favorable direction and stays on the safe side of the current price.
- Note: TP ladders are not persisted; after restart, trailing resumes after the next valid signal is received.


## Troubleshooting

- MT5 initialize() failed: ensure MT5 terminal is installed and running, and credentials or a loggedâ€‘in session exist.
- Symbol not found: check `SYMBOL` and `SYMBOL_SUFFIX` and that the symbol is enabled in the terminal.
- No signals received: verify `TELEGRAM_TOKEN`, `TELEGRAM_CHANNEL_ID`, and that the bot is a member of the group/channel sending messages.
- Orders not placed: check lot/volume min/step and that SL/TP are within valid distances for your broker.
- Trailing not moving SL: ensure a numeric TP ladder has been parsed for that symbol/direction since startup.


## Disclaimer

This software is provided â€œas isâ€ without warranties. Trading is risky; use at your own risk and test thoroughly on a demo account.


