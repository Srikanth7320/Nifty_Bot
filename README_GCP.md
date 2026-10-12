# All NSE EQ-Series Swing Paper-Trading Bot (1–5 Trading Sessions)

This application generates **paper-trading** entries/exits for short-term swing setups. It does not submit, modify, or cancel broker orders. Paper P&L is not real profit and this strategy has not been proven profitable.

## 1. Automatic data-provider selection

Provider selection happens when the app starts:

- **Empty `UPSTOX_ACCESS_TOKEN`** → use Yahoo Finance daily candles for indicators and Yahoo Finance intraday bars for a recent decision price.
- **Non-empty `UPSTOX_ACCESS_TOKEN`** → use Upstox historical candles and the Upstox market-quote endpoint.

When you receive your Upstox Analytics token, add it to `.env`:

```env
UPSTOX_ACCESS_TOKEN=paste_your_token_here
```

Then restart the app/service. You do not need to change Python code. Do not paste your real token into chat, Git, or a shared ZIP.

**Important Yahoo Finance limitation:** Yahoo Finance data is not an official exchange feed and may be delayed, unavailable, or incomplete. In Yahoo mode, the bot accepts an intraday bar only if its timestamp is no older than `YFINANCE_MAX_QUOTE_AGE_MINUTES`. If no sufficiently recent bar is available, it skips that symbol for buy/sell decisions instead of treating the previous daily close as current. The quote is the close of a recent intraday bar, not a guaranteed live tick.

If an Upstox token is configured but Upstox returns an error, the bot skips decisions for that symbol; it does not silently switch providers. To use Yahoo temporarily, empty the Upstox token and restart.

## 2. Configuration (`.env`)

Copy `.env.example` to `.env` and edit it. The `.env.example` template has empty API-token fields. Copy it to `.env` and add only the secrets you choose to use.

| Variable | Default | Meaning |
|---|---:|---|
| `MAX_SCAN_SYMBOLS` | `500` | Maximum number of symbols considered per scan from the full NSE EQ-series universe. Active positions are checked separately. |
| `MAX_BUYS_PER_SCAN` | `3` | Maximum new paper buys in one scan. |
| `MAX_OPEN_POSITIONS` | `10` | Maximum simultaneous open paper positions. |
| `SCAN_INTERVAL_MINUTES` | `15` | Scheduler interval during market hours. |
| `MIN_STOCK_PRICE` | `50` | Minimum stock price in INR. |
| `MAX_STOCK_PRICE` | `5000` | Maximum stock price in INR. |
| `MIN_AVG_DAILY_TURNOVER_INR` | `20000000` | Minimum average daily traded value over the prior 20 completed sessions (₹2 crore). |
| `MIN_AVG_DAILY_VOLUME` | `10000` | Minimum average shares traded per day over the prior 20 completed sessions. |
| `COOLING_PERIOD_HOURS` | `24` | Hours a sold symbol is blocked from re-entry. |
| `YFINANCE_QUOTE_INTERVAL` | `5m` | Yahoo intraday bar interval when no Upstox token is configured. |
| `YFINANCE_MAX_QUOTE_AGE_MINUTES` | `30` | Maximum age of the latest Yahoo bar for trade decisions. |
| `RSI_BUY_THRESHOLD` | `50` | Lower bound of the RSI entry range. |
| `RSI_MAX_BUY_THRESHOLD` | `68` | Upper bound of the RSI entry range. |
| `RSI_EXIT_THRESHOLD` | `45` | RSI exit threshold. |
| `UPSTOX_ACCESS_TOKEN` | empty | Empty selects Yahoo; non-empty selects Upstox. |

`MAX_SCAN_SYMBOLS` controls how many stocks are scanned, not how many buy signals are sent. The bot may buy fewer than `MAX_BUYS_PER_SCAN`, including zero. It checks active positions separately so their exits are not omitted by the universe scan cap. The scan cursor is persisted in `data/scan_state.json`; batches wrap around the complete NSE universe, and if a scan stops early due to a buy/position cap it resumes from the next unprocessed symbol.

