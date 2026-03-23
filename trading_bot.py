"""
Trading Brain Bot
Runs 6x daily, fetches T212 portfolio, calls Claude for analysis,
sends short summary to WhatsApp and full analysis to email.
"""

import os
import requests
import schedule
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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
GMAIL_ADDRESS       = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD  = os.environ.get("GMAIL_APP_PASSWORD")

T212_ENV = os.environ.get("T212_ENV", "demo")

# ── Trading Brain System Prompt ────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are an aggressive, market-aware trading analyst managing a small test portfolio 
of £100–£500 on Trading 212. Your mandate is to beat the market actively.

RULES YOU MUST FOLLOW:
1. POSITION SIZING: Never recommend deploying more than 40% of available cash in 
   a single trade.
2. ENTRY: Only recommend a trade if you can clearly state (a) why this stock is 
   moving NOW, (b) what the catalyst is, (c) what the target exit price is.
3. PROFIT EXIT: Take at least 50% profit when a position is up 15–25%. Let the 
   rest run if momentum continues.
4. LOSS EXIT: Recommend cutting a position if, based on available information, 
   the stock will NOT recover to break-even within one week. Ask: has the thesis 
   broken down? Is the catalyst gone? Is momentum against us?
5. NO REVENGE TRADING: Never recommend re-entering a stock immediately after a loss.
6. FREQUENCY: Only recommend a trade when something genuinely interesting is 
   happening. Recommending "hold cash" is a valid and often correct call.

OUTPUT FORMAT — always structure your response exactly like this:

📊 MARKET MOOD
[2-3 sentences on today's overall market environment]

🔍 OPPORTUNITY SCAN
[What you found and why it's interesting, or why nothing stood out]

⚡ RECOMMENDATION
Action: BUY / SELL / HOLD CASH
Stock: [Ticker — Full Name] (or N/A if holding cash)
Position Size: £[amount] (or N/A)
Entry Rationale: [clear reasoning]
Target Exit: £[price] (or N/A)
Stop Condition: [plain English — when to cut]
Conviction: [X/10]

📋 PORTFOLIO NOTE
[Any comment on existing holdings — should anything be sold or adjusted?]

⚠️ RISK REMINDER
[One honest sentence about what could go wrong with this call]
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

    portfolio_text = f"""
CURRENT PORTFOLIO SNAPSHOT ({datetime.now().strftime('%d %b %Y, %H:%M')})
Total Account Value: £{total_value:.2f}
Cash Available to Trade: £{available_cash:.2f}
Unrealised P&L: £{unrealised_pnl:.2f}

OPEN POSITIONS:
"""

    if positions:
        for pos in positions:
            ticker = pos.get("ticker", "Unknown")
            quantity = pos.get("quantity", 0)
            avg_price = pos.get("averagePrice", 0)
            current_price = pos.get("currentPrice", 0)
            pnl = pos.get("ppl", 0)
            pnl_pct = ((current_price - avg_price) / avg_price * 100) if avg_price else 0

            portfolio_text += (
                f"• {ticker}: {quantity:.4f} shares | "
                f"Avg buy: £{avg_price:.2f} | "
                f"Current: £{current_price:.2f} | "
                f"P&L: £{pnl:.2f} ({pnl_pct:+.1f}%)\n"
            )
    else:
        portfolio_text += "• No open positions\n"

    return portfolio_text


# ── Claude API ─────────────────────────────────────────────────────────────────

def get_trade_recommendation(portfolio_context):
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    user_message = f"""
{portfolio_context}

Please scan the current market right now, find the best opportunity (or confirm 
holding cash is correct), and give me a full trade recommendation following your rules.

Search for: current market conditions, any major news today, any stocks with 
unusual momentum or catalysts in the last 24 hours.
"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
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
        return full_response

    except Exception as e:
        print(f"Claude API error: {e}")
        return None


def extract_whatsapp_summary(full_analysis):
    if not full_analysis:
        return "⚠️ Trading Brain: Analysis failed — check your email."

    lines = full_analysis.split("\n")
    action_line = ""
    stock_line = ""
    conviction_line = ""

    for line in lines:
        if line.strip().startswith("Action:"):
            action_line = line.strip()
        if line.strip().startswith("Stock:"):
            stock_line = line.strip()
        if line.strip().startswith("Conviction:"):
            conviction_line = line.strip()

    timestamp = datetime.now().strftime("%d %b, %H:%M")
    summary = f"🤖 TRADING BRAIN — {timestamp}\n\n"

    if action_line:
        summary += f"{action_line}\n{stock_line}\n{conviction_line}\n"
        summary += "\nFull analysis in your email 📧"
    else:
        summary += "Analysis complete — full breakdown in your email 📧"

    return summary


# ── Email ──────────────────────────────────────────────────────────────────────

def send_email(subject, full_analysis):
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = GMAIL_ADDRESS

        text_part = MIMEText(full_analysis, "plain")

        html_content = f"""
        <html><body style="font-family: monospace; padding: 20px; background: #f9f9f9;">
        <div style="max-width: 600px; background: white; padding: 24px;
        border-radius: 8px; border-left: 4px solid #00c896;">
        <h2 style="color: #00c896; margin-top: 0;">🤖 Trading Brain</h2>
        <pre style="white-space: pre-wrap; font-size: 14px; line-height: 1.6;">{full_analysis}</pre>
        <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
        <p style="color: #999; font-size: 12px;">
        Automated analysis only — not financial advice. You decide whether to act.
        </p>
        </div></body></html>
        """

        html_part = MIMEText(html_content, "html")
        msg.attach(text_part)
        msg.attach(html_part)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, GMAIL_ADDRESS, msg.as_string())

        print("✅ Email sent")
        return True

    except Exception as e:
        print(f"Email error: {e}")
        return False


# ── Twilio WhatsApp ────────────────────────────────────────────────────────────

def send_whatsapp(message):
    from twilio.rest import Client

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
    full_analysis = get_trade_recommendation(portfolio_context)

    if full_analysis:
        print(f"Analysis received ({len(full_analysis)} chars)")
    else:
        print("Analysis failed")

    whatsapp_summary = extract_whatsapp_summary(full_analysis)
    print("Sending WhatsApp summary...")
    send_whatsapp(whatsapp_summary)

    timestamp = datetime.now().strftime("%d %b %Y, %H:%M")
    subject = f"Trading Brain — {timestamp}"
    email_body = full_analysis if full_analysis else (
        "Analysis unavailable — Claude API error. Check Railway logs."
    )
    print("Sending full analysis email...")
    send_email(subject, email_body)

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
        "TWILIO_WHATSAPP_FROM", "TWILIO_WHATSAPP_TO",
        "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"
    ]

    missing = [v for v in required_vars if not os.environ.get(v)]

    if missing:
        print(f"❌ Missing environment variables: {', '.join(missing)}")
        exit(1)

    start_scheduler()
