“””
Trading Brain Bot - Full Live Trading Version

- Runs 6x daily
- Fetches live T212 portfolio
- Calls Claude for analysis
- Sends WhatsApp with 5 min cancel window
- Executes trade automatically if not cancelled
  “””

import os
import requests
import schedule
import time
import base64
import threading
from datetime import datetime
from anthropic import Anthropic
from flask import Flask, request

# ── Configuration ─────────────────────────────────────────────────────────────

T212_API_KEY       = os.environ.get(“T212_API_KEY”)
T212_API_SECRET    = os.environ.get(“T212_API_SECRET”)
ANTHROPIC_API_KEY  = os.environ.get(“ANTHROPIC_API_KEY”)
TWILIO_ACCOUNT_SID = os.environ.get(“TWILIO_ACCOUNT_SID”)
TWILIO_AUTH_TOKEN  = os.environ.get(“TWILIO_AUTH_TOKEN”)
TWILIO_FROM        = os.environ.get(“TWILIO_WHATSAPP_FROM”)
TWILIO_TO          = os.environ.get(“TWILIO_WHATSAPP_TO”)

T212_ENV           = os.environ.get(“T212_ENV”, “live”)
MAX_TRADE_AMOUNT   = 100  # Hard cap £100 per trade
CANCEL_WINDOW_SECS = 300  # 5 minutes

# ── State ─────────────────────────────────────────────────────────────────────

# Stores the pending trade while we wait for possible cancellation

pending_trade = {
“active”: False,
“ticker”: None,
“action”: None,   # “BUY” or “SELL”
“amount”: None,
“cancelled”: False
}

# ── Flask Web Server (listens for CANCEL replies) ─────────────────────────────

app = Flask(**name**)

@app.route(”/cancel”, methods=[“POST”])
def handle_cancel():
“”“Twilio calls this endpoint when you reply to the WhatsApp message.”””
incoming = request.form.get(“Body”, “”).strip().upper()
sender   = request.form.get(“From”, “”)

```
if sender == TWILIO_TO and incoming == "CANCEL":
    if pending_trade["active"]:
        pending_trade["cancelled"] = True
        pending_trade["active"]    = False
        print("🚫 Trade cancelled via WhatsApp")
        send_whatsapp("🚫 Trade cancelled. No action taken.")
    else:
        send_whatsapp("No pending trade to cancel.")
else:
    print(f"Ignored message: '{incoming}' from {sender}")

# Must return a valid TwiML response (even if empty)
return '<?xml version="1.0" encoding="UTF-8"?><Response></Response>', 200
```

def start_web_server():
“”“Run Flask in a background thread so it doesn’t block the scheduler.”””
app.run(host=“0.0.0.0”, port=8080, debug=False, use_reloader=False)

# ── T212 API ───────────────────────────────────────────────────────────────────

def t212_headers():
credentials = base64.b64encode(
f”{T212_API_KEY}:{T212_API_SECRET}”.encode()
).decode()
return {“Authorization”: f”Basic {credentials}”}

def get_t212_portfolio():
base_url = f”https://{T212_ENV}.trading212.com/api/v0”
headers  = t212_headers()

```
try:
    summary   = requests.get(f"{base_url}/equity/account/summary",
                             headers=headers, timeout=10)
    summary.raise_for_status()

    positions = requests.get(f"{base_url}/equity/portfolio",
                             headers=headers, timeout=10)
    positions.raise_for_status()

    return summary.json(), positions.json()

except requests.RequestException as e:
    print(f"T212 API error: {e}")
    return None, None
```

def get_instrument_details(ticker):
“”“Get the current price and fractional share info for a ticker.”””
base_url = f”https://{T212_ENV}.trading212.com/api/v0”
headers  = t212_headers()

```
try:
    resp = requests.get(f"{base_url}/equity/metadata/instruments",
                        headers=headers, timeout=10)
    resp.raise_for_status()
    instruments = resp.json()

    for inst in instruments:
        if inst.get("ticker") == ticker:
            return inst

    return None

except requests.RequestException as e:
    print(f"Instrument lookup error: {e}")
    return None
```

