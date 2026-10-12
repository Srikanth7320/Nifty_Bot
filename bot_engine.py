"""Paper-trading engine. No broker orders are submitted by this module."""
from __future__ import annotations
import json, os, threading, tempfile
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
import requests
from dotenv import load_dotenv
from bot_status import publish_status

load_dotenv()
IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SYMBOLS_FILE = str(DATA_DIR / "nse_equity_symbols.csv")
SCAN_STATE_FILE = DATA_DIR / "scan_state.json"
ACTIVE_FILE = str(DATA_DIR / "active_stocks.csv")
SOLD_FILE = str(DATA_DIR / "sold_stocks.csv")
TRADES_FILE = str(DATA_DIR / "trades_history.csv")
COOLING_FILE = str(DATA_DIR / "cooling_period.csv")
INSTRUMENTS_FILE = DATA_DIR / "upstox_nse_instruments.json"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN") or os.getenv("UPSTOX_ANALYTICS_ACCESS_TOKEN")
publish_status("Trading engine module loaded. Data provider selected: " + ("Upstox" if UPSTOX_ACCESS_TOKEN and UPSTOX_ACCESS_TOKEN.strip() else "Yahoo Finance (no Upstox token configured)"))
MIN_PRICE = float(os.getenv("MIN_STOCK_PRICE", 50))
MAX_PRICE = float(os.getenv("MAX_STOCK_PRICE", 5000))
MIN_AVG_DAILY_TURNOVER_INR = max(0.0, float(os.getenv("MIN_AVG_DAILY_TURNOVER_INR", 20000000)))
MIN_AVG_DAILY_VOLUME = max(0.0, float(os.getenv("MIN_AVG_DAILY_VOLUME", 10000)))
RSI_BUY_THRESHOLD = float(os.getenv("RSI_BUY_THRESHOLD", 50))
RSI_MAX_BUY_THRESHOLD = float(os.getenv("RSI_MAX_BUY_THRESHOLD", 68))
RSI_EXIT_THRESHOLD = float(os.getenv("RSI_EXIT_THRESHOLD", 45))
COOLING_HOURS = max(0, float(os.getenv("COOLING_PERIOD_HOURS", 24)))
MAX_SCAN_SYMBOLS = max(1, int(os.getenv("MAX_SCAN_SYMBOLS", 500)))
MAX_OPEN_POSITIONS = max(1, int(os.getenv("MAX_OPEN_POSITIONS", 10)))
MAX_BUYS_PER_SCAN = max(1, int(os.getenv("MAX_BUYS_PER_SCAN", 3)))
SCAN_INTERVAL_MINUTES = max(1, int(os.getenv("SCAN_INTERVAL_MINUTES", 15)))
YFINANCE_QUOTE_INTERVAL = os.getenv("YFINANCE_QUOTE_INTERVAL", "5m").strip() or "5m"
YFINANCE_MAX_QUOTE_AGE_MINUTES = max(1, float(os.getenv("YFINANCE_MAX_QUOTE_AGE_MINUTES", 30)))
# Prevent overlapping scans within this process. Run only one app worker for CSV storage.
SCAN_LOCK = threading.Lock()
DATA_LOCK = threading.RLock()

TRADE_COLUMNS = ["time", "symbol", "price", "type", "quantity", "reason", "pnl"]
ACTIVE_COLUMNS = ["symbol", "buy_price", "quantity", "buy_time", "peak_price", "entry_atr"]
SOLD_COLUMNS = ["symbol", "buy_price", "sold_price", "quantity", "profit", "sold_time", "reason"]
COOLING_COLUMNS = ["symbol", "time"]


def get_current_ist_time():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _read_csv(path, columns):
    try:
        if os.path.exists(path):
            df = pd.read_csv(path)
            for col in columns:
                if col not in df.columns:
                    df[col] = pd.NA
            return df[columns].copy()
    except (pd.errors.EmptyDataError, pd.errors.ParserError, OSError, ValueError) as exc:
        print(f"Could not read {path}: {exc}")
    return pd.DataFrame(columns=columns)


