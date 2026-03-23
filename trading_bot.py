"""
Trading Brain Bot - Full Live Trading Version
- Runs 6x daily
- Fetches live T212 portfolio
- Calls Claude for analysis
- Sends WhatsApp with 5 min cancel window
- Executes trade automatically if not cancelled
"""

import os
import requests
import schedule
import time
import base64
import threading
import json
from datetime import datetime
from anthropic import Anthropic
from flask import Flask, request

# ── Configuration ─────────────────────────────────────────────────────────────

T212_API_KEY       = os.environ.get("T212_API_KEY")
T212_API_SECRET    = os.environ.get("T212_API_SECRET")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM        = os.environ.get("TWILIO_WHATSAPP_FROM")
TWILIO_TO          = os.environ.get("TWILIO_WHATSAPP_TO")

T212_ENV           = os.environ.get("T212_ENV", "live")
MAX_TRADE_AMOUNT   = 100  # Hard cap £100 per trade
CANCEL_WINDOW_SECS = 60  # 1 minute

# ── State ─────────────────────────────────────────────────────────────────────
# Stores the pending trade while we wait for possible cancellation

pending_trade = {
    "active": False,
    "ticker": None,
    "action": None,   # "BUY" or "SELL"
    "amount": None,
    "cancelled": False
}

# ── Trade Journal ──────────────────────────────────────────────────────────────

JOURNAL_FILE = "/app/trade_journal.json"

def load_journal():
    """Load existing journal or create empty one."""
    try:
        with open(JOURNAL_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"trades": [], "checks": []}

def save_journal(journal):
    """Save journal to disk."""
    try:
        with open(JOURNAL_FILE, "w") as f:
            json.dump(journal, f, indent=2)
    except Exception as e:
        print(f"Journal save error: {e}")

def log_check(analysis, action, ticker, amount):
    """Log every analysis check regardless of whether a trade was made."""
    journal = load_journal()
    entry = {
        "timestamp": datetime.now().isoformat(),
        "action":    action or "UNKNOWN",
        "ticker":    ticker or "N/A",
        "amount":    amount or 0,
        "analysis":  analysis[:500] if analysis else "No analysis",
        "outcome":   "PENDING"  # updated later if trade executes
    }
    journal["checks"].append(entry)
    # Keep last 200 checks to avoid file growing too large
    journal["checks"] = journal["checks"][-200:]
    save_journal(journal)
    print(f"Journal: logged check — {action} {ticker}")
    return len(journal["checks"]) - 1  # return index for later update

def log_trade_outcome(check_index, outcome, exit_price=None, pnl=None):
    """Update a journal entry with the trade outcome."""
    journal = load_journal()
    if 0 <= check_index < len(journal["checks"]):
        journal["checks"][check_index]["outcome"]     = outcome
        journal["checks"][check_index]["exit_price"]  = exit_price
        journal["checks"][check_index]["pnl"]         = pnl
        journal["checks"][check_index]["updated_at"]  = datetime.now().isoformat()
        save_journal(journal)
        print(f"Journal: updated outcome — {outcome}")

def get_journal_summary():
    """Return a short summary of recent performance for Claude to reference."""
    journal = load_journal()
    checks  = journal.get("checks", [])

    if not checks:
        return "No trading history yet."

    total_checks  = len(checks)
    trades        = [c for c in checks if c["action"] in ("BUY", "SELL")]
    executed      = [t for t in trades if t["outcome"] == "EXECUTED"]
    cancelled     = [t for t in trades if t["outcome"] == "CANCELLED"]
    failed        = [t for t in trades if t["outcome"] == "FAILED"]
    holds         = [c for c in checks if c["action"] == "HOLD"]

    # Last 10 checks summary
    recent = checks[-10:]
    recent_summary = []
    for c in recent:
        ts     = c["timestamp"][:10]
        action = c["action"]
        ticker = c.get("ticker", "N/A")
        result = c.get("outcome", "PENDING")
        recent_summary.append(f"{ts}: {action} {ticker} → {result}")

    summary = (
        f"TRADING HISTORY SUMMARY\n"
        f"Total checks: {total_checks} | "
        f"Trades attempted: {len(trades)} | "
        f"Executed: {len(executed)} | "
        f"Cancelled: {len(cancelled)} | "
        f"Holds: {len(holds)}\n\n"
        f"LAST 10 CHECKS:\n" +
        "\n".join(recent_summary)
    )
    return summary



# ── Flask Web Server (listens for CANCEL replies) ─────────────────────────────

app = Flask(__name__)