def get_current_price(ticker):
“”“Fetch the current price of a ticker from the portfolio or instruments endpoint.”””
base_url = f”https://{T212_ENV}.trading212.com/api/v0”
headers  = t212_headers()
try:
resp = requests.get(
f”{base_url}/equity/portfolio”,
headers=headers, timeout=10
)
resp.raise_for_status()
for pos in resp.json():
if pos.get(“ticker”) == ticker:
return pos.get(“currentPrice”, 0)
# Not in portfolio — fetch from instruments list
resp2 = requests.get(
f”{base_url}/equity/metadata/instruments”,
headers=headers, timeout=10
)
resp2.raise_for_status()
for inst in resp2.json():
if inst.get(“ticker”) == ticker:
return inst.get(“currentPrice”, 0)
return None
except requests.RequestException as e:
print(f”Price fetch error: {e}”)
return None

def execute_trade(ticker, action, amount_gbp):
“”“Place a market order on Trading 212.”””
base_url = f”https://{T212_ENV}.trading212.com/api/v0”
headers  = t212_headers()
headers[“Content-Type”] = “application/json”

```
try:
    if action == "BUY":
        # T212 only supports orders by QUANTITY, not value
        # So we fetch the current price and calculate shares to buy
        price = get_current_price(ticker)
        if not price or price <= 0:
            print(f"Could not get price for {ticker}")
            return False
        quantity = round(amount_gbp / price, 6)  # fractional shares
        payload = {
            "ticker":   ticker,
            "quantity": quantity
        }

    if action == "SELL":
        # For sells, get current quantity and sell all
        _, positions = get_t212_portfolio()
        quantity = 0
        if positions:
            for pos in positions:
                if pos.get("ticker") == ticker:
                    quantity = pos.get("quantity", 0)
                    break

        if quantity <= 0:
            print(f"No position found for {ticker} to sell")
            return False

        payload = {
            "ticker":   ticker,
            "quantity": -abs(quantity)  # negative = sell
        }

    resp = requests.post(
        f"{base_url}/equity/orders/market",
        headers=headers,
        json=payload,
        timeout=10
    )
    resp.raise_for_status()
    print(f"✅ Trade executed: {action} {ticker} £{amount_gbp}")
    return True

except requests.RequestException as e:
    print(f"Trade execution error: {e}")
    return False
```

def format_portfolio_context(summary, positions):
if not summary:
return “Portfolio data unavailable.”

```
cash            = summary.get("cash", {})
investments     = summary.get("investments", {})
available_cash  = cash.get("availableToTrade", 0)
total_value     = summary.get("totalValue", 0)
unrealised_pnl  = investments.get("unrealizedProfitLoss", 0)

text = (
    f"Portfolio: £{total_value:.2f} total | "
    f"£{available_cash:.2f} cash | "
    f"P&L: £{unrealised_pnl:.2f}\nPositions:\n"
)

if positions:
    for pos in positions:
        ticker      = pos.get("ticker", "?")
        avg_price   = pos.get("averagePrice", 0)
        cur_price   = pos.get("currentPrice", 0)
        pnl         = pos.get("ppl", 0)
        pnl_pct     = ((cur_price - avg_price) / avg_price * 100) if avg_price else 0
        text += (
            f"• {ticker}: avg £{avg_price:.2f} | "
            f"now £{cur_price:.2f} | "
            f"P&L £{pnl:.2f} ({pnl_pct:+.1f}%)\n"
        )
else:
    text += "• No open positions\n"

return text
```

# ── Claude API ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = “””
You are an aggressive trading analyst managing a £100–£500 portfolio on Trading 212.
Your job is to beat the market. Be concise — entire response must stay under 1100 characters.

RULES:

1. Max £100 on a single trade
1. Only enter if you can state the catalyst, why NOW, and target exit
1. Take 50% profit at +15–25%, let rest run
1. Cut a position if it won’t recover to break-even within one week
1. Never revenge trade
1. Hold cash is a valid call

RESPOND IN THIS EXACT FORMAT — keep each line brief:

🌍 MARKET: [one sentence]
🔍 SCAN: [one sentence on opportunity or why nothing]
⚡ ACTION: BUY / SELL / HOLD CASH
📈 STOCK: [Ticker symbol only, e.g. NVDA_US_EQ] (or N/A)
💰 SIZE: £[amount under £100] (or N/A)
🎯 TARGET: £[price] (or N/A)
✂️ STOP: [max 10 words]
🔥 CONVICTION: [X/10]
📋 PORTFOLIO: [one sentence]
⚠️ RISK: [one sentence]

