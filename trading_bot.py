"""
Trading Brain Bot
Runs 6x daily, fetches T212 portfolio, calls Claude for analysis,
sends full recommendation to WhatsApp via Twilio (under 1500 chars).
"""

import os
import requests
import schedule
import time
from datetime import datetime
from anthropic import Anthropic

# ── Configuration ─────────────────────────────────────────────────────────────

T212_API_KEY        = os.environ.get("T212_API_KEY")
T212_API_SECRET     = os.environ.get("T212_API_SECRET")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY")
TWILIO_ACCOUNT_SID  = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM         = os.environ.get("TWILIO_WHATSAPP_FROM")
TWILIO_TO           = os.environ.get("TWILIO_WHATSAPP_TO")

T212_ENV = os.environ.get("T212_ENV", "demo")

# ── Trading Brain System Prompt ────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are an aggressive trading analyst managing a £100–£500 portfolio on Trading 212.
Your job is to beat the market. Be concise — your entire response must stay under 1400 characters.

RULES:
1. Max 40% of cash in one trade
2. Only enter if you can state the catalyst, why NOW, and target exit
3. Take 50% profit at +15–25%, let rest run
4. Cut a position if it won't recover to break-even within one week
5. Never revenge trade — no re-entering a stock right after a loss
6. Hold cash is a valid call — don't force trades

RESPOND IN THIS FORMAT ONLY — keep each line brief:

🌍 MARKET: [one sentence on mood]
🔍 SCAN: [one sentence on best opportunity or why nothing stands out]
⚡ ACTION: BUY / SELL / HOLD CASH
📈 STOCK: [Ticker] (or N/A)
💰 SIZE: £[amount] (or N/A)
🎯 TARGET: £[price] (or N/A)
✂️ STOP: [plain English, max 10 words]
🔥 CONVICTION: [X/10]
📋 PORTFOLIO: [one sentence on existing holdings]
⚠️ RISK: [one sentence — what could go wrong]
"""

# ── T212 API ───────────────────────────────────────────────────────────────────

def get_t212_portfolio():
    base_url = f"https://{T212_ENV}.trading212.com/api/v0"

    import base64
    credentials = base64.b64encode(
        f"{T212_API_KEY}:{T212_API_SECRET}".encode()
    ).decode()

    headers = {"Authorization": f"Basic {credentials}"}

    try:
        summary_resp = requests.get(
            f"{base_url}/equity/account/summary",
            headers=headers,
            timeout=10
        )
        summary_resp.raise_for_status()
        summary = summary_resp.json()

        positions_resp = requests.get(
            f"{base_url}/equity/portfolio",
            headers=headers,
            timeout=10
        )
        positions_resp.raise_for_status()
        positions = positions_resp.json()

        return summary, positions

    except requests.RequestException as e:
        print(f"T212 API error: {e}")
        return None, None


def format_portfolio_context(summary, positions):
    if not summary:
        return "Portfolio data unavailable — T212 API error."

    cash = summary.get("cash", {})
    investments = summary.get("investments", {})

    available_cash = cash.get("availableToTrade", 0)
    total_value = summary.get("totalValue", 0)
    unrealised_pnl = investments.get("unrealizedProfitLoss", 0)

    portfolio_text = (
        f"Portfolio: £{total_value:.2f} total | "
        f"£{available_cash:.2f} cash | "
        f"P&L: £{unrealised_pnl:.2f}\n"
        f"Positions:\n"
    )

    if positions:
        for pos in positions:
            ticker = pos.get("ticker", "?")
            avg_price = pos.get("averagePrice", 0)
            current_price = pos.get("currentPrice", 0)
            pnl = pos.get("ppl", 0)
            pnl_pct = ((current_price - avg_price) / avg_price * 100) if avg_price else 0
            portfolio_text += (
                f"• {ticker}: avg £{avg_price:.2f} | "
                f"now £{current_price:.2f} | "
                f"P&L £{pnl:.2f} ({pnl_pct:+.1f}%)\n"
            )
    else:
        portfolio_text += "• No open positions\n"

    return portfolio_text


# ── Claude API ─────────────────────────────────────────────────────────────────

def get_trade_recommendation(portfolio_context):
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    user_message = (
        f"{portfolio_context}\n"
        f"Date/Time: {datetime.now().strftime('%d %b %Y, %H:%M')}\n\n"
        f"Scan the market now. Find the best opportunity or confirm hold cash. "
        f"Follow your rules. Keep response under 1400 characters."
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            system=SYSTEM_PROMPT,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search"
            }],
            messages=[{"role": "user", "content": user_message}]
        )

        full_response = " ".join(
            block.text for block in response.content
            if hasattr(block, "text") and block.text is not None
        )
        return full_response.strip()

    except Exception as e:
        print(f"Claude API error: {e}")
        return None


# ── Twilio WhatsApp ────────────────────────────────────────────────────────────

def send_whatsapp(message):
    from twilio.rest import Client

    # Hard cap at 1500 chars just in case
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
    print(f"\n{'='*50}")
    print(f"Trading check at {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*50}")

    print("Fetching T212 portfolio...")
    summary, positions = get_t212_portfolio()
    portfolio_context = format_portfolio_context(summary, positions)
    print(portfolio_context)

    print("Calling Claude for analysis...")
    analysis = get_trade_recommendation(portfolio_context)

    if analysis:
        print(f"Analysis received ({len(analysis)} chars)")
        print(analysis)
    else:
        analysis = "⚠️ Analysis unavailable — Claude API error. Check Railway logs."

    timestamp = datetime.now().strftime("%d %b, %H:%M")
    message = f"🤖 TRADING BRAIN — {timestamp}\n\n{analysis}"

    print("Sending WhatsApp...")
    send_whatsapp(message)
    print("✅ Job complete\n")


# ── Scheduler ─────────────────────────────────────────────────────────────────

def start_scheduler():
    schedule.every().day.at("07:00").do(run_trading_check)
    schedule.every().day.at("09:30").do(run_trading_check)
    schedule.every().day.at("12:00").do(run_trading_check)
    schedule.every().day.at("15:30").do(run_trading_check)
    schedule.every().day.at("18:00").do(run_trading_check)
    schedule.every().day.at("21:00").do(run_trading_check)

    print("Trading Brain scheduler started.")
    print("Checks: 07:00, 09:30, 12:00, 15:30, 18:00, 21:00 UTC")
    print("\nRunning initial check on startup...")
    run_trading_check()

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    required_vars = [
        "T212_API_KEY", "T212_API_SECRET", "ANTHROPIC_API_KEY",
        "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
        "TWILIO_WHATSAPP_FROM", "TWILIO_WHATSAPP_TO"
    ]

    missing = [v for v in required_vars if not os.environ.get(v)]

    if missing:
        print(f"❌ Missing environment variables: {', '.join(missing)}")
        exit(1)

    start_scheduler()