@app.route("/cancel", methods=["POST"])
def handle_cancel():
    """Twilio calls this endpoint when you reply to the WhatsApp message."""
    incoming = request.form.get("Body", "").strip().upper()
    sender   = request.form.get("From", "")

    if sender == TWILIO_TO and incoming == "CANCEL":
        if pending_trade["active"]:
            pending_trade["cancelled"] = True
            pending_trade["active"]    = False
            print("🚫 Trade cancelled via WhatsApp")
            # Log cancellation in journal
            log_trade_outcome(
                pending_trade.get("check_index", -1),
                "CANCELLED"
            )
            send_whatsapp("🚫 Trade cancelled. No action taken.")
        else:
            send_whatsapp("No pending trade to cancel.")
    else:
        print(f"Ignored message: '{incoming}' from {sender}")

    # Must return a valid TwiML response (even if empty)
    return '<?xml version="1.0" encoding="UTF-8"?><Response></Response>', 200

def start_web_server():
    """Run Flask in a background thread so it doesn't block the scheduler."""
    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)

# ── T212 API ───────────────────────────────────────────────────────────────────

def t212_headers():
    credentials = base64.b64encode(
        f"{T212_API_KEY}:{T212_API_SECRET}".encode()
    ).decode()
    return {"Authorization": f"Basic {credentials}"}

def get_t212_portfolio():
    base_url = f"https://{T212_ENV}.trading212.com/api/v0"
    headers  = t212_headers()

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

def get_instrument_details(ticker):
    """Get the current price and fractional share info for a ticker."""
    base_url = f"https://{T212_ENV}.trading212.com/api/v0"
    headers  = t212_headers()

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