IMPORTANT: For STOCK, use the exact Trading 212 ticker format e.g. AAPL_US_EQ, NVDA_US_EQ, TSLA_US_EQ
“””

def get_trade_recommendation(portfolio_context):
client = Anthropic(api_key=ANTHROPIC_API_KEY)

```
user_message = (
    f"{portfolio_context}\n"
    f"Date/Time: {datetime.now().strftime('%d %b %Y, %H:%M')}\n\n"
    f"Scan the market now. Find the best opportunity or confirm hold cash. "
    f"Keep response under 1100 characters."
)

try:
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": user_message}]
    )

    return " ".join(
        block.text for block in response.content
        if hasattr(block, "text") and block.text is not None
    ).strip()

except Exception as e:
    print(f"Claude API error: {e}")
    return None
```

def parse_recommendation(analysis):
“”“Extract action, ticker, and amount from Claude’s response.”””
action = None
ticker = None
amount = None

```
for line in analysis.split("\n"):
    line = line.strip()
    if line.startswith("⚡ ACTION:"):
        val = line.replace("⚡ ACTION:", "").strip().upper()
        if "BUY" in val:
            action = "BUY"
        elif "SELL" in val:
            action = "SELL"
        else:
            action = "HOLD"
    if line.startswith("📈 STOCK:"):
        val = line.replace("📈 STOCK:", "").strip()
        if val.upper() != "N/A":
            ticker = val.split()[0]  # take first word only
    if line.startswith("💰 SIZE:"):
        val = line.replace("💰 SIZE:", "").strip()
        val = val.replace("£", "").replace(",", "").split()[0]
        try:
            amount = min(float(val), MAX_TRADE_AMOUNT)
        except ValueError:
            amount = None

return action, ticker, amount
```

# ── Twilio WhatsApp ────────────────────────────────────────────────────────────

def send_whatsapp(message):
from twilio.rest import Client

```
if len(message) > 1500:
    message = message[:1497] + "..."

try:
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    msg = client.messages.create(
        from_=TWILIO_FROM,
        to=TWILIO_TO,
        body=message
    )
    print(f"✅ WhatsApp sent: {msg.sid}")
    return True
except Exception as e:
    print(f"WhatsApp error: {e}")
    return False
```

# ── Main Job ───────────────────────────────────────────────────────────────────

def run_trading_check():
global pending_trade