def _write_csv_atomic(df, path):
    path = str(path)
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".csv", dir=directory)
    os.close(fd)
    try:
        df.to_csv(tmp, index=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"Telegram error: {exc}")


def refresh_nse_equity_symbols():
    """Refresh the tradable NSE main-board equity universe from NSE's official CSV.

    Only the regular EQ series is included; ETFs, debt, SME and non-equity
    instruments are intentionally excluded. On failure, retain a previously
    downloaded full-universe cache; never silently replace it with a sample.
    """
    urls = [
        "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
        "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
    ]
    last_error = None
    for url in urls:
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; PaperTradingBot/1.0)"}, timeout=20)
            r.raise_for_status()
            df = pd.read_csv(pd.io.common.BytesIO(r.content))
            # NSE's published header uses SYMBOL and SERIES; tolerate casing/spacing changes.
            cols = {str(c).strip().upper(): c for c in df.columns}
            symbol_col = cols.get("SYMBOL")
            series_col = cols.get("SERIES")
            if symbol_col is None or df.empty:
                raise ValueError(f"Unexpected NSE equity CSV columns: {list(df.columns)}")
            if series_col is not None:
                df = df[df[series_col].astype(str).str.strip().str.upper().eq("EQ")]
            symbols = (df[symbol_col].dropna().astype(str).str.strip().str.upper()
                       .loc[lambda x: x.str.fullmatch(r"[A-Z0-9&_-]+", na=False)]
                       .drop_duplicates().sort_values())
            if len(symbols) < 100:
                raise ValueError(f"NSE list returned only {len(symbols)} eligible symbols; refusing to replace cache")
            _write_csv_atomic(pd.DataFrame({"Symbol": symbols.tolist()}), SYMBOLS_FILE)
            publish_status(f"NSE universe refreshed: {len(symbols)} eligible EQ-series stocks loaded from the official security list.", "success")
            return True
        except Exception as exc:
            last_error = exc
            publish_status(f"NSE universe refresh failed from {url}: {exc}", "warning")
    if os.path.exists(SYMBOLS_FILE) and os.path.getsize(SYMBOLS_FILE) > 0:
        try:
            cached = pd.read_csv(SYMBOLS_FILE)
            if "Symbol" in cached.columns and len(cached) >= 100:
                publish_status(f"NSE source unavailable; retaining cached universe ({len(cached)} symbols).", "warning")
                return True
        except Exception:
            pass
    publish_status(f"No valid full NSE universe cache; scanning is paused. Last error: {last_error}", "error")
    return False


# Backwards-compatible name for deployments that imported the earlier helper.
refresh_nifty500_symbols = refresh_nse_equity_symbols


def _read_scan_cursor(universe_size):
    if universe_size <= 0:
        return 0
    try:
        payload = json.loads(SCAN_STATE_FILE.read_text(encoding="utf-8"))
        return int(payload.get("cursor", 0)) % universe_size
    except Exception:
        return 0


def rotating_scan_batch(symbols, limit):
    """Return the next slice of the complete universe, wrapping around the end."""
    if not symbols:
        return [], 0
    start = _read_scan_cursor(len(symbols))
    count = min(max(1, int(limit)), len(symbols))
    return [symbols[(start + i) % len(symbols)] for i in range(count)], start


def advance_scan_cursor(start, processed, universe_size):
    """Persist progress only for symbols actually examined, so early exits resume fairly."""
    if universe_size <= 0:
        return
    next_cursor = (int(start) + max(0, int(processed))) % universe_size
    try:
        fd, tmp = tempfile.mkstemp(prefix=".scan_state_", suffix=".json", dir=str(DATA_DIR))
        os.close(fd)
        Path(tmp).write_text(json.dumps({"cursor": next_cursor, "updated_at": get_current_ist_time()}), encoding="utf-8")
        os.replace(tmp, SCAN_STATE_FILE)
    except Exception as exc:
        print(f"Could not persist rotating scan cursor: {exc}")
        try:
            if 'tmp' in locals() and os.path.exists(tmp): os.remove(tmp)
        except OSError:
            pass


def is_market_open():
    now = datetime.now(IST)
    return now.weekday() < 5 and dtime(9, 15) <= now.time() <= dtime(15, 30)


