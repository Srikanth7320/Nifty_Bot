# NSE Short-Term Momentum Playbook — SOP Paper-Trading Bot

This application generates **paper-trading** entries/exits for short-term swing setups. It does not submit, modify, or cancel broker orders. Paper P&L is not real profit and this strategy has not been proven profitable.

## 1. Market-data provider status

This archive implements **Yahoo Finance daily history plus recent Yahoo intraday bars only**. The code in this archive does **not** implement Upstox quotes or Upstox historical candles, even if an Upstox token is present. Do not assume setting `UPSTOX_ACCESS_TOKEN` switches providers. Yahoo Finance is unofficial, can be delayed/incomplete/rate-limited, and is not an exchange-grade feed. During market hours the bot rejects missing or stale intraday bars rather than using yesterday's close for new trade decisions. No broker orders are submitted; this is paper trading only.

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
| `YFINANCE_QUOTE_INTERVAL` | `5m` | Yahoo intraday bar interval. |
| `YFINANCE_MAX_QUOTE_AGE_MINUTES` | `30` | Maximum age of the latest Yahoo bar for trade decisions. |
| `RSI_BUY_THRESHOLD` | `50` | Lower bound of the RSI entry range. |
| `RSI_MAX_BUY_THRESHOLD` | `68` | Upper bound of the RSI entry range. |
| `RSI_EXIT_THRESHOLD` | `45` | RSI exit threshold. |

`MAX_SCAN_SYMBOLS` controls how many stocks are scanned, not how many buy signals are sent. The bot may buy fewer than `MAX_BUYS_PER_SCAN`, including zero. It checks active positions separately so their exits are not omitted by the universe scan cap. The scan cursor is persisted in `data/scan_state.json`; batches wrap around the complete NSE universe, and if a scan stops early due to a buy/position cap it resumes from the next unprocessed symbol.

The dashboard intentionally has **no login yet**, as requested. It is bound to `127.0.0.1` by default and should only be accessed locally or through an SSH tunnel. Set a strong `FLASK_SECRET_KEY` for session/CSRF protection. Generate one with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Never expose port 5000 publicly. On GCP, access the dashboard using the SSH tunnel in the deployment section.

## 3. Strategy summary

New entries are accepted only when the SOP technical setup, manual external-data checklist, liquidity requirements, news gate, position sizing and session timing all pass. The external checklist is fail-closed: unknown circuit/surveillance/pledging/institutional/catalyst/delivery data blocks the entry. Existing paper positions are managed using stop-loss, staged partial exits, breakeven protection, runner trailing and emergency exit rules. See **Section 6: Mandatory SOP enforcement added** for the exact fields and thresholds.

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

The bot scheduler starts with the app. The dashboard defaults to `http://127.0.0.1:5000` and intentionally has no login yet.

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

Check the `.env` file before starting. This archive currently uses Yahoo Finance only; an Upstox token will not change the provider until Upstox integration is implemented.

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
User=YOUR_LINUX_USERNAME
WorkingDirectory=/opt/niftybot
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/niftybot/.venv/bin/python /opt/niftybot/app.py
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

Keep that terminal open, then visit `http://127.0.0.1:5000` in your browser. There is no login yet; keep the tunnel private and do not expose port 5000 to the internet. Close the tunnel with Ctrl+C when finished.

### E. Upstox integration status

Upstox integration is not included in this archive. Receiving a token will not activate Upstox until a real quote/history adapter is implemented and tested. Continue with Yahoo only if its limitations are acceptable, or wait for the Upstox adapter before relying on this bot.

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


## Dashboard status ticker and About page

The dashboard displays a scrolling runtime ticker for provider selection, NSE universe refresh, scan progress, warnings and paper signals. Open **About & Rules** to see entry/exit conditions and current configuration. There is no login; keep the dashboard bound to `127.0.0.1` and use an SSH tunnel on GCP.

## API startup checks and supplementary news RSS

Before launching the dashboard/scheduler, the bot checks Yahoo Finance quote access, NewsAPI, Gemini, and Telegram. If any required check fails, startup aborts; fix the integration/network issue and restart. Google News RSS is checked as a supplementary source, but an RSS outage alone does not fail the required integration check.

