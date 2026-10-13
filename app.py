"""Flask dashboard + Telegram control for a paper-trading bot. No live orders."""
import os, threading, time, secrets
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
    is_market_open, get_provider_status, _read_csv, ACTIVE_COLUMNS, SOLD_COLUMNS, COOLING_COLUMNS, TRADE_COLUMNS, SCAN_INTERVAL_MINUTES)

load_dotenv()
IST = ZoneInfo("Asia/Kolkata")
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
APPROVED_CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID") or "")

# NSE holidays change annually. Keep this list current from official NSE notices.
NSE_HOLIDAYS = set(filter(None, os.getenv("NSE_HOLIDAYS", "").split(",")))

def market_open_with_holidays():
    now = datetime.now(IST)
    return is_market_open() and now.strftime("%Y-%m-%d") not in NSE_HOLIDAYS

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
        live = fetch_stock_data_with_fallback(str(row["symbol"]))
        if live:
            unrealized += (live["price"] - float(row["buy_price"])) * int(row["quantity"]); priced += 1
        else: unpriced += 1
    msg = (f"Paper portfolio summary\nBuy records: {buys}\nSell records: {sells}\nActive positions: {len(active)}\n"
           f"Realized P&L: ₹{realized:.2f}\nUnrealized P&L (priced positions only): ₹{unrealized:.2f}\n"
           f"Positions without fresh data: {unpriced}")
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": msg}, timeout=10).raise_for_status()
    except Exception as exc: print(f"Telegram summary error: {exc}")

def background_scheduler():
    publish_status("Background scheduler started.")
    # This repository currently implements Yahoo Finance quotes only.
    publish_status("Market-data provider: Yahoo Finance (Upstox quote integration is not implemented in this version).", "warning")
    publish_status("Dashboard has no login by design; keep it on localhost and use an SSH tunnel on GCP.", "info")
    refresh_nse_equity_symbols()
    schedule.every().day.at("08:00").do(refresh_nse_equity_symbols)
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
        live = fetch_stock_data_with_fallback(str(row["symbol"]))
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
        provider_name="Yahoo Finance (only implemented quote provider)",
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
        live = fetch_stock_data_with_fallback(symbol); price = live["price"] if live else None
        invested = buy * qty; current_val = price * qty if price is not None else None
        pnl = current_val - invested if current_val is not None else None
        stocks.append({"symbol":symbol,"buy_time":row["buy_time"],"buy_price":round(buy,2),"current_price":round(price,2) if price is not None else None,
          "quantity":qty,"invested":round(invested,2),"current_val":round(current_val,2) if current_val is not None else None,
          "pnl":round(pnl,2) if pnl is not None else None,"pnl_pct":round(pnl/invested*100,2) if pnl is not None and invested else None})
    return render_template("active.html", active_stocks=stocks)

@app.route("/sold")
def sold_page(): return render_template("sold.html", sold_stocks=_read_csv(SOLD_FILE, SOLD_COLUMNS).fillna("").to_dict(orient="records"))
@app.route("/cooling")
def cooling_page(): return render_template("cooling.html", cooling_stocks=_read_csv(COOLING_FILE, COOLING_COLUMNS).fillna("").to_dict(orient="records"))

@app.route("/update_quantity", methods=["POST"])
def update_quantity():
    symbol = str(request.form.get("symbol", "")).strip().upper()
    try: quantity = int(request.form.get("quantity", ""))
    except (TypeError, ValueError): quantity = 0
    if not symbol or quantity < 1 or quantity > 1_000_000:
        flash("Quantity must be between 1 and 1,000,000.", "warning")
        return redirect(url_for("active_page"))
    from bot_engine import DATA_LOCK, _write_csv_atomic
    with DATA_LOCK:
        active = _read_csv(ACTIVE_FILE, ACTIVE_COLUMNS)
        if not (active["symbol"].astype(str).str.upper() == symbol).any():
            flash("Position not found.", "warning")
        else:
            active.loc[active["symbol"].astype(str).str.upper() == symbol, "quantity"] = quantity
            _write_csv_atomic(active, ACTIVE_FILE)
            flash(f"Updated {symbol} quantity to {quantity} (paper portfolio only).", "success")
    return redirect(url_for("active_page"))

@app.route("/trigger_scan", methods=["POST"])
def trigger_scan():
    if not market_open_with_holidays(): flash("Market closed or holiday; scan not run.", "warning")
    else:
        result = run_market_scan()
        flash(f"Scan result: {result}", "success" if result.get("status") == "complete" else "warning")
    return redirect(url_for("dashboard"))

if __name__ == "__main__":
    # No login yet: intentionally localhost-only. Access remotely only via SSH tunnel.
    publish_status("Web dashboard starting in localhost-only mode.")
    threading.Thread(target=background_scheduler, daemon=True).start()
    threading.Thread(target=telegram_command_listener, daemon=True).start()
    app.run(host=os.getenv("FLASK_HOST", "127.0.0.1"), port=int(os.getenv("FLASK_PORT", "5000")), debug=False)
