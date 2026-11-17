# t212-analyst

A human-in-the-loop market analysis and trade assistant for **Trading 212** (`demo` and `live` Invest / Stocks ISA accounts).

> **Compliance & Safety First:** `monitor.py` is strictly read-only on Trading 212 and **never** places orders automatically. Every trade idea is saved as a `PENDING` proposal in SQLite and requires your explicit approval in `executor.py` before any order is submitted.

---

## Project Structure

```text
t212-analyst/
├── .env.example          # Environment variables template
├── .gitignore
├── requirements.txt      # Python dependencies
├── watchlist.json        # Customizable watchlist & T212<->Yahoo ticker mapping
├── config.py             # Env/config loader & validation
├── t212_client.py        # Trading 212 API v0 client (Basic Auth + rate limits)
├── market_data.py        # External quotes & news via yfinance + RSS
├── analyst.py            # Threshold gate + LLM briefing & trade idea generator
├── notifier.py           # Console, Discord, and Telegram notifications
├── storage.py            # SQLite persistence + atomic idempotency locks
├── monitor.py            # Continuous monitor daemon (--once supported)
├── executor.py           # Human-in-the-loop approval & order execution CLI
├── deploy/
│   ├── t212-monitor.service
│   └── crontab.example
└── tests/
    └── test_t212_analyst.py
```

---

## 1. Quickstart: Testing in `"demo"` Mode First

### Step 1: Create a Demo API Key in Trading 212
1. Log into the **Trading 212 Web App** and switch to your **Practice (Demo)** account.
2. Go to **Settings -> API** and generate a new API key pair.
3. Copy both the **API Key** and **API Secret** immediately (the secret is only shown once).

### Step 2: Install Dependencies & Configure `.env`

**Windows (PowerShell):**
```powershell
cd t212-analyst
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item .env.example .env
```

**Linux / macOS:**
```bash
cd t212-analyst
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
```

Edit `.env` and set at minimum:
```dotenv
T212_API_KEY=your_demo_api_key
T212_API_SECRET=your_demo_api_secret
T212_ENV=demo
```
*(Optional: add `LLM_API_KEY` for AI-powered briefings; if left blank, `analyst.py` runs in deterministic fallback mode so you can still test the full pipeline.)*

### Step 3: Run a Single Monitor Cycle in Demo Mode
Run one cycle ignoring market hours and forcing an analysis briefing so you can verify connectivity, SQLite storage, and notifications immediately:

```powershell
python monitor.py --once --ignore-market-hours --force-analysis
```

You should see:
- Your demo open positions and free cash logged and saved to `data\t212_analyst.db`.
- External quotes & news pulled for your held tickers + `watchlist.json`.
- A formatted briefing printed to the console (and sent to Discord/Telegram if configured).

### Step 4: Review & Approve/Reject Proposals
List any generated proposals:
```powershell
python executor.py list
```

Launch the interactive approval shell:
```powershell
python executor.py interactive
```
Or approve/reject a specific proposal ID directly:
```powershell
python executor.py approve 1
python executor.py reject 2
```

When approving, `executor.py`:
1. Requires you to type `APPROVE <id>` (unless `--yes` is passed).
2. Atomically locks the proposal (`PENDING` -> `APPROVED_IN_FLIGHT`) in SQLite.
3. Checks `/equity/orders` for duplicate open orders.
4. Submits `/equity/orders/market` or `/equity/orders/limit` (using **negative quantity** for `SELL` orders).
5. Never auto-retries a timed-out order POST, preventing duplicate submissions.

---

## 2. API Compliance & Safeguards

- **No unattended trading:** `monitor.py` has no order-execution code path.
- **Rate limits enforced in `t212_client.py`:**
  - `GET /equity/positions`: max 1 request / 1 second
  - `GET /equity/account/cash`: max 1 request / 5 seconds
  - `GET /equity/orders`: max 1 request / 5 seconds
  - `429` responses back off automatically on `GET` requests.
- **External data offloading:** Intraday quotes and news are pulled from `yfinance` and Yahoo Finance RSS rather than polling Trading 212.
- **Cost control:** `analyst.py` only calls the LLM when a price move exceeds `PRICE_MOVE_THRESHOLD_PCT`, a news catalyst is detected, or the daily summary schedule is reached.

---

## 3. Deploying Continuously in the Background

### Windows: Task Scheduler (Recommended on Windows)
Run the included PowerShell helper to register a scheduled task that runs `monitor.py --once` every 15 minutes (automatically respecting `MARKET_HOURS_ONLY=true`):
```powershell
cd t212-analyst
powershell -ExecutionPolicy Bypass -File .\deploy\Register-T212MonitorTask.ps1
```
To run continuously in a terminal window instead:
```powershell
.\.venv\Scripts\python.exe monitor.py
```

### Linux / VPS: `systemd`
1. Copy the project to `/opt/t212-analyst` (or update paths in `deploy/t212-monitor.service`).
2. Update `User=` and `Group=` in `deploy/t212-monitor.service`.
3. Install and start the service:
   ```bash
   sudo cp deploy/t212-monitor.service /etc/systemd/system/t212-monitor.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now t212-monitor
   sudo journalctl -u t212-monitor -f
   ```

### Linux / VPS: `cron`
Use `deploy/crontab.example` to run `monitor.py --once` every 15 minutes on weekdays:
```bash
crontab -e
```
Paste the line from `deploy/crontab.example` adjusted to your installation path.
