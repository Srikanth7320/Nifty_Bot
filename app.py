"""Flask dashboard + Telegram control for a paper-trading bot. No live orders."""
import os, threading, time, secrets, tempfile, json
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
import pandas as pd
import schedule
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, flash, session, abort, jsonify
from bot_status import publish_status, recent_status
from bot_engine import (run_market_scan, refresh_nse_equity_symbols, send_telegram,
    ACTIVE_FILE, SOLD_FILE, COOLING_FILE, TRADES_FILE, fetch_stock_data_with_fallback,
    is_market_open, get_provider_status, _read_csv, ACTIVE_COLUMNS, SOLD_COLUMNS, COOLING_COLUMNS, TRADE_COLUMNS, SCAN_INTERVAL_MINUTES, run_api_health_checks, report_api_health, DATA_DIR, NSE_HOLIDAYS as ENGINE_NSE_HOLIDAYS)

load_dotenv()
IST = ZoneInfo("Asia/Kolkata")
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
APPROVED_CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID") or "")

# Serializes quantity updates handled by this Flask process.
# CSV replacement is atomic so readers do not see a partially-written file.
ACTIVE_UPDATE_LOCK = threading.RLock()

def _write_active_csv_atomic(dataframe, destination):
    destination = os.path.abspath(destination)
    directory = os.path.dirname(destination) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".active_stocks_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            dataframe.to_csv(handle, index=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


# NSE holidays change annually. Keep this list current from official NSE notices.
NSE_HOLIDAYS = set(filter(None, os.getenv("NSE_HOLIDAYS", "").split(",")))

def market_open_with_holidays():
    now = datetime.now(IST)
    return is_market_open() and now.strftime("%Y-%m-%d") not in NSE_HOLIDAYS

# Persistent post-close price snapshot. It is deliberately separate from trade-entry/exit state.
CLOSING_SNAPSHOT_FILE = os.path.join(DATA_DIR, "closing_prices.json")
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nseindia.com/",
    "Origin": "https://www.nseindia.com",
}

def _read_closing_snapshot():
    try:
        with open(CLOSING_SNAPSHOT_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {"prices": {}}
    except (OSError, ValueError, TypeError):
        return {"prices": {}}

def capture_post_close_prices():
    """Capture NSE website's post-close last-traded price for each active position.

    NSE's public website endpoint is not a formally supported market-data API and may
    rate-limit/block automated requests. On any per-symbol failure, the last good
    stored price is retained rather than overwritten with a guessed value.
    """
    now = datetime.now(IST)
    if now.weekday() >= 5 or now.strftime("%Y-%m-%d") in (NSE_HOLIDAYS | ENGINE_NSE_HOLIDAYS) or now.time() < datetime.strptime("15:35", "%H:%M").time():
        return {"updated": 0, "failed": 0, "skipped": True}
    active = _read_csv(ACTIVE_FILE, ACTIVE_COLUMNS)
    snapshot = _read_closing_snapshot()
    prices = snapshot.setdefault("prices", {})
    if active.empty:
        snapshot.update({"captured_at": now.strftime("%Y-%m-%d %H:%M:%S"), "date": now.strftime("%Y-%m-%d"), "source": "NSE website post-close quote"})
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CLOSING_SNAPSHOT_FILE, "w", encoding="utf-8") as handle: json.dump(snapshot, handle, indent=2)
        publish_status("Post-close price snapshot: no active positions to update.", "info")
        return {"updated": 0, "failed": 0, "skipped": False}
    session = requests.Session()
    updated, failed = 0, []
    try:
        # Prime the NSE session for its cookie-based public website endpoint.
        session.get("https://www.nseindia.com/", headers=NSE_HEADERS, timeout=12)
        for _, row in active.iterrows():
            symbol = str(row.get("symbol", "")).strip().upper()
            if not symbol: continue
            try:
                response = session.get("https://www.nseindia.com/api/quote-equity", params={"symbol": symbol}, headers=NSE_HEADERS, timeout=12)
                response.raise_for_status()
                payload = response.json()
                price = float(payload.get("priceInfo", {}).get("lastPrice", 0) or 0)
                if price <= 0 or not (price < float("inf")):
                    raise ValueError("NSE response had no valid lastPrice")
                prices[symbol] = {"price": round(price, 2), "date": now.strftime("%Y-%m-%d"),
                                  "captured_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                                  "source": "NSE website post-close last-traded price"}
                updated += 1
            except Exception as exc:
                failed.append(symbol)
                print(f"NSE post-close snapshot failed for {symbol}: {type(exc).__name__}")
            time.sleep(0.25)
    finally:
        session.close()
    snapshot["prices"] = prices
    if updated:
        snapshot["captured_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        snapshot["date"] = now.strftime("%Y-%m-%d")
        snapshot["source"] = "NSE website post-close last-traded price"
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CLOSING_SNAPSHOT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, indent=2)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp, CLOSING_SNAPSHOT_FILE)
    message = f"Post-close NSE price snapshot: updated {updated}/{len(active)} active positions."
    if failed: message += f" Failed symbols retained their last saved price: {', '.join(failed[:15])}"
    publish_status(message, "warning" if failed else "success")
    if failed:
        send_telegram("⚠️ " + message)
    return {"updated": updated, "failed": len(failed), "symbols_failed": failed}