def calculate_rsi(hist, window=14):
    close = pd.to_numeric(hist["Close"], errors="coerce")
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(window, min_periods=window).mean()
    loss = (-delta.clip(upper=0)).rolling(window, min_periods=window).mean()
    if len(close) < window + 1 or pd.isna(gain.iloc[-1]) or pd.isna(loss.iloc[-1]):
        return float("nan")
    if loss.iloc[-1] == 0:
        return 100.0 if gain.iloc[-1] > 0 else 50.0
    rs = gain.iloc[-1] / loss.iloc[-1]
    return float(100 - (100 / (1 + rs)))


def calculate_atr(hist, window=14):
    """Average True Range from completed daily candles; returns None if unavailable."""
    if hist is None or len(hist) < window + 1:
        return None
    high = pd.to_numeric(hist["High"], errors="coerce")
    low = pd.to_numeric(hist["Low"], errors="coerce")
    close = pd.to_numeric(hist["Close"], errors="coerce")
    prev_close = close.shift(1)
    true_range = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    value = true_range.rolling(window, min_periods=window).mean().iloc[-1]
    return float(value) if pd.notna(value) and value > 0 else None


def _upstox_instrument_map():
    """Cache the official Upstox NSE instrument master and map equity symbols to keys."""
    url = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
    # Upstox serves this file gzip-compressed; requests transparently decompresses it.
    refresh = not INSTRUMENTS_FILE.exists() or (datetime.now().timestamp() - INSTRUMENTS_FILE.stat().st_mtime > 24 * 3600)
    if refresh:
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            import gzip
            raw = r.content
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, list) or not payload:
                raise ValueError("Unexpected Upstox instrument master format")
            tmp = INSTRUMENTS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, INSTRUMENTS_FILE)
        except Exception as exc:
            print(f"Upstox instrument master refresh failed: {exc}")
    if not INSTRUMENTS_FILE.exists():
        return {}
    try:
        rows = json.loads(INSTRUMENTS_FILE.read_text(encoding="utf-8"))
        return {str(x.get("trading_symbol", "")).upper(): x["instrument_key"]
                for x in rows if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ"
                and x.get("trading_symbol") and x.get("instrument_key")}
    except Exception as exc:
        print(f"Upstox instrument cache invalid: {exc}")
        return {}


def _fetch_upstox_history(symbol):
    if not UPSTOX_ACCESS_TOKEN:
        return None
    mapping = _upstox_instrument_map()
    key = mapping.get(symbol.upper().replace(".NS", ""))
    if not key:
        print(f"No Upstox equity instrument key for {symbol}")
        return None
    to_date = datetime.now(IST).date()
    from_date = to_date - timedelta(days=400)
    url = f"https://api.upstox.com/v2/historical-candle/{requests.utils.quote(key, safe='')}/day/{to_date:%Y-%m-%d}/{from_date:%Y-%m-%d}"
    headers = {"Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}", "Accept": "application/json"}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        candles = r.json().get("data", {}).get("candles", [])
        if not candles:
            return None
        # Upstox candle shape: [timestamp, open, high, low, close, volume, open_interest]
        df = pd.DataFrame(candles, columns=["Date", "Open", "High", "Low", "Close", "Volume", "OpenInterest"][:len(candles[0])])
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.dropna(subset=["Date", "Close"]).sort_values("Date").set_index("Date")
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.dropna(subset=["Close"])
    except Exception as exc:
        print(f"Upstox history error for {symbol}: {exc}")
        return None


def _yfinance_ticker_symbol(symbol):
    return symbol if symbol.endswith(".NS") else f"{symbol}.NS"


def _fetch_yfinance_history(symbol):
    """Fetch daily candles from Yahoo Finance for indicators (not an exchange-guaranteed feed)."""
    import yfinance as yf
    df = yf.Ticker(_yfinance_ticker_symbol(symbol)).history(period="1y", interval="1d", auto_adjust=False)
    return None if df.empty else df