The dashboard intentionally has **no login**, as requested. It includes a scrolling runtime ticker and an About & Rules page. It is bound to `127.0.0.1` by default and should only be accessed locally or through an SSH tunnel. Set a strong `FLASK_SECRET_KEY` for session/CSRF protection. Generate one with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Never expose port 5000 publicly. On GCP, access the dashboard using the SSH tunnel in the deployment section.

## 3. Strategy summary

### Entry conditions

1. Symbol is in the full NSE EQ-series equity universe and is neither active nor cooling down.
2. Price is within the configured price range.
3. Price is in the upper 40% of its 52-week range (range score at least 60%).
4. A sufficiently fresh provider quote/bar is available.
5. Average turnover over the prior 20 completed sessions is at least ₹2 crore by default, and average daily volume is at least 10,000 shares; both thresholds are configurable.
6. Current price breaks above the highest high of the prior 20 completed daily candles.
7. EMA(9) > EMA(20), and last completed daily close > EMA(20).
8. RSI is between 50 and 68.
9. Last completed daily volume is at least 1.2× the average of the preceding 20 completed sessions.
10. Optional AI/news screening does not explicitly return FAIL; if unavailable, technical rules can still pass.
11. The scan respects `MAX_OPEN_POSITIONS` and `MAX_BUYS_PER_SCAN`.

### Exit conditions

A position is paper-sold if price falls below EMA(9), RSI falls below `RSI_EXIT_THRESHOLD`, price falls below its recorded peak minus 2× the entry ATR(14), or the maximum five-session holding window is reached. There is no fixed percentage stop-loss or take-profit. The ATR exit does not guarantee a maximum loss; gaps and delayed/missing quotes can cause worse real-world outcomes.

Indicators use completed daily candles. The scan interval does not turn the strategy into a 15-minute-candle strategy.

## 4. Run locally

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
# Edit .env, then:
python app.py
```

The bot scheduler starts with the app. The dashboard defaults to `http://127.0.0.1:5000` and has no login, shows a scrolling status ticker, and includes an About & Rules page.

## 5. Deploy on Google Cloud Platform (Compute Engine VM)

