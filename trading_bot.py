"""
Trading Brain Bot
Runs 6x daily, fetches T212 portfolio, calls Claude for analysis,
sends full recommendation to WhatsApp via Twilio.
"""

import os
import requests
import schedule
import time
from datetime import datetime
from anthropic import Anthropic

# ── Configuration ─────────────────────────────────────────────────────────────
# All sensitive values come from environment variables (set these in Railway)

T212_API_KEY        = os.environ.get("T212_API_KEY")
T212_API_SECRET     = os.environ.get("T212_API_SECRET")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY")
TWILIO_ACCOUNT_SID  = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM         = os.environ.get("TWILIO_WHATSAPP_FROM")   # e.g. whatsapp:+14155238886
TWILIO_TO           = os.environ.get("TWILIO_WHATSAPP_TO")     # e.g. whatsapp:+447700000000

# Use demo (paper trading) or live — change to "live" when ready
T212_ENV = os.environ.get("T212_ENV", "demo")  # "demo" or "live"

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
    """Fetch current portfolio and cash from Trading 212."""
    base_url = f"https://{T212_ENV}.trading212.com/api/v0"
    
    import base64
    credentials = base64.b64encode(
        f"{T212_API_KEY}:{T212_API_SECRET}".encode()
    ).decode()
    
    headers = {"Authorization": f"Basic {credentials}"}
    
    try:
        # Get account summary
        summary_resp = requests.get(
            f"{base_url}/equity/account/summary", 
            headers=headers, 
            timeout=10
        )
        summary_resp.raise_for_status()
        summary = summary_resp.json()
        
        # Get open positions
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
    """Convert T212 data into readable context for Claude."""
    if not summary:
        return "Portfolio data unavailable — T212 API error."
    
    cash = summary.get("cash", {})
    investments = summary.get("investments", {})
    
    available_cash = cash.get("availableToTrade", 0)
    total_value = summary.get("totalValue", 0)
    unrealised_pnl = investments.get("unrealizedProfitLoss", 0)
    
    portfolio_text = f"""
CURRENT PORTFOLIO SNAPSHOT ({datetime.now().strftime('%d %b %Y, %H:%M')})
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
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
            pnl = pos.get("ppl", 0)  # profit/loss
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
    """Call Claude with full trading brain context and get recommendation."""
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
        
        # Extract the text response (may include tool use blocks)
        full_response = " ".join(
            block.text for block in response.content 
            if hasattr(block, "text")
        )
        return full_response
        
    except Exception as e:
        print(f"Claude API error: {e}")
        return f"Analysis unavailable — Claude API error: {str(e)}"


# ── Twilio WhatsApp ────────────────────────────────────────────────────────────

def send_whatsapp(message):
    """Send message via Twilio WhatsApp."""
    from twilio.rest import Client
    
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        
        msg = client.messages.create(
            from_=TWILIO_FROM,
            to=TWILIO_TO,
            body=message
        )
        print(f"WhatsApp sent: {msg.sid}")
        return True
        
    except Exception as e:
        print(f"Twilio error: {e}")
        return False


# ── Main Job ───────────────────────────────────────────────────────────────────

def run_trading_check():
    """The full pipeline: fetch → analyse → send."""
    print(f"\n{'='*50}")
    print(f"Trading check running at {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*50}")
    
    # 1. Fetch portfolio
    print("Fetching T212 portfolio...")
    summary, positions = get_t212_portfolio()
    portfolio_context = format_portfolio_context(summary, positions)
    print(portfolio_context)
    
    # 2. Get Claude's recommendation
    print("Calling Claude for analysis...")
    recommendation = get_trade_recommendation(portfolio_context)
    print(f"Recommendation received ({len(recommendation)} chars)")
    
    # 3. Build WhatsApp message
    timestamp = datetime.now().strftime("%d %b, %H:%M")
    whatsapp_message = f"🤖 TRADING BRAIN — {timestamp}\n\n{recommendation}"
    
    # WhatsApp has a 1600 char limit — truncate if needed
    if len(whatsapp_message) > 1580:
        whatsapp_message = whatsapp_message[:1577] + "..."
    
    # 4. Send to WhatsApp
    print("Sending WhatsApp message...")
    success = send_whatsapp(whatsapp_message)
    
    if success:
        print("✅ Job complete")
    else:
        print("❌ WhatsApp send failed")


# ── Scheduler ─────────────────────────────────────────────────────────────────
# 6 checks spread across the day (all times UTC, which is UK time in winter;
# adjust by +1 hour for BST in summer)

def start_scheduler():
    schedule.every().day.at("07:00").do(run_trading_check)   # Pre-market
    schedule.every().day.at("09:30").do(run_trading_check)   # UK market open
    schedule.every().day.at("12:00").do(run_trading_check)   # Midday
    schedule.every().day.at("15:30").do(run_trading_check)   # US market open
    schedule.every().day.at("18:00").do(run_trading_check)   # Late afternoon
    schedule.every().day.at("21:00").do(run_trading_check)   # US mid-session
    
    print("Trading Brain scheduler started.")
    print("Scheduled checks: 07:00, 09:30, 12:00, 15:30, 18:00, 21:00 UTC")
    print("Waiting for next scheduled run...\n")
    
    # Run once immediately on startup so you can verify it works
    print("Running initial check on startup...")
    run_trading_check()
    
    while True:
        schedule.run_pending()
        time.sleep(60)  # Check every minute


if __name__ == "__main__":
    # Validate all environment variables are set
    required_vars = [
        "T212_API_KEY", "T212_API_SECRET", "ANTHROPIC_API_KEY",
        "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
        "TWILIO_WHATSAPP_FROM", "TWILIO_WHATSAPP_TO"
    ]
    
    missing = [v for v in required_vars if not os.environ.get(v)]
    
    if missing:
        print(f"❌ Missing environment variables: {', '.join(missing)}")
        print("Set these in Railway before deploying.")
        exit(1)
    
    start_scheduler()