def _fetch_yfinance_quote(symbol):
    """Fetch a recent intraday bar close. Yahoo data may be delayed; stale bars are rejected."""
    import yfinance as yf
    ticker = yf.Ticker(_yfinance_ticker_symbol(symbol))
    try:
        bars = ticker.history(period="1d", interval=YFINANCE_QUOTE_INTERVAL, auto_adjust=False, prepost=False)
    except Exception as exc:
        print(f"Yahoo Finance intraday quote error for {symbol}: {exc}")
        return None
    if bars is None or bars.empty or "Close" not in bars:
        return None
    bars = bars.dropna(subset=["Close"])
    if bars.empty:
        return None
    stamp = pd.Timestamp(bars.index[-1])
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize(IST)
    else:
        stamp = stamp.tz_convert(IST)
    age_minutes = (pd.Timestamp(datetime.now(IST)) - stamp).total_seconds() / 60.0
    price = float(bars["Close"].iloc[-1])
    # Intraday bar timestamps identify the bar, not a guaranteed exchange tick time.
    if price <= 0 or age_minutes < -2 or age_minutes > YFINANCE_MAX_QUOTE_AGE_MINUTES:
        publish_status(f"Yahoo Finance quote for {symbol} is stale/unusable (age={age_minutes:.1f} min).", "warning")
        return None
    return {"price": price, "time": stamp.isoformat(),
            "source": f"Yahoo Finance {YFINANCE_QUOTE_INTERVAL} bar close (may be delayed)",
            "fresh": True}


def fetch_stock_data_with_fallback(symbol):
    """Use Upstox when a token is configured; otherwise use Yahoo daily + fresh intraday bars."""
    clean = str(symbol).strip().upper().replace(".NS", "")
    try:
        use_upstox = bool(UPSTOX_ACCESS_TOKEN and UPSTOX_ACCESS_TOKEN.strip())
        hist = _fetch_upstox_history(clean) if use_upstox else _fetch_yfinance_history(clean)
        if hist is None or hist.empty or len(hist) < 50:
            return None
        hist = hist.copy()
        # Indicators use completed daily candles only; never use today's partial candle.
        if is_market_open():
            try:
                local_dates = pd.to_datetime(hist.index).date
                hist = hist.loc[[d < datetime.now(IST).date() for d in local_dates]]
            except Exception:
                pass
        for col in ["Close", "High", "Low", "Volume"]:
            if col not in hist:
                return None
            hist[col] = pd.to_numeric(hist[col], errors="coerce")
        hist = hist.dropna(subset=["Close", "High", "Low"])
        if len(hist) < 50:
            return None
        # Historical close is for display only; decisions require a fresh provider quote/bar.
        current_price = float(hist["Close"].iloc[-1])
        quote_source = "last completed daily candle (display only)"
        quote_time = str(hist.index[-1])
        quote_is_fresh = False
        if use_upstox:
            key = _upstox_instrument_map().get(clean)
            if key:
                try:
                    r = requests.get("https://api.upstox.com/v3/market-quote/quotes",
                                     headers={"Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}", "Accept": "application/json"},
                                     params={"instrument_key": key}, timeout=10)
                    r.raise_for_status()
                    quote_data = r.json().get("data", {})
                    item = quote_data.get(key) or next(iter(quote_data.values()), {})
                    lp = item.get("last_price")
                    if lp is not None and float(lp) > 0:
                        current_price = float(lp)
                        quote_source = "Upstox last traded price"
                        quote_time = item.get("timestamp") or item.get("last_trade_time") or get_current_ist_time()
                        quote_is_fresh = True
                except Exception as exc:
                    publish_status(f"Upstox quote unavailable for {clean}; skipping this symbol: {exc}", "warning")
        else:
            quote = _fetch_yfinance_quote(clean)
            if quote:
                current_price = quote["price"]
                quote_source = quote["source"]
                quote_time = quote["time"]
                quote_is_fresh = True
        rsi = calculate_rsi(hist)
        if pd.isna(rsi) or current_price <= 0:
            return None
        return {"symbol": clean, "price": current_price, "low_52w": float(hist["Low"].tail(252).min()),
                "high_52w": float(hist["High"].tail(252).max()), "rsi": rsi, "history": hist,
                "atr": calculate_atr(hist), "quote_source": quote_source, "quote_time": str(quote_time),
                "quote_is_fresh": quote_is_fresh}
    except Exception as exc:
        publish_status(f"Data error for {clean}: {exc}", "warning")
        return None