```
print(f"\n{'='*50}")
print(f"Trading check at {datetime.now().strftime('%H:%M:%S')}")
print(f"{'='*50}")

# 1. Fetch portfolio
summary, positions = get_t212_portfolio()
portfolio_context  = format_portfolio_context(summary, positions)
print(portfolio_context)

# 2. Get recommendation
print("Calling Claude...")
analysis = get_trade_recommendation(portfolio_context)

if not analysis:
    send_whatsapp("⚠️ Trading Brain: Analysis failed. Check Railway logs.")
    return

print(f"Analysis ({len(analysis)} chars):\n{analysis}")

# 3. Parse the recommendation
action, ticker, amount = parse_recommendation(analysis)
print(f"Parsed: action={action}, ticker={ticker}, amount={amount}")

# 4. If holding cash, just send the analysis
if action == "HOLD" or not ticker or not amount:
    timestamp = datetime.now().strftime("%d %b, %H:%M")
    send_whatsapp(f"🤖 TRADING BRAIN — {timestamp}\n\n{analysis}")
    return

# 5. Work out funding source for BUY orders
funding_line = ""
if action == "BUY" and summary:
    available_cash = summary.get("cash", {}).get("availableToTrade", 0)

    if available_cash >= amount:
        # Enough cash — simple case
        funding_line = (
            f"💵 FUNDED FROM: Cash balance "
            f"(£{available_cash:.2f} available)"
        )
    else:
        # Not enough cash — show what would need selling
        shortfall = amount - available_cash
        funding_line = (
            f"💵 FUNDED FROM: £{available_cash:.2f} cash"
        )
        if positions:
            # Find the smallest position that covers the shortfall
            candidates = [
                p for p in positions
                if p.get("currentPrice", 0) * p.get("quantity", 0) >= shortfall
            ]
            if candidates:
                # Pick the one closest in value to the shortfall
                best = min(
                    candidates,
                    key=lambda p: abs(
                        p.get("currentPrice", 0) * p.get("quantity", 0) - shortfall
                    )
                )
                pos_value = best.get("currentPrice", 0) * best.get("quantity", 0)
                funding_line += (
                    f" + selling {best.get('ticker')} "
                    f"(worth ~£{pos_value:.2f})\n"
                    f"⚠️ Note: {best.get('ticker')} will be sold first "
                    f"to cover the shortfall of £{shortfall:.2f}"
                )
            else:
                funding_line += (
                    f"\n⚠️ Warning: Only £{available_cash:.2f} available — "
                    f"insufficient funds for this trade"
                )

elif action == "SELL":
    # Show what we're selling and its current value
    if positions:
        for pos in positions:
            if pos.get("ticker") == ticker:
                pos_value = pos.get("currentPrice", 0) * pos.get("quantity", 0)
                pnl       = pos.get("ppl", 0)
                pnl_pct   = (
                    (pos.get("currentPrice", 0) - pos.get("averagePrice", 0))
                    / pos.get("averagePrice", 0) * 100
                ) if pos.get("averagePrice", 0) else 0
                funding_line = (
                    f"💵 SELLING: {ticker} "
                    f"(current value £{pos_value:.2f} | "
                    f"P&L £{pnl:.2f} / {pnl_pct:+.1f}%)"
                )
                break

# 6. Set pending trade and notify via WhatsApp
pending_trade = {
    "active":    True,
    "ticker":    ticker,
    "action":    action,
    "amount":    amount,
    "cancelled": False
}

timestamp = datetime.now().strftime("%d %b, %H:%M")
message = (
    f"🤖 TRADING BRAIN — {timestamp}\n\n"
    f"{analysis}\n\n"
    f"{funding_line}\n\n"
    f"⏳ I will {action} £{amount:.0f} of {ticker} in 5 mins.\n"
    f"Reply *CANCEL* to stop this trade."
)
send_whatsapp(message)

# 6. Wait 5 minutes, checking every 30 seconds for cancellation
print(f"Waiting 5 mins before executing {action} {ticker}...")
for _ in range(10):  # 10 x 30s = 5 mins
    time.sleep(30)
    if pending_trade["cancelled"]:
        print("Trade was cancelled — skipping execution")
        return

# 7. Execute if not cancelled
if pending_trade["active"]:
    pending_trade["active"] = False
    print(f"Executing trade: {action} {ticker} £{amount}")
    success = execute_trade(ticker, action, amount)

    if success:
        send_whatsapp(
            f"✅ Trade executed: {action} £{amount:.0f} of {ticker}.\n"
            f"Check Trading 212 for confirmation."
        )
    else:
        send_whatsapp(
            f"❌ Trade FAILED: {action} {ticker}. "
            f"Check Railway logs and your T212 account."
        )
```

# ── Scheduler ─────────────────────────────────────────────────────────────────

def start_scheduler():
schedule.every().day.at(“07:00”).do(run_trading_check)
schedule.every().day.at(“09:30”).do(run_trading_check)
schedule.every().day.at(“12:00”).do(run_trading_check)
schedule.every().day.at(“15:30”).do(run_trading_check)
schedule.every().day.at(“18:00”).do(run_trading_check)
schedule.every().day.at(“21:00”).do(run_trading_check)

```
print("Scheduler started: 07:00, 09:30, 12:00, 15:30, 18:00, 21:00 UTC")
print("\nRunning initial check on startup...")
run_trading_check()

while True:
    schedule.run_pending()
    time.sleep(60)
```

# ── Entry Point ────────────────────────────────────────────────────────────────

if **name** == “**main**”:
required_vars = [
“T212_API_KEY”, “T212_API_SECRET”, “ANTHROPIC_API_KEY”,
“TWILIO_ACCOUNT_SID”, “TWILIO_AUTH_TOKEN”,
“TWILIO_WHATSAPP_FROM”, “TWILIO_WHATSAPP_TO”
]

```
missing = [v for v in required_vars if not os.environ.get(v)]
if missing:
    print(f"❌ Missing env vars: {', '.join(missing)}")
    exit(1)

# Start web server in background thread (listens for CANCEL)
print("Starting web server on port 8080...")
web_thread = threading.Thread(target=start_web_server, daemon=True)
web_thread.start()

# Start scheduler on main thread
start_scheduler()
```