def get_active_position_price(symbol):
    """Live quote during market hours; persistent NSE snapshot while market is closed."""
    symbol = str(symbol).strip().upper()
    if market_open_with_holidays():
        live = fetch_stock_data_with_fallback(symbol)
        if live:
            return {"price": float(live["price"]), "source": live.get("provider", "Live quote"), "captured_at": "Live market quote", "is_snapshot": False}
        return None
    saved = _read_closing_snapshot().get("prices", {}).get(symbol)
    if isinstance(saved, dict) and float(saved.get("price", 0) or 0) > 0:
        return {**saved, "is_snapshot": True}
    # Before the first successful NSE snapshot only, show the best available fallback
    # while clearly identifying it as a fallback in the UI.
    live = fetch_stock_data_with_fallback(symbol)
    if live:
        return {"price": float(live["price"]), "source": "Yahoo Finance fallback (no NSE snapshot yet)", "captured_at": "Fallback; not an NSE snapshot", "is_snapshot": True}
    return None

def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]
app.jinja_env.globals["csrf_token"] = csrf_token

@app.before_request
def csrf_protection():
    # No login is implemented yet by design. Keep the server bound to localhost
    # and require CSRF tokens for state-changing POST routes.
    if request.method == "POST":
        supplied = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not expected or not secrets.compare_digest(supplied, expected):
            abort(400, "Invalid CSRF token. Refresh the page and retry.")


def telegram_command_listener():
    if not TELEGRAM_TOKEN or not APPROVED_CHAT_ID:
        return
    last_update_id = 0
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                             params={"offset": last_update_id + 1, "timeout": 25}, timeout=30)
            r.raise_for_status()
            for update in r.json().get("result", []):
                last_update_id = max(last_update_id, int(update.get("update_id", 0)))
                message = update.get("message", {})
                chat_id = str(message.get("chat", {}).get("id", ""))
                command = (message.get("text") or "").strip().lower()
                if chat_id != APPROVED_CHAT_ID: continue
                if command == "/summary": send_portfolio_summary(chat_id)
                elif command == "/scan":
                    if not market_open_with_holidays():
                        send_telegram("Manual scan rejected: market is closed or holiday.")
                    else:
                        send_telegram("Paper scan started.")
                        result = run_market_scan()
                        send_telegram(f"Scan result: {result}")
        except Exception as exc:
            print(f"Telegram polling error: {exc}")
        time.sleep(2)

def send_portfolio_summary(chat_id):
    trades = _read_csv(TRADES_FILE, TRADE_COLUMNS)
    active = _read_csv(ACTIVE_FILE, ACTIVE_COLUMNS)
    sold = _read_csv(SOLD_FILE, SOLD_COLUMNS)
    buys = int((trades["type"] == "BUY").sum()) if not trades.empty else 0
    sells = int((trades["type"] == "SELL").sum()) if not trades.empty else 0
    realized = pd.to_numeric(sold["profit"], errors="coerce").fillna(0).sum() if not sold.empty else 0.0
    unrealized = 0.0; priced = 0; unpriced = 0
    for _, row in active.iterrows():
        live = get_active_position_price(str(row["symbol"]))
        if live:
            unrealized += (live["price"] - float(row["buy_price"])) * int(row["quantity"]); priced += 1
        else: unpriced += 1
    msg = (f"Paper portfolio summary\nBuy records: {buys}\nSell records: {sells}\nActive positions: {len(active)}\n"
           f"Realized P&L: ₹{realized:.2f}\nUnrealized P&L (priced positions only): ₹{unrealized:.2f}\n"
           f"Positions without fresh data: {unpriced}")
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": msg}, timeout=10).raise_for_status()
    except Exception as exc: print(f"Telegram summary error: {exc}")