def fetch_news_and_fundamentals(symbol):
    """AI/news is optional context only; never claims financial statements were verified."""
    if not GEMINI_API_KEY:
        return True, "AI filter disabled; technical rules only"
    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        news = ""
        if NEWS_API_KEY:
            r = requests.get("https://newsapi.org/v2/everything", params={"q": f"{symbol} stock India", "apiKey": NEWS_API_KEY, "pageSize": 3, "sortBy": "publishedAt"}, timeout=8)
            r.raise_for_status()
            news = " ".join(a.get("title", "") for a in r.json().get("articles", []))
        prompt = (f"Give a cautious sentiment screen for NSE ticker {symbol} using ONLY these headlines: {news!r}. "
                  "Do not infer quarterly profits or fundamentals from headlines. Return PASS, FAIL, or UNKNOWN and a short reason.")
        response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        answer = (response.text or "").strip()
        first = answer.upper()
        if first.startswith("FAIL") or "STATUS: FAIL" in first:
            return False, answer
        if first.startswith("PASS") or "STATUS: PASS" in first:
            return True, answer
        # UNKNOWN or malformed result should not silently reject a technical paper signal.
        return True, "AI sentiment inconclusive; technical rules only"
    except Exception as exc:
        print(f"AI/news filter unavailable for {symbol}: {exc}")
        return True, "AI/news unavailable; technical rules only"


def evaluate_swing_entry(hist, current_price, rsi):
    """1–5 session swing setup: 20-day breakout + short-term trend + RSI/volume confirmation.

    All historical indicators use completed daily candles; current_price is a fresh Upstox quote.
    The breakout threshold is the prior 20 completed sessions' high, excluding the latest candle.
    """
    if hist is None or len(hist) < 30 or pd.isna(rsi) or current_price <= 0:
        return False, "Insufficient data"
    close = pd.to_numeric(hist["Close"], errors="coerce")
    high = pd.to_numeric(hist["High"], errors="coerce")
    volume = pd.to_numeric(hist["Volume"], errors="coerce")
    ema9 = close.ewm(span=9, adjust=False, min_periods=9).mean().iloc[-1]
    ema20 = close.ewm(span=20, adjust=False, min_periods=20).mean().iloc[-1]
    prior_20d_high = high.iloc[-21:-1].max() if len(high) >= 21 else float("nan")
    vol_avg20 = volume.iloc[-21:-1].mean() if len(volume) >= 21 else float("nan")
    last_completed_volume = volume.iloc[-1]
    checks = {
        "breakout": (pd.notna(prior_20d_high) and current_price > float(prior_20d_high)
                     and pd.notna(close.iloc[-1]) and close.iloc[-1] <= float(prior_20d_high)),
        "trend": pd.notna(ema9) and pd.notna(ema20) and ema9 > ema20 and close.iloc[-1] > ema20,
        "rsi": RSI_BUY_THRESHOLD <= float(rsi) <= RSI_MAX_BUY_THRESHOLD,
        "volume": pd.notna(vol_avg20) and vol_avg20 > 0 and pd.notna(last_completed_volume) and last_completed_volume >= 1.2 * vol_avg20,
    }
    passed = all(checks.values())
    details = ", ".join(f"{name}={'PASS' if ok else 'FAIL'}" for name, ok in checks.items())
    return passed, details


def evaluate_technicals(hist, current_price, rsi):
    """Backward-compatible wrapper for the short-term swing entry filter."""
    passed, _ = evaluate_swing_entry(hist, current_price, rsi)
    return passed


def update_position_peak(symbol, peak_price, entry_atr=None):
    """Persist a position's highest observed price and initial ATR if missing."""
    with DATA_LOCK:
        active = _active_df()
        mask = active["symbol"].astype(str).str.upper() == str(symbol).upper()
        if not mask.any():
            return
        old_peak = pd.to_numeric(active.loc[mask, "peak_price"], errors="coerce").max()
        if pd.notna(old_peak):
            peak_price = max(float(old_peak), float(peak_price))
        active.loc[mask, "peak_price"] = float(peak_price)
        existing_atr = pd.to_numeric(active.loc[mask, "entry_atr"], errors="coerce").iloc[0]
        if pd.isna(existing_atr) and entry_atr and entry_atr > 0:
            active.loc[mask, "entry_atr"] = float(entry_atr)
        _write_csv_atomic(active, ACTIVE_FILE)