def get_current_price(ticker):
    """
    Fetch current price for a ticker.
    1. Check existing T212 portfolio positions first (fastest)
    2. Fall back to Yahoo Finance for stocks not yet held
    T212 ticker format is like AAPL_US_EQ — Yahoo uses AAPL
    """
    base_url = f"https://{T212_ENV}.trading212.com/api/v0"
    headers  = t212_headers()

    # Step 1 — check T212 portfolio for current price
    try:
        resp = requests.get(
            f"{base_url}/equity/portfolio",
            headers=headers, timeout=10
        )
        resp.raise_for_status()
        for pos in resp.json():
            if pos.get("ticker") == ticker:
                price = pos.get("currentPrice", 0)
                if price and price > 0:
                    print(f"Price from T212 portfolio: {ticker} = {price}")
                    return price
    except requests.RequestException as e:
        print(f"T212 portfolio price fetch error: {e}")

    # Step 2 — fall back to Yahoo Finance
    # Convert T212 format (XOM_US_EQ) to Yahoo format (XOM)
    yahoo_ticker = ticker.split("_")[0]

    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_ticker}"
        headers_yf = {
            "User-Agent": "Mozilla/5.0"
        }
        resp = requests.get(url, headers=headers_yf, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        price = (
            data["chart"]["result"][0]["meta"]["regularMarketPrice"]
        )
        if price and price > 0:
            print(f"Price from Yahoo Finance: {yahoo_ticker} = {price}")
            return price
    except Exception as e:
        print(f"Yahoo Finance price fetch error: {e}")

    print(f"Could not get price for {ticker} from any source")
    return None

def validate_ticker(ticker):
    """Check ticker exists in T212's instruments list. Return True/False."""
    base_url = f"https://{T212_ENV}.trading212.com/api/v0"
    headers  = t212_headers()
    try:
        resp = requests.get(
            f"{base_url}/equity/metadata/instruments",
            headers=headers, timeout=10
        )
        resp.raise_for_status()
        valid_tickers = [inst.get("ticker") for inst in resp.json()]
        if ticker in valid_tickers:
            print(f"✅ Ticker validated: {ticker}")
            return True
        else:
            # Try to find close matches to help with debugging
            base = ticker.split("_")[0]
            matches = [t for t in valid_tickers if t.startswith(base)]
            print(f"❌ Ticker not found: {ticker}")
            print(f"   Similar tickers in T212: {matches[:5]}")
            return False
    except requests.RequestException as e:
        print(f"Ticker validation error: {e}")
        return False


def execute_trade(ticker, action, amount_gbp):
    """Place a market order on Trading 212."""
    base_url = f"https://{T212_ENV}.trading212.com/api/v0"
    headers  = t212_headers()
    headers["Content-Type"] = "application/json"

    # Validate ticker exists in T212 before attempting order
    if not validate_ticker(ticker):
        send_whatsapp(
            f"⚠️ Trade aborted: {ticker} not found in Trading 212.\n"
            f"Claude may have used the wrong ticker format. No money moved."
        )
        return False

    try:
        if action == "BUY":
            # T212 only supports orders by QUANTITY, not value
            # So we fetch the current price and calculate shares to buy
            price = get_current_price(ticker)
            if not price or price <= 0:
                print(f"Could not get price for {ticker}")
                return False
            quantity = round(amount_gbp / price, 4)  # T212 max 4 decimal places
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

        # Log full response before raising so we can see T212's error message
        print(f"T212 response status: {resp.status_code}")
        print(f"T212 response body: {resp.text}")
        print(f"T212 request payload: {payload}")

        resp.raise_for_status()
        print(f"✅ Trade executed: {action} {ticker} £{amount_gbp}")
        return True

    except requests.RequestException as e:
        print(f"Trade execution error: {e}")
        # Also try to print response body if available
        try:
            print(f"T212 error detail: {e.response.text}")
        except Exception:
            pass
        return False

def format_portfolio_context(summary, positions):
    if not summary:
        return "Portfolio data unavailable."

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

# ── Claude API ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are an aggressive trading analyst managing a £100–£500 portfolio on Trading 212.
Your job is to beat the market. Be concise — entire response must stay under 1100 characters.

RULES:
1. Max £100 on a single trade
2. Only enter if you can state the catalyst, why NOW, and target exit
3. Take 50% profit at +15–25%, let rest run
4. Cut a position if it won't recover to break-even within one week
5. Never revenge trade
6. Hold cash is a valid call

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
"""

def get_trade_recommendation(portfolio_context):
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    # Include recent trading history so Claude can learn from past calls
    journal_summary = get_journal_summary()

    user_message = (
        f"{portfolio_context}\n"
        f"Date/Time: {datetime.now().strftime('%d %b %Y, %H:%M')}\n\n"
        f"RECENT TRADING HISTORY (learn from this):\n{journal_summary}\n\n"
        f"Scan the market now. Find the best opportunity or confirm hold cash. "
        f"Consider past decisions when making this recommendation. "
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

def parse_recommendation(analysis):
    """Extract action, ticker, and amount from Claude's response."""
    action = None
    ticker = None
    amount = None

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

# ── Twilio WhatsApp ────────────────────────────────────────────────────────────

def send_whatsapp(message):
    from twilio.rest import Client

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

# ── Main Job ───────────────────────────────────────────────────────────────────

def run_trading_check():
    global pending_trade

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

    # Log this check to the journal
    check_index = log_check(analysis, action, ticker, amount)

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
        "active":      True,
        "ticker":      ticker,
        "action":      action,
        "amount":      amount,
        "cancelled":   False,
        "check_index": check_index
    }

    timestamp = datetime.now().strftime("%d %b, %H:%M")
    message = (
        f"🤖 TRADING BRAIN — {timestamp}\n\n"
        f"{analysis}\n\n"
        f"{funding_line}\n\n"
        f"⏳ I will {action} £{amount:.0f} of {ticker} in 1 min.\n"
        f"Reply *CANCEL* to stop this trade."
    )
    send_whatsapp(message)

    # 6. Wait 5 minutes, checking every 30 seconds for cancellation
    print(f"Waiting 5 mins before executing {action} {ticker}...")
    for _ in range(2):  # 2 x 30s = 1 min
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
            log_trade_outcome(check_index, "EXECUTED")
            send_whatsapp(
                f"✅ Trade executed: {action} £{amount:.0f} of {ticker}.\n"
                f"Check Trading 212 for confirmation."
            )
        else:
            log_trade_outcome(check_index, "FAILED")
            send_whatsapp(
                f"❌ Trade FAILED: {action} {ticker}. "
                f"Check Railway logs and your T212 account."
            )

# ── Scheduler ─────────────────────────────────────────────────────────────────

def start_scheduler():
    schedule.every().day.at("07:00").do(run_trading_check)
    schedule.every().day.at("09:30").do(run_trading_check)
    schedule.every().day.at("12:00").do(run_trading_check)
    schedule.every().day.at("15:30").do(run_trading_check)
    schedule.every().day.at("18:00").do(run_trading_check)
    schedule.every().day.at("21:00").do(run_trading_check)

    print("Scheduler started: 07:00, 09:30, 12:00, 15:30, 18:00, 21:00 UTC")
    print("\nRunning initial check on startup...")
    run_trading_check()

    while True:
        schedule.run_pending()
        time.sleep(60)

# ── Entry Point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    required_vars = [
        "T212_API_KEY", "T212_API_SECRET", "ANTHROPIC_API_KEY",
        "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
        "TWILIO_WHATSAPP_FROM", "TWILIO_WHATSAPP_TO"
    ]

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