def pre_market_api_check():
    """Recheck required integrations before market open; degrade news gate on failure."""
    healthy, results = run_api_health_checks(include_rss=True)
    details = report_api_health(results, "Pre-market API check")
    if not healthy:
        # This is a daily fallback only. RSS is still supplementary and never
        # upgrades an incomplete NewsAPI/Gemini check to a full confirmation.
        os.environ["REQUIRE_AI_NEWS_CONFIRMATION"] = "false"
        send_telegram("⚠️ *Pre-market API check failed*\n" + details +
                      "\nREQUIRE_AI_NEWS_CONFIRMATION set to false for this process/day. News/RSS is not treated as a full news confirmation.")
    else:
        os.environ["REQUIRE_AI_NEWS_CONFIRMATION"] = os.getenv("REQUIRE_AI_NEWS_CONFIRMATION_CONFIGURED", "true")
        send_telegram("✅ *Pre-market API check passed*\n" + details)

def background_scheduler():
    publish_status("Background scheduler started.")
    publish_status("Market data: Yahoo Finance for intraday scans; best-effort NSE website snapshots for active positions after close.", "info")
    publish_status("Dashboard has no login by design; keep it on localhost and use an SSH tunnel on GCP.", "info")
    refresh_nse_equity_symbols()
    schedule.every().day.at("08:00").do(pre_market_api_check)
    schedule.every().day.at("08:05").do(refresh_nse_equity_symbols)
    schedule.every().day.at("15:45").do(capture_post_close_prices)
    schedule.every(SCAN_INTERVAL_MINUTES).minutes.do(lambda: run_market_scan() if market_open_with_holidays() else None)
    while True:
        schedule.run_pending(); time.sleep(1)

@app.route("/api/status")
def api_status():
    return jsonify({"events": recent_status(60), "provider": get_provider_status()})

@app.route("/")
def dashboard():
    active = _read_csv(ACTIVE_FILE, ACTIVE_COLUMNS)
    sold = _read_csv(SOLD_FILE, SOLD_COLUMNS)
    cooling = _read_csv(COOLING_FILE, COOLING_COLUMNS)
    total_invested = total_current = 0.0; unpriced_count = 0
    for _, row in active.iterrows():
        buy = float(row["buy_price"]); qty = int(row["quantity"]); total_invested += buy * qty
        live = get_active_position_price(str(row["symbol"]))
        if live: total_current += live["price"] * qty
        else: unpriced_count += 1
    realized = pd.to_numeric(sold["profit"], errors="coerce").fillna(0).sum() if not sold.empty else 0.0
    summary = {"active_count": len(active), "sold_count": len(sold), "cooling_count": len(cooling),
        "total_invested": round(total_invested, 2), "total_current": round(total_current, 2),
        "unrealized_pnl": round(total_current-total_invested, 2) if unpriced_count == 0 else None,
        "realized_pnl": round(float(realized), 2), "unpriced_count": unpriced_count}
    return render_template("dashboard.html", summary=summary, market_open=market_open_with_holidays())

@app.route("/about")
def about_page():
    from bot_engine import (MIN_PRICE, MAX_PRICE, RSI_BUY_THRESHOLD, RSI_MAX_BUY_THRESHOLD,
        RSI_EXIT_THRESHOLD, MAX_OPEN_POSITIONS, MAX_BUYS_PER_SCAN, MAX_SCAN_SYMBOLS,
        SCAN_INTERVAL_MINUTES, COOLING_HOURS, MIN_AVG_DAILY_TURNOVER_INR, MIN_AVG_DAILY_VOLUME)
    return render_template("about.html",
        provider_name="Yahoo Finance for intraday scans; NSE website post-close snapshot for active positions",
        min_price=MIN_PRICE, max_price=MAX_PRICE, rsi_min=RSI_BUY_THRESHOLD, rsi_max=RSI_MAX_BUY_THRESHOLD,
        rsi_exit=RSI_EXIT_THRESHOLD, max_open=MAX_OPEN_POSITIONS, max_buys=MAX_BUYS_PER_SCAN,
        max_scan=MAX_SCAN_SYMBOLS, scan_interval=SCAN_INTERVAL_MINUTES, cooling_hours=COOLING_HOURS,
        min_turnover=f"{MIN_AVG_DAILY_TURNOVER_INR:,.0f}", min_volume=f"{MIN_AVG_DAILY_VOLUME:,.0f}")