def trading_sessions_held(hist, buy_time):
    """Count completed market sessions after the entry date using the fetched candle calendar."""
    try:
        entered = pd.to_datetime(buy_time, errors="raise")
        entry_date = entered.date()
        dates = pd.to_datetime(hist.index, errors="coerce").date
        # Count the entry session as day 1; exit by the fifth session at the latest.
        return 1 + sum(1 for day in dates if day >= entry_date)
    except Exception:
        return 0


def _active_df(): return _read_csv(ACTIVE_FILE, ACTIVE_COLUMNS)
def _sold_df(): return _read_csv(SOLD_FILE, SOLD_COLUMNS)
def _trades_df(): return _read_csv(TRADES_FILE, TRADE_COLUMNS)
def _cooling_df(): return _read_csv(COOLING_FILE, COOLING_COLUMNS)


def _cooling_active_symbols():
    df = _cooling_df()
    if df.empty: return set()
    now = datetime.now(IST)
    active = set()
    for _, row in df.iterrows():
        try:
            stamp = pd.to_datetime(row["time"], errors="raise")
            if stamp.tzinfo is None: stamp = stamp.tz_localize(IST)
            else: stamp = stamp.tz_convert(IST)
            if now - stamp.to_pydatetime() < timedelta(hours=COOLING_HOURS):
                active.add(str(row["symbol"]).upper())
        except Exception:
            continue
    return active


def record_buy_trade(symbol, price, quantity=1, reason="Swing breakout signal", entry_atr=None):
    symbol = str(symbol).upper().strip()
    price = float(price); quantity = int(quantity)
    if not symbol or price <= 0 or quantity <= 0: raise ValueError("Invalid buy details")
    with DATA_LOCK:
        active = _active_df()
        if symbol in active["symbol"].astype(str).str.upper().values:
            return False
        now = get_current_ist_time()
        active = pd.concat([active, pd.DataFrame([{"symbol": symbol, "buy_price": price, "quantity": quantity, "buy_time": now,
            "peak_price": price, "entry_atr": entry_atr if entry_atr and entry_atr > 0 else pd.NA}])], ignore_index=True)
        trades = _trades_df()
        trades = pd.concat([trades, pd.DataFrame([{"time": now, "symbol": symbol, "price": price, "type": "BUY", "quantity": quantity, "reason": reason, "pnl": 0.0}])], ignore_index=True)
        _write_csv_atomic(active, ACTIVE_FILE)
        _write_csv_atomic(trades, TRADES_FILE)
    return True


def record_sell_trade(symbol, buy_price, sold_price, quantity, reason="Exit signal"):
    symbol = str(symbol).upper().strip()
    buy_price, sold_price, quantity = float(buy_price), float(sold_price), int(quantity)
    if not symbol or buy_price <= 0 or sold_price <= 0 or quantity <= 0: raise ValueError("Invalid sell details")
    with DATA_LOCK:
        active = _active_df()
        match = active[active["symbol"].astype(str).str.upper() == symbol]
        if match.empty:
            return False  # idempotency: never record a second sale for a closed position
        actual = match.iloc[0]
        buy_price = float(actual["buy_price"]); quantity = int(actual["quantity"])
        now = get_current_ist_time(); profit = round((sold_price - buy_price) * quantity, 2)
        active = active[active["symbol"].astype(str).str.upper() != symbol]
        sold = _sold_df()
        sold = pd.concat([sold, pd.DataFrame([{"symbol": symbol, "buy_price": buy_price, "sold_price": sold_price, "quantity": quantity, "profit": profit, "sold_time": now, "reason": reason}])], ignore_index=True)
        trades = _trades_df()
        trades = pd.concat([trades, pd.DataFrame([{"time": now, "symbol": symbol, "price": sold_price, "type": "SELL", "quantity": quantity, "reason": reason, "pnl": profit}])], ignore_index=True)
        cooling = _cooling_df()
        cooling = pd.concat([cooling[cooling["symbol"].astype(str).str.upper() != symbol], pd.DataFrame([{"symbol": symbol, "time": now}])], ignore_index=True)
        # Atomic individual files; single worker/scan lock prevents in-process concurrent writes.
        _write_csv_atomic(sold, SOLD_FILE)
        _write_csv_atomic(trades, TRADES_FILE)
        _write_csv_atomic(cooling, COOLING_FILE)
        _write_csv_atomic(active, ACTIVE_FILE)
    return True