At 08:00 IST each day, required integrations are checked again and a Telegram status alert is sent. If a required pre-market check fails, the process sets `REQUIRE_AI_NEWS_CONFIRMATION=false` for the day as a clearly logged technical-only fallback. This does not make news confirmation complete; BUY signals and logs should be understood as operating without full AI/news confirmation during that fallback. A healthy pre-market check restores the configured value.

For each candidate, the bot queries NewsAPI and Google News RSS independently, including RSS when NewsAPI succeeds. It merges/deduplicates the headlines and passes source labels to Gemini. RSS is supplementary only: while `REQUIRE_AI_NEWS_CONFIRMATION=true`, NewsAPI must succeed and return at least one usable headline, and Gemini must return an unambiguous PASS. RSS-only headlines never satisfy the NewsAPI requirement.

Create your private `.env` from `.env.example`; do not share or commit `.env`.

## 6. Mandatory SOP enforcement added

The entry workflow now uses a strict SOP gate rather than the legacy 52-week momentum score alone:

- **Weekly/daily trend:** 30-week SMA, Price > EMA9 > EMA20 > SMA50, rising EMA9/EMA20, and an approximate higher-high/higher-low check.
- **Momentum/volatility:** RSI 55–68, MACD(12,26,9) positive/expanding, Bollinger squeeze/expansion, ATR(14), a 2–6 week compressed base, breakout trigger, and intraday volume relative to the prior 20-day average.
- **Two entry setups:** Setup A high-volume shelf breakout; Setup B overbought-spike pullback/retest with declining volume and intraday green reversal. New entries are limited to 10:00–14:30 IST.
- **Liquidity:** average 20-session traded value and share volume minimums remain mandatory.
- **Risk sizing:** position size is capped at 1.5% of configured capital risk and available capital; the bot requires at least 4 shares so 50%/25%/25% staged exits are feasible. Configure `TRADING_CAPITAL_INR` to the actual paper-account capital.
- **Exit management:** stop loss, target 1 (50% partial), breakeven stop, round-number rejection partial, EMA20/EMA9 runner trail, emergency RSI/MACD/distribution exits, and a 15-session time exit.
- **External checks are fail-closed:** `data/sop_external_checks.csv` must contain a row for the symbol and current date. Circuit band, promoter pledge, FII/DII status, ASM/GSM, sector tailwind, catalyst, board meeting date, dilution, and delivery-volume ratio cannot be reliably inferred from Yahoo or RSS. Missing/unknown values block entry. `PASS` values must be based on your own verified NSE/company disclosures; do not enter guessed values.
- **News:** NewsAPI and Google News RSS are both queried; RSS is supplementary. Strict news confirmation still requires usable NewsAPI evidence and an unambiguous Gemini PASS.

### External checklist CSV values

Use one row per symbol per trading date. Expected values include `institutional_status=PASS`, `sector_tailwind_status=PASS`, `catalyst_status=PASS`, `equity_dilution_status=CLEAR`, and `surveillance_stage=NONE` or a verified stage below 2. `circuit_band_pct` must be `10` or `20`; `promoter_pledge_pct` must be below `10`; `delivery_volume_ratio` must be at least `1.2` by default. `board_meeting_date` should be the verified next earnings-related board date; leave blank only if you intend the strict gate to block the trade. `as_of_date` must match the current IST date exactly.

### Important implementation limitations

This is still **paper trading only**. The project does not submit broker orders. Yahoo Finance is unofficial and can be delayed; the bot rejects stale/missing intraday bars, but this does not make Yahoo an exchange-grade feed. The SOP's wick-angle, true multi-swing HH/HL, bearish RSI divergence, sector-wide breadth, corporate catalyst materiality, and NSE delivery/circuit/surveillance checks are approximated or manually supplied, not independently validated from official feeds. The external CSV is a control mechanism, not a live NSE integration. Validate on historical data and paper trade before considering any real capital.


### Post-close active-position prices

At 15:45 IST, the scheduler attempts to capture each active position from the NSE website quote endpoint and persists the result in `data/closing_prices.json`. While the NSE market is closed, the dashboard and portfolio summary use this saved snapshot instead of re-fetching Yahoo prices. During market hours they return to the normal fresh intraday quote path. This endpoint is a best-effort public website interface, not a guaranteed licensed market-data API; failures retain the last saved price and are reported in bot status/Telegram. Google Search snippets are intentionally not parsed as price data. Keep the bot process running for the scheduled snapshot.