@app.route("/active")
def active_page():
    stocks = []
    active = _read_csv(ACTIVE_FILE, ACTIVE_COLUMNS)
    for _, row in active.iterrows():
        symbol = str(row["symbol"]); buy = float(row["buy_price"]); qty = int(row["quantity"])
        live = get_active_position_price(symbol); price = live["price"] if live else None
        invested = buy * qty; current_val = price * qty if price is not None else None
        pnl = current_val - invested if current_val is not None else None
        stocks.append({"symbol":symbol,"buy_time":row["buy_time"],"buy_price":round(buy,2),"current_price":round(price,2) if price is not None else None,
          "quantity":qty,"invested":round(invested,2),"current_val":round(current_val,2) if current_val is not None else None,
          "pnl":round(pnl,2) if pnl is not None else None,"pnl_pct":round(pnl/invested*100,2) if pnl is not None and invested else None,
          "price_source":live.get("source", "Unknown") if live else "Unavailable", "price_captured_at":live.get("captured_at", "") if live else "", "is_snapshot":bool(live and live.get("is_snapshot"))})
    snapshot_meta = _read_closing_snapshot()
    return render_template("active.html", active_stocks=stocks, market_open=market_open_with_holidays(), snapshot_meta=snapshot_meta)

@app.route("/sold")
def sold_page(): return render_template("sold.html", sold_stocks=_read_csv(SOLD_FILE, SOLD_COLUMNS).fillna("").to_dict(orient="records"))
@app.route("/cooling")
def cooling_page(): return render_template("cooling.html", cooling_stocks=_read_csv(COOLING_FILE, COOLING_COLUMNS).fillna("").to_dict(orient="records"))

@app.route("/update_quantity", methods=["POST"])
def update_quantity():
    symbol = str(request.form.get("symbol", "")).strip().upper()

    try:
        quantity = int(request.form.get("quantity", ""))
    except (TypeError, ValueError):
        quantity = 0

    if not symbol or quantity < 1 or quantity > 1_000_000:
        flash("Quantity must be between 1 and 1,000,000.", "warning")
        return redirect(url_for("active_page"), code=303)

    try:
        with ACTIVE_UPDATE_LOCK:
            active = _read_csv(ACTIVE_FILE, ACTIVE_COLUMNS)
            symbol_mask = active["symbol"].astype(str).str.upper() == symbol

            if not symbol_mask.any():
                flash("Position not found.", "warning")
            else:
                active.loc[symbol_mask, "quantity"] = quantity
                _write_active_csv_atomic(active, ACTIVE_FILE)
                flash(
                    f"Updated {symbol} quantity to {quantity} (paper portfolio only).",
                    "success",
                )
    except Exception:
        app.logger.exception("Failed to update paper quantity for %s", symbol)
        flash("Could not update quantity due to a file error. Check the application log.", "warning")

    return redirect(url_for("active_page"), code=303)

@app.route("/trigger_scan", methods=["POST"])
def trigger_scan():
    if not market_open_with_holidays(): flash("Market closed or holiday; scan not run.", "warning")
    else:
        result = run_market_scan()
        flash(f"Scan result: {result}", "success" if result.get("status") == "complete" else "warning")
    return redirect(url_for("dashboard"))

if __name__ == "__main__":
    # Fail closed: do not launch dashboard/scheduler if a required integration is down.
    healthy, results = run_api_health_checks(include_rss=True)
    details = report_api_health(results, "Startup API check")
    if not healthy:
        print("Startup aborted because one or more required API checks failed. Fix configuration/connectivity and restart.")
        # Telegram may itself be down, but send a diagnostic alert if possible.
        send_telegram("🛑 *NSE bot startup aborted*\n" + details + "\nFix failed integrations and restart the service.")
        raise SystemExit(1)
    # Preserve the configured strict-mode value for a healthy pre-market reset.
    os.environ.setdefault("REQUIRE_AI_NEWS_CONFIRMATION_CONFIGURED", os.getenv("REQUIRE_AI_NEWS_CONFIRMATION", "true"))
    publish_status("API checks passed. Web dashboard starting in localhost-only mode.")
    threading.Thread(target=background_scheduler, daemon=True).start()
    threading.Thread(target=telegram_command_listener, daemon=True).start()
    app.run(host=os.getenv("FLASK_HOST", "127.0.0.1"), port=int(os.getenv("FLASK_PORT", "5000")), debug=False)