def run_market_scan(force=False):
    if not force and not is_market_open():
        return {"status": "market_closed", "checked": 0, "buys": 0, "sells": 0}
    if not SCAN_LOCK.acquire(blocking=False):
        return {"status": "scan_already_running", "checked": 0, "buys": 0, "sells": 0}
    try:
        publish_status(f"Paper scan started at {get_current_ist_time()} IST.")
        if not os.path.exists(SYMBOLS_FILE) or os.path.getsize(SYMBOLS_FILE) == 0:
            if not refresh_nse_equity_symbols():
                return {"status": "symbol_universe_unavailable", "checked": 0, "buys": 0, "sells": 0}
        try:
            symbols_df = pd.read_csv(SYMBOLS_FILE)
            col = "Symbol" if "Symbol" in symbols_df.columns else symbols_df.columns[0]
            all_symbols = symbols_df[col].dropna().astype(str).str.strip().str.upper().drop_duplicates().tolist()
            if len(all_symbols) < 100:
                return {"status": "symbol_universe_invalid", "checked": 0, "buys": 0, "sells": 0}
            symbols, scan_start = rotating_scan_batch(all_symbols, MAX_SCAN_SYMBOLS)
            provider = "Upstox" if UPSTOX_ACCESS_TOKEN and UPSTOX_ACCESS_TOKEN.strip() else "Yahoo Finance"
            publish_status(f"Provider: {provider}. Scanning batch of {len(symbols)} from {len(all_symbols)} eligible NSE EQ stocks (cursor {scan_start}).", "success")
        except Exception as exc:
            publish_status(f"Cannot load NSE equity symbols: {exc}", "error")
            return {"status": "symbol_file_error", "checked": 0, "buys": 0, "sells": 0}
        buys = sells = checked = processed_symbols = 0
        # Positions are checked first. A fresh provider quote/bar is mandatory for decisions.
        # Without a fresh quote/bar, never trade from a stale daily close.
        active = _active_df().copy()
        for _, row in active.iterrows():
            symbol = str(row["symbol"]).upper()
            data = fetch_stock_data_with_fallback(symbol)
            if not data or not data.get("quote_is_fresh", False):
                continue
            checked += 1
            price = float(data["price"])
            buy = float(row["buy_price"])
            hist = data["history"]
            close = pd.to_numeric(hist["Close"], errors="coerce")
            ema9 = close.ewm(span=9, adjust=False, min_periods=9).mean().iloc[-1]
            atr = data.get("atr")
            previous_peak = pd.to_numeric(pd.Series([row.get("peak_price")]), errors="coerce").iloc[0]
            peak = max(price, float(previous_peak)) if pd.notna(previous_peak) else max(price, buy)
            entry_atr = pd.to_numeric(pd.Series([row.get("entry_atr")]), errors="coerce").iloc[0]
            if pd.isna(entry_atr) or entry_atr <= 0:
                entry_atr = atr
            update_position_peak(symbol, peak, entry_atr)
            held_sessions = trading_sessions_held(hist, row.get("buy_time"))
            # No fixed percentage SL/TP: use trend failure, volatility-adjusted trailing exit,
            # and a maximum 5-session holding window for the requested 1–5 day strategy.
            trail_atr = float(entry_atr) if pd.notna(entry_atr) and entry_atr > 0 else (float(atr) if atr and atr > 0 else None)
            trail_level = peak - 2.0 * trail_atr if trail_atr else None
            reasons = []
            if pd.notna(ema9) and price < float(ema9):
                reasons.append("price below 9-day EMA")
            if data["rsi"] < RSI_EXIT_THRESHOLD:
                reasons.append(f"RSI {data['rsi']:.1f} below {RSI_EXIT_THRESHOLD:g}")
            if trail_level is not None and price < trail_level:
                reasons.append("2×ATR trailing trend exit")
            if held_sessions >= 5:
                reasons.append("maximum 5 trading sessions reached")
            if not reasons:
                continue
            reason = "; ".join(reasons)
            pnl_pct = (price / buy - 1) * 100 if buy else 0
            if record_sell_trade(symbol, buy, price, int(row["quantity"]), reason):
                sells += 1
                publish_status(f"PAPER SELL signal recorded for {symbol} at ₹{price:.2f}; reason: {reason}.", "warning")
                send_telegram(f"🔴 PAPER SELL\nStock: {symbol}\nBuy: ₹{buy:.2f}\nPrice: ₹{price:.2f}\nP&L: {pnl_pct:.2f}%\nHeld sessions: {held_sessions}\nReason: {reason}")
        cooling_symbols = _cooling_active_symbols()
        active_symbols = set(_active_df()["symbol"].astype(str).str.upper())
        for symbol in symbols:
            if len(active_symbols) >= MAX_OPEN_POSITIONS or buys >= MAX_BUYS_PER_SCAN:
                break
            processed_symbols += 1
            if processed_symbols == 1 or processed_symbols % 25 == 0:
                publish_status(f"Scan progress: {processed_symbols}/{len(symbols)} batch symbols examined; {checked} fresh-data checks so far.")
            if symbol in cooling_symbols or symbol in active_symbols: continue
            data = fetch_stock_data_with_fallback(symbol)
            if not data or not data.get("quote_is_fresh", False):
                continue
            checked += 1; price = float(data["price"])
            if not (MIN_PRICE <= price <= MAX_PRICE): continue
            low, high = data["low_52w"], data["high_52w"]
            if high <= low: continue
            perf_score = (price - low) / (high - low) * 100
            if perf_score < 60: continue
            # Liquidity guard: require both average traded value and share volume
            # over the prior 20 completed sessions, separate from breakout volume.
            hist = data["history"]
            recent = hist.tail(21).copy()
            close20 = pd.to_numeric(recent["Close"], errors="coerce").iloc[-21:-1]
            volume20 = pd.to_numeric(recent["Volume"], errors="coerce").iloc[-21:-1]
            avg_turnover = (close20 * volume20).mean()
            avg_volume = volume20.mean()
            if (pd.isna(avg_turnover) or pd.isna(avg_volume)
                    or avg_turnover < MIN_AVG_DAILY_TURNOVER_INR
                    or avg_volume < MIN_AVG_DAILY_VOLUME):
                continue
            entry_ok, entry_details = evaluate_swing_entry(hist, price, data["rsi"])
            if not entry_ok: continue
            passed, note = fetch_news_and_fundamentals(symbol)
            if not passed: continue
            if record_buy_trade(symbol, price, 1, f"1–5 session swing breakout ({entry_details})", data.get("atr")):
                buys += 1; active_symbols.add(symbol)
                publish_status(f"PAPER BUY recorded for {symbol} at ₹{price:.2f}; entry: {entry_details}.", "success")
                send_telegram(f"🟢 PAPER SWING BUY\nStock: {symbol}\nPrice: ₹{price:.2f}\n52W range score: {perf_score:.1f}%\nRSI: {data['rsi']:.1f}\nEntry: {entry_details}\nData: {data['quote_source']} ({data['quote_time']})\nNote: {note}")
        advance_scan_cursor(scan_start, processed_symbols, len(all_symbols))
        result = {"status": "complete", "checked": checked, "buys": buys, "sells": sells,
                  "universe_size": len(all_symbols), "batch_size": len(symbols),
                  "next_scan_cursor": _read_scan_cursor(len(all_symbols))}
        publish_status(f"Scan completed: checked={checked}, paper buys={buys}, paper sells={sells}, universe={len(all_symbols)}, batch={len(symbols)}.", "success")
        return result
    finally:
        SCAN_LOCK.release()