A small Compute Engine VM is a straightforward fit for this bot because it runs a persistent scheduler and writes local CSV files. A VM and attached disk can incur charges; review Google Cloud pricing before creating resources. Official guide: [Create a Linux VM](https://docs.cloud.google.com/compute/docs/create-linux-vm-instance).

### A. Create a VM

1. In Google Cloud Console, select/create a project and enable billing.
2. Enable the Compute Engine API.
3. Create a Linux VM (Ubuntu LTS is suitable). Choose a small instance for initial paper testing.
4. Do **not** create a public firewall rule for TCP port 5000. Keep SSH access restricted to your own access method/IP where practical.
5. Connect using the VM's SSH button in the Console or `gcloud compute ssh`.

### B. Upload the ZIP and `.env`

Run these commands on your **own computer**, replacing `INSTANCE_NAME` and `ZONE`. Download the bot ZIP and the `.env.example` template first, then create your private `.env` locally. No dashboard login is configured yet.

```bash
gcloud compute scp TradingBot_All_NSE_Equities_Paper_GCP.zip INSTANCE_NAME:~/bot.zip --zone ZONE
gcloud compute scp .env INSTANCE_NAME:~/bot.env --zone ZONE
```

On the VM:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip unzip
sudo mkdir -p /opt/niftybot
sudo chown "$USER":"$USER" /opt/niftybot
unzip ~/bot.zip -d /opt/niftybot
# Move your private environment file into place and restrict access:
mv ~/bot.env /opt/niftybot/.env
chmod 600 /opt/niftybot/.env
cd /opt/niftybot
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Check the `.env` file before starting. Keep the Upstox token empty until it is issued; Yahoo mode will be selected automatically.

### C. Run it as a persistent service

On the VM, create a systemd unit:

```bash
sudo tee /etc/systemd/system/niftybot.service >/dev/null <<'SERVICE'
[Unit]
Description=Nifty Swing Paper Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=kvslakshmi77
WorkingDirectory=/opt/niftybot
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/niftybot/.venv/bin/python3 /opt/niftybot/app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICE
```

Replace `YOUR_LINUX_USERNAME` with the output of `whoami`, then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now niftybot
sudo systemctl status niftybot
```

View logs:

```bash
sudo journalctl -u niftybot -f
```

Restart after editing `.env` or updating the app:

```bash
sudo systemctl restart niftybot
```

### D. Access the dashboard securely with an SSH tunnel

Keep `FLASK_HOST=127.0.0.1` in `.env`; do not open port 5000 in a GCP firewall. The dashboard has no login at this stage, so the localhost binding and private SSH tunnel are essential. On your own computer, run:

```bash
gcloud compute ssh INSTANCE_NAME --zone ZONE -- -N -L 5000:127.0.0.1:5000
```

Keep that terminal open, then visit `http://127.0.0.1:5000` in your browser. There is no login; keep the tunnel private and do not expose port 5000 to the internet. Close the tunnel with Ctrl+C when finished.

### E. Update to Upstox later

1. Edit `/opt/niftybot/.env` on the VM and set `UPSTOX_ACCESS_TOKEN` to your token.
2. Keep the token private and the file permission at `600`.
3. Restart: `sudo systemctl restart niftybot`.
4. Check `sudo journalctl -u niftybot -n 100` for provider errors. With a non-empty token, the bot selects Upstox. If Upstox data/quotes fail, that symbol is skipped rather than silently using a stale price.

### F. Persist and back up paper-trading data

The CSV files live under `/opt/niftybot/data/` on the VM's disk. Back up this directory before upgrades and periodically. Do not run multiple app instances against the same CSV files. This is a paper-trading prototype; for a more robust long-running service, migrate the ledger to SQLite or another transactional database.

## 6. Data files

- `data/active_stocks.csv` — current paper positions.
- `data/sold_stocks.csv` — closed paper positions.
- `data/trades_history.csv` — BUY/SELL ledger.
- `data/cooling_period.csv` — re-entry cooldown records.
- `data/nse_equity_symbols.csv` — full NSE EQ-series universe cache.
- `data/scan_state.json` — persistent rotating scan cursor.

The full NSE EQ-series symbol universe is refreshed from NSE's official equity-list CSV. Only regular `EQ` series equities are included; ETFs, debt instruments, SME listings, and other non-EQ series are excluded. If NSE is unavailable, a previously downloaded full-universe cache is retained. A fresh installation without a valid cache skips scanning instead of silently substituting a small sample. The list refresh is from NSE directly; Gemini/Google search is not used as a source of trading symbols because search results are not a stable, authoritative machine-readable security master.

## 7. Risk and data limitations

- Paper trading only; no broker orders are sent.
- Yahoo Finance is a third-party source, may be delayed or incomplete, and is not guaranteed to provide real-time exchange data.
- An analytics token is read-only; it is not an order-placement token.
- No fixed percentage stop-loss exists by design. An ATR-based trailing exit is not a guaranteed risk cap.
- Strategy heuristics are not a promise of returns. Test over many market conditions, including slippage, fees, taxes, gaps, and unavailable data.
- Keep the dashboard private, keep secrets out of Git/ZIP files, and rotate any credential that may have been exposed.


## Live dashboard status ticker

The dashboard now includes a continuously scrolling status ticker and a recent-activity panel. It polls the local `/api/status` endpoint every three seconds and reports the configured market-data provider (Upstox when a token is set, otherwise Yahoo Finance), universe refresh results, scan start/progress/completion, data warnings, and paper BUY/SELL records. Status messages are kept in a bounded in-memory buffer and are not durable across a process restart; operational logs also go to the service journal. The ticker is informational and does not mean quotes are real-time or guaranteed.

The dashboard remains unauthenticated by request, so keep `FLASK_HOST=127.0.0.1` and access it on GCP using an SSH tunnel. Do not open port 5000 to the internet. The status endpoint is subject to the same localhost-only access boundary.
