# t212-analyst

A 24/7 human-in-the-loop market analysis and trade assistant for **Trading 212** (`demo` and `live` Invest / Stocks ISA accounts).

> **Compliance & Safety First:** `monitor.py` is strictly read-only on Trading 212 and **never** places orders automatically. Every trade idea is saved as a `PENDING` proposal in SQLite and requires your explicit approval (via `executor.py` CLI or Telegram `/approve <id>`) before any order is submitted.

---

## How It Works Day-to-Day

1. **Always-On Services (Oracle Cloud VM / Linux VPS):**
   - `t212-monitor.service` — wakes up every **15 minutes** during US market hours (`09:30–16:00 ET`, Mon–Fri).
   - `t212-telegram-bot.service` — stays connected to Telegram 24/7, listening **only** to your authorized `TELEGRAM_CHAT_ID`.
2. **Every 15 Minutes (Read-Only):**
   - Fetches your open Trading 212 positions, average prices, P&L, and free cash (rate-limited: max `1 req/s` positions, `1 req/5s` cash).
   - Pulls 1-year daily prices & Yahoo Finance RSS news for your held tickers + `watchlist.json` (100% pure-Python HTTP, no `pandas`/`numpy` DLLs required).
   - Computes technical indicators: **RSI-14, SMA-20/50/200, 52-week high/low, 5d/20d momentum, volume ratio (vs 20d avg), and 20d annualized volatility**.
   - Saves snapshots to `data/t212_analyst.db`.
3. **Cost-Controlled AI Analysis (`analyst.py`):**
   - Only calls the LLM (Groq, Gemini, or any OpenAI-compatible API) when at least one trigger fires:
     - Price move `>= ±threshold%` (default `±2%`, customizable per ticker in `watchlist.json`)
     - Major news catalyst keyword in headlines
     - **RSI-14** extreme (`<= 30` oversold or `>= 70` overbought) or volume spike (`>= 2.0x` 20d avg)
     - Daily EOD summary schedule (`16:15 ET`)
   - Fetches macro regime context (**VIX**, **US 10Y Yield**, **DXY**, **GBP/USD**, **Gold**, **WTI Crude Oil**) and sends a structured briefing to Telegram.
4. **Human-in-the-Loop Execution (`telegram_bot.py` & `executor.py`):**
   - Actionable `BUY`/`SELL` ideas are saved as `PENDING` with a unique `idempotency_key`.
   - From Telegram on your phone:
     - `/list` — view `PENDING` proposals
     - `/approve <id>` — atomically locks the proposal, checks open orders for duplicates, and submits `/equity/orders/market` or `/equity/orders/limit` (using negative quantities for `SELL` orders)
     - `/reject <id>` — dismisses a proposal
     - `/status` — shows recent orders

---

## Project Structure

```text
t212-analyst/
├── .env.example                    # Environment variables template
├── .gitignore
├── requirements.txt                # Pure-Python dependencies
├── watchlist.json                  # Customizable watchlist & T212<->Yahoo ticker mapping
├── config.py                       # Env/config loader & validation
├── t212_client.py                  # Trading 212 API v0 client (Basic Auth + rate limits)
├── market_data.py                  # Pure-Python Yahoo chart/RSS, technicals & macro context
├── analyst.py                      # Threshold gate + LLM briefing & trade idea generator
├── notifier.py                     # Console, Discord, and Telegram notifications
├── storage.py                      # SQLite persistence + atomic idempotency locks
├── monitor.py                      # Continuous monitor daemon (--once supported)
├── executor.py                     # CLI human-in-the-loop approval & order execution
├── telegram_bot.py                 # 24/7 Telegram bot for phone /approve & /reject
├── deploy/
│   ├── t212-monitor.service        # systemd unit for monitor.py
│   ├── t212-telegram-bot.service   # systemd unit for telegram_bot.py
│   ├── crontab.example             # Linux cron alternative
│   └── Register-T212MonitorTask.ps1# Windows Task Scheduler helper
└── tests/
    └── test_t212_analyst.py        # Unit tests (mocked API)
```

---

## 1. Quickstart & Testing in `"demo"` Mode

### Step 1: Generate a Demo API Key in Trading 212
1. Log into the **Trading 212 Web App** and switch to your **Practice (Demo)** account.
2. Go to **Settings -> API** and generate a new API key pair.
3. Copy both the **API Key** and **API Secret** immediately (the secret is only shown once).

### Step 2: Install Dependencies & Configure `.env`

**Linux / Oracle VM:**
```bash
cd /opt/t212-analyst
python3 -m venv .venv
.venv/bin/pip install --upgrade pip setuptools wheel
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

**Windows (Git Bash):**
```bash
cd t212-analyst
python -m venv .venv
source .venv/Scripts/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:
```dotenv
T212_API_KEY=your_demo_api_key
T212_API_SECRET=your_demo_api_secret
T212_ENV=demo

# Free-tier Groq example (or OpenAI / Gemini / OpenRouter):
LLM_API_KEY=your_groq_api_key
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=llama-3.3-70b-versatile

# Optional Telegram alerts & phone approvals:
TELEGRAM_BOT_TOKEN=your_botfather_token
TELEGRAM_CHAT_ID=your_numeric_chat_id
```

### Step 3: Run a Single Demo Cycle
```bash
.venv/bin/python monitor.py --once --ignore-market-hours --force-analysis
```

### Step 4: Review & Approve Proposals
**Via CLI:**
```bash
python executor.py list
python executor.py interactive
python executor.py approve 1
```
**Via Telegram (Phone):**
Run `python telegram_bot.py` and message your bot `/list`, `/approve <id>`, `/reject <id>`, or `/status`.

---

## 2. 24/7 Deployment on a Linux VM (e.g. Oracle Cloud Always Free)

```bash
cd /opt/t212-analyst
sed -i "s/YOUR_LINUX_USER/$USER/g" deploy/t212-monitor.service deploy/t212-telegram-bot.service

sudo cp deploy/t212-monitor.service /etc/systemd/system/
sudo cp deploy/t212-telegram-bot.service /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now t212-monitor t212-telegram-bot
sudo systemctl status t212-monitor t212-telegram-bot
```

View live logs anytime:
```bash
sudo journalctl -u t212-monitor -u t212-telegram-bot -f
```

When you are ready to switch from `demo` to `live`, update `T212_API_KEY`, `T212_API_SECRET`, and `T212_ENV=live` in `/opt/t212-analyst/.env` and run:
```bash
sudo systemctl restart t212-monitor t212-telegram-bot
```
