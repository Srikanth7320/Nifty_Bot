"""Paper-trading engine for the NSE bot.

This version of the uploaded project uses Yahoo Finance for quotes. It does not
implement Upstox; do not label a scan as Upstox unless a real Upstox fetcher is
added and successfully returns usable data.
"""
import os
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv

try:
    from google import genai
except ImportError:
    genai = None

load_dotenv()
IST = ZoneInfo("Asia/Kolkata")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "").strip()

MIN_PRICE = float(os.getenv("MIN_STOCK_PRICE", "50"))
MAX_PRICE = float(os.getenv("MAX_STOCK_PRICE", "5000"))
RSI_BUY_THRESHOLD = float(os.getenv("RSI_BUY_THRESHOLD", "55"))
RSI_EXIT_THRESHOLD = float(os.getenv("RSI_EXIT_THRESHOLD", "45"))
MAX_SCAN_SYMBOLS = max(1, int(os.getenv("MAX_SCAN_SYMBOLS", "50")))

# Optional comma-separated YYYY-MM-DD values in .env. The defaults below are
# exchange holidays listed for 2026; keep this list updated each calendar year.
_DEFAULT_HOLIDAYS_2026 = {
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-21",
    "2026-03-26", "2026-03-31", "2026-04-03", "2026-04-14",
    "2026-05-01", "2026-05-28", "2026-06-26", "2026-08-15",
    "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-08",
    "2026-11-10", "2026-11-24", "2026-12-25",
}
_env_holidays = os.getenv("NSE_HOLIDAYS", "").strip()
NSE_HOLIDAYS = ({x.strip() for x in _env_holidays.split(",") if x.strip()}
                if _env_holidays else _DEFAULT_HOLIDAYS_2026)

client = None
if GEMINI_API_KEY and genai is not None:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as exc:
        print(f"Gemini client initialization failed: {exc}")

DATA_DIR = os.getenv("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)
SYMBOLS_FILE = os.path.join(DATA_DIR, "nifty500_symbols.csv")
ACTIVE_FILE = os.path.join(DATA_DIR, "active_stocks.csv")
SOLD_FILE = os.path.join(DATA_DIR, "sold_stocks.csv")
TRADES_FILE = os.path.join(DATA_DIR, "trades_history.csv")
COOLING_FILE = os.path.join(DATA_DIR, "cooling_period.csv")

# Read by app/dashboard code if desired. Status describes the latest completed
# scan, while LAST_QUOTE_PROVIDER describes the latest successful quote fetch.
LAST_SCAN_PROVIDER = "Not scanned yet"
LAST_SCAN_NOTE = "Waiting for the next market scan"
LAST_SCAN_TIME = ""
LAST_QUOTE_PROVIDER = "None"
LAST_QUOTE_TIME = ""


def get_current_ist_time():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def set_scan_status(provider, note, scan_time=None):
    global LAST_SCAN_PROVIDER, LAST_SCAN_NOTE, LAST_SCAN_TIME
    LAST_SCAN_PROVIDER = str(provider)
    LAST_SCAN_NOTE = str(note)
    LAST_SCAN_TIME = scan_time or get_current_ist_time()
    message = f"Last scan: {LAST_SCAN_PROVIDER} — {LAST_SCAN_NOTE}"
    print(f"[SCAN STATUS] {LAST_SCAN_TIME} | {message}")
    try:
        from bot_status import publish_status
        publish_status(message, "warning" if ("No usable" in LAST_SCAN_PROVIDER or "incomplete" in LAST_SCAN_NOTE.lower()) else "success")
    except Exception:
        pass


def get_provider_status():
    """Return a simple dict for dashboard/API use."""
    return {
        "last_scan_provider": LAST_SCAN_PROVIDER,
        "last_scan_note": LAST_SCAN_NOTE,
        "last_scan_time": LAST_SCAN_TIME,
        "last_quote_provider": LAST_QUOTE_PROVIDER,
        "last_quote_time": LAST_QUOTE_TIME,
    }


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if not response.ok:
            print(f"Telegram API returned HTTP {response.status_code}: {response.text[:200]}")
    except Exception as exc:
        print(f"Telegram error: {exc}")


def refresh_nifty500_symbols():
    url = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NSEPaperBot/1.0)"}
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        # Validate the response before replacing the current local symbol file.
        from io import BytesIO
        candidate = pd.read_csv(BytesIO(response.content))
        if candidate.empty or "Symbol" not in candidate.columns:
            raise ValueError("NSE response did not contain a Symbol column")
        candidate.to_csv(SYMBOLS_FILE, index=False)
        print(f"Updated Nifty 500 symbols: {len(candidate)} rows")
        return True
    except Exception as exc:
        print(f"NSE symbol-list refresh failed; using local cache if available: {exc}")

    if not os.path.exists(SYMBOLS_FILE) or os.path.getsize(SYMBOLS_FILE) == 0:
        pd.DataFrame({"Symbol": ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"]}).to_csv(
            SYMBOLS_FILE, index=False
        )
    return False


def is_market_open():
    """Regular NSE equity session in IST; also checks configured holidays."""
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    if now.strftime("%Y-%m-%d") in NSE_HOLIDAYS:
        return False
    return dtime(9, 15) <= now.time() <= dtime(15, 30)


def calculate_rsi(hist, window=14):
    if hist is None or "Close" not in hist or len(hist) < window + 1:
        return float("nan")
    delta = hist["Close"].diff()
    gain = delta.clip(lower=0).rolling(window=window).mean()
    loss = (-delta.clip(upper=0)).rolling(window=window).mean()
    last_gain, last_loss = gain.iloc[-1], loss.iloc[-1]
    if pd.isna(last_gain) or pd.isna(last_loss):
        return float("nan")
    if last_loss == 0:
        return 100.0 if last_gain > 0 else 50.0
    return float(100 - (100 / (1 + (last_gain / last_loss))))


def fetch_stock_data_with_fallback(symbol):
    """Fetch usable Yahoo daily data. Despite the legacy function name, this
    uploaded version has no Upstox implementation or automatic provider fallback.
    """
    global LAST_QUOTE_PROVIDER, LAST_QUOTE_TIME
    clean_symbol = str(symbol).strip().upper()
    ticker_symbol = clean_symbol if clean_symbol.endswith(".NS") else f"{clean_symbol}.NS"
    try:
        hist = yf.Ticker(ticker_symbol).history(period="1y", auto_adjust=False)
        if hist is None or hist.empty or len(hist) < 50:
            print(f"Yahoo Finance returned insufficient history for {clean_symbol}")
            return None
        required = {"Close", "Low", "High", "Volume"}
        if not required.issubset(hist.columns):
            print(f"Yahoo Finance response missing required columns for {clean_symbol}")
            return None
        hist = hist.dropna(subset=["Close", "Low", "High"])
        if len(hist) < 50:
            return None
        current_price = float(hist["Close"].iloc[-1])
        low_52w = float(hist["Low"].tail(252).min())
        high_52w = float(hist["High"].tail(252).max())
        rsi = calculate_rsi(hist)
        if not (current_price > 0 and pd.notna(rsi) and pd.notna(low_52w) and pd.notna(high_52w)):
            print(f"Yahoo Finance returned invalid values for {clean_symbol}")
            return None
        LAST_QUOTE_PROVIDER = "Yahoo Finance"
        LAST_QUOTE_TIME = get_current_ist_time()
        return {
            "symbol": clean_symbol.removesuffix(".NS"),
            "price": current_price,
            "low_52w": low_52w,
            "high_52w": high_52w,
            "rsi": rsi,
            "history": hist,
            "provider": "Yahoo Finance",
        }
    except Exception as exc:
        print(f"Yahoo Finance quote error for {clean_symbol}: {exc}")
        return None


def fetch_news_and_fundamentals(symbol):
    """Fetch news independently, then require explicit Gemini confirmation by default.

    Returns (passed, explanation). Missing keys, no usable news, or API errors
    are reported as incomplete and do not silently pass a new BUY candidate.
    """
    require_confirmation = os.getenv("REQUIRE_AI_NEWS_CONFIRMATION", "true").strip().lower() in {
        "1", "true", "yes", "on"
    }
    headlines = []
    news_status = "NewsAPI disabled"
    news_error = None

    if NEWS_API_KEY:
        try:
            response = requests.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": f'"{symbol}" AND (NSE OR shares OR stock OR company)',
                    "apiKey": NEWS_API_KEY,
                    "pageSize": 5,
                    "sortBy": "publishedAt",
                    "language": "en",
                },
                timeout=10,
            )
            response.raise_for_status()
            body = response.json()
            if body.get("status") != "ok":
                raise RuntimeError(body.get("message", "NewsAPI returned non-ok status"))
            for article in body.get("articles", []):
                title = (article.get("title") or "").strip()
                published = (article.get("publishedAt") or "").strip()
                if title and title.lower() != "[removed]":
                    headlines.append(f"{published}: {title}" if published else title)
            news_status = f"NewsAPI returned {len(headlines)} headline(s)"
        except Exception as exc:
            news_error = type(exc).__name__
            news_status = f"NewsAPI unavailable ({news_error})"
            print(f"NewsAPI request failed for {symbol}: {news_error}")
    else:
        news_error = "API key not configured"

    if not GEMINI_API_KEY or client is None:
        reason = f"Gemini unavailable/not configured; {news_status}"
        return (not require_confirmation, "Technical-only mode: " + reason) if not require_confirmation else (False, "ANALYSIS INCOMPLETE: " + reason)

    if not headlines and require_confirmation:
        return False, f"ANALYSIS INCOMPLETE: {news_status}; no relevant headlines to assess"

    prompt = f"""You are a cautious research assistant supporting a PAPER-TRADING screen for NSE stock {symbol}.
Use ONLY the supplied evidence. Do not claim to have live market data, browse the web, or invent financial metrics.
Recent NewsAPI headlines (may be irrelevant or incomplete):
{chr(10).join(headlines) if headlines else "No headlines supplied."}

Evaluate only whether the supplied headlines contain material positive/negative risks or whether evidence is insufficient.
Return exactly this format:
STATUS: PASS or FAIL
SENTIMENT: positive, neutral, negative, or insufficient
RISK_FLAGS: comma-separated flags or none
REASON: one concise sentence
PASS only when evidence is sufficient and there is no clear material adverse signal. If evidence is insufficient, return FAIL.
"""
    try:
        response = client.models.generate_content(model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"), contents=prompt)
        answer = (getattr(response, "text", None) or "").strip()
        upper = answer.upper()
        if not answer or "STATUS: PASS" not in upper or "STATUS: FAIL" in upper:
            return False, "Gemini did not provide an unambiguous PASS: " + (answer[:400] or "empty response")
        return True, f"{news_status}; Gemini confirmed: {answer[:500]}"
    except Exception as exc:
        print(f"Gemini evaluation failed for {symbol}: {type(exc).__name__}")
        return False, f"ANALYSIS INCOMPLETE: Gemini request failed ({type(exc).__name__}); {news_status}"



def calculate_atr(hist, window=14):
    """Return the latest Average True Range, or NaN when history is insufficient."""
    if hist is None or len(hist) < window or not all(c in hist.columns for c in ("High", "Low", "Close")):
        return float("nan")
    high, low, close = hist["High"], hist["Low"], hist["Close"]
    previous_close = close.shift(1)
    true_range = pd.concat([(high - low).abs(), (high - previous_close).abs(), (low - previous_close).abs()], axis=1).max(axis=1)
    value = true_range.rolling(window=window).mean().iloc[-1]
    return float(value) if pd.notna(value) else float("nan")


def trading_sessions_held(history, entry_time):
    """Count sessions held using supplied history, not the machine's current date.

    The live/entry session counts as one. If history already contains the entry
    date, count that completed session plus the live session (e.g. 2). If the
    entry date is after the last history date, only the entry session counts (1).
    """
    try:
        entry = pd.Timestamp(entry_time)
        if entry.tzinfo is not None:
            entry = entry.tz_convert(IST).tz_localize(None)
        entry_date = entry.date()
        if history is None or history.empty:
            return 1

        idx = pd.DatetimeIndex(history.index)
        if idx.tz is not None:
            idx = idx.tz_convert(IST).tz_localize(None)
        dates = sorted(set(ts.date() for ts in idx))
        if not dates or entry_date > dates[-1]:
            return 1
        completed_since_entry = sum(1 for day in dates if day >= entry_date)
        return max(1, completed_since_entry + 1)
    except Exception:
        return 1


def _read_scan_cursor(universe_size):
    """Read scan cursor for tests and rotating universe batches."""
    cursor_file = os.path.join(DATA_DIR, "scan_cursor.txt")
    try:
        with open(cursor_file, "r", encoding="utf-8") as fh:
            return int(fh.read().strip() or "0") % max(1, universe_size)
    except (OSError, ValueError):
        return 0


def evaluate_swing_entry(hist, current_price, rsi):
    """Apply the technical swing-entry filters and return (passed, explanations).

    The live quote must break the prior 20 completed sessions' high; indicators
    use historical daily candles only. This is a screening rule, not a guarantee.
    """
    reasons = []
    if hist is None or len(hist) < 21:
        return False, ["Insufficient price history"]
    required = {"High", "Low", "Close", "Volume"}
    if not required.issubset(hist.columns):
        return False, ["Missing required OHLCV columns"]
    close = pd.to_numeric(hist["Close"], errors="coerce")
    high = pd.to_numeric(hist["High"], errors="coerce")
    volume = pd.to_numeric(hist["Volume"], errors="coerce")
    if pd.isna(current_price) or current_price <= 0 or pd.isna(rsi):
        return False, ["Invalid quote or RSI"]
    prior_high = float(high.iloc[-21:-1].max())
    if float(current_price) <= prior_high:
        reasons.append("Live price has not broken the prior 20-session high")
    ema9 = close.ewm(span=9, adjust=False).mean().iloc[-1]
    ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
    if not (ema9 > ema20):
        reasons.append("EMA9 is not above EMA20")
    if not (float(close.iloc[-1]) > ema20):
        reasons.append("Last completed close is not above EMA20")
    if not (50 <= float(rsi) <= 68):
        reasons.append("RSI is outside the 50-68 entry range")
    if len(volume) >= 21:
        avg_volume = float(volume.iloc[-21:-1].mean())
        if avg_volume <= 0 or float(volume.iloc[-1]) < 1.2 * avg_volume:
            reasons.append("Latest volume is below 1.2x prior 20-session average")
    else:
        reasons.append("Insufficient volume history")
    price_low = float(close.tail(252).min())
    price_high = float(close.tail(252).max())
    if price_high > price_low:
        range_score = (float(current_price) - price_low) / (price_high - price_low)
        if range_score < 0.60:
            reasons.append("Price is below the upper 40% of its 52-week range")
    if reasons:
        return False, reasons
    return True, ["All technical entry filters passed"]

def evaluate_technicals(hist, current_price, rsi):
    if hist is None or len(hist) < 50 or pd.isna(rsi):
        return False
    close = hist["Close"]
    volume = hist["Volume"]
    ma_50 = close.rolling(window=50).mean().iloc[-1]
    ma_200 = close.rolling(window=200).mean().iloc[-1] if len(hist) >= 200 else ma_50
    volume_avg = volume.rolling(window=20).mean().iloc[-1]
    current_volume = volume.iloc[-1]
    values = [ma_50, ma_200, volume_avg, current_volume]
    if any(pd.isna(value) for value in values):
        return False
    bullish_score = 0
    bearish_score = 0
    if current_price > ma_50:
        bullish_score += 2
    else:
        bearish_score += 1
    if current_price > ma_200:
        bullish_score += 2
    else:
        bearish_score += 1
    if rsi >= RSI_BUY_THRESHOLD:
        bullish_score += 2
    else:
        bearish_score += 1
    if current_volume > volume_avg:
        bullish_score += 1
    else:
        bearish_score += 1
    return bullish_score >= (bearish_score * 3)


def _read_csv_or_empty(path, columns):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return pd.DataFrame(columns=columns)
    try:
        frame = pd.read_csv(path)
        for column in columns:
            if column not in frame.columns:
                frame[column] = pd.Series(dtype="object")
        return frame
    except (pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
        print(f"Could not read {path}; treating as empty: {exc}")
        return pd.DataFrame(columns=columns)


def rotating_scan_batch(symbols, limit):
    """Return a rotating batch so large universes are covered over successive scans."""
    if not symbols:
        return [], 0
    limit = max(1, int(limit))
    cursor_file = os.path.join(DATA_DIR, "scan_cursor.txt")
    start = _read_scan_cursor(len(symbols))
    count = min(limit, len(symbols))
    batch = [symbols[(start + i) % len(symbols)] for i in range(count)]
    next_start = (start + count) % len(symbols)
    try:
        with open(cursor_file, "w", encoding="utf-8") as fh:
            fh.write(str(next_start))
    except OSError as exc:
        print(f"Could not persist scan cursor: {type(exc).__name__}")
    return batch, start


def run_market_scan():
    """Run one paper scan during market hours and publish its provider summary."""
    if not is_market_open():
        set_scan_status("Market closed", "Scheduled scan skipped outside NSE market hours")
        return

    global LAST_SCAN_PROVIDER
    scan_started = get_current_ist_time()
    print(f"Running paper market scan at {scan_started} IST")
    quote_providers = set()
    quote_successes = 0
    quote_failures = 0
    buy_count = 0
    sell_count = 0

    if not os.path.exists(SYMBOLS_FILE):
        refresh_nifty500_symbols()
    symbols_df = _read_csv_or_empty(SYMBOLS_FILE, ["Symbol"])
    if symbols_df.empty:
        set_scan_status("No data", "Symbol list is empty; scan not performed", scan_started)
        return
    symbol_col = "Symbol" if "Symbol" in symbols_df.columns else symbols_df.columns[0]
    symbols = [str(s).strip().upper() for s in symbols_df[symbol_col].dropna().tolist() if str(s).strip()]
    symbols = list(dict.fromkeys(symbols))
    symbols, _scan_start_index = rotating_scan_batch(symbols, MAX_SCAN_SYMBOLS)

    cooling_df = _read_csv_or_empty(COOLING_FILE, ["symbol", "time"])
    cooling_symbols = set(cooling_df["symbol"].astype(str).str.upper()) if not cooling_df.empty else set()
    active_df = _read_csv_or_empty(ACTIVE_FILE, ["symbol", "buy_price", "quantity", "buy_time"])
    active_symbols = set(active_df["symbol"].astype(str).str.upper()) if not active_df.empty else set()

    # Evaluate existing paper positions first. Work on a copy because selling
    # writes the active CSV while this loop is running.
    for _, row in active_df.copy().iterrows():
        sym = str(row.get("symbol", "")).strip().upper()
        if not sym:
            continue
        try:
            buy_price = float(row["buy_price"])
            qty = int(row.get("quantity", 1))
        except (TypeError, ValueError, KeyError):
            print(f"Skipping malformed active-position row: {row.to_dict()}")
            continue
        data = fetch_stock_data_with_fallback(sym)
        if not data:
            quote_failures += 1
            continue
        quote_successes += 1
        quote_providers.add(data["provider"])
        current_price, rsi, hist = data["price"], data["rsi"], data["history"]
        ma_50 = hist["Close"].rolling(window=50).mean().iloc[-1]
        pnl_pct = ((current_price - buy_price) / buy_price) * 100 if buy_price else 0.0
        if current_price < ma_50 or rsi < RSI_EXIT_THRESHOLD:
            reason = f"Momentum breakdown (price below 50-day average or RSI {rsi:.1f} below {RSI_EXIT_THRESHOLD})"
            record_sell_trade(sym, buy_price, current_price, qty)
            sell_count += 1
            send_telegram(
                f"🔴 *DYNAMIC PAPER SELL SIGNAL*\nStock: `{sym}`\nBought: ₹{buy_price:.2f}\n"
                f"Current: ₹{current_price:.2f}\nP&L: {pnl_pct:.2f}%\nReason: {reason}"
            )

    # Refresh active symbols after any sells, to avoid accidentally buying a symbol
    # that remains in the original in-memory active list.
    active_df = _read_csv_or_empty(ACTIVE_FILE, ["symbol", "buy_price", "quantity", "buy_time"])
    active_symbols = set(active_df["symbol"].astype(str).str.upper()) if not active_df.empty else set()

    for sym in symbols:
        if sym in cooling_symbols or sym in active_symbols:
            continue
        data = fetch_stock_data_with_fallback(sym)
        if not data:
            quote_failures += 1
            continue
        quote_successes += 1
        quote_providers.add(data["provider"])
        price, low, high, rsi = data["price"], data["low_52w"], data["high_52w"], data["rsi"]
        if not (MIN_PRICE <= price <= MAX_PRICE) or high <= low:
            continue
        perf_score = ((price - low) / (high - low)) * 100
        if perf_score < 70 or not evaluate_technicals(data["history"], price, rsi):
            continue
        passed_ai, reason = fetch_news_and_fundamentals(sym)
        if not passed_ai:
            print(f"Candidate {sym} skipped by fundamentals check: {reason}")
            continue
        send_telegram(
            f"🟢 *PAPER BUY SIGNAL*\nStock: `{sym}`\nPrice: ₹{price:.2f}\n"
            f"52W Score: {perf_score:.1f}%\nRSI: {rsi:.1f}\n"
            f"Reason: Bullish technicals; {reason[:180]}"
        )
        record_buy_trade(sym, price)
        active_symbols.add(sym)
        buy_count += 1

    if quote_providers:
        provider_label = " + ".join(sorted(quote_providers))
        if quote_failures:
            note = f"{quote_successes} usable quote(s); {quote_failures} quote failure(s); {buy_count} buy(s), {sell_count} sell(s)"
        else:
            note = f"{quote_successes} usable quote(s); {buy_count} buy(s), {sell_count} sell(s)"
        set_scan_status(provider_label, note, scan_started)
    else:
        set_scan_status("No usable quotes", f"No quote provider returned usable data; {quote_failures} quote failure(s)", scan_started)


def record_buy_trade(symbol, price):
    current_time = get_current_ist_time()
    trade_entry = {"time": current_time, "symbol": symbol, "price": price, "type": "BUY"}
    df_trades = _read_csv_or_empty(TRADES_FILE, ["time", "symbol", "price", "type"])
    df_trades = pd.concat([df_trades, pd.DataFrame([trade_entry])], ignore_index=True)
    df_trades.to_csv(TRADES_FILE, index=False)

    active_df = _read_csv_or_empty(ACTIVE_FILE, ["symbol", "buy_price", "quantity", "buy_time"])
    if symbol not in active_df["symbol"].astype(str).values:
        new_row = {"symbol": symbol, "buy_price": price, "quantity": 1, "buy_time": current_time}
        active_df = pd.concat([active_df, pd.DataFrame([new_row])], ignore_index=True)
        active_df.to_csv(ACTIVE_FILE, index=False)


def record_sell_trade(symbol, buy_price, sold_price, quantity):
    current_time = get_current_ist_time()
    trade_entry = {"time": current_time, "symbol": symbol, "price": sold_price, "type": "SELL"}
    df_trades = _read_csv_or_empty(TRADES_FILE, ["time", "symbol", "price", "type"])
    df_trades = pd.concat([df_trades, pd.DataFrame([trade_entry])], ignore_index=True)
    df_trades.to_csv(TRADES_FILE, index=False)

    active_df = _read_csv_or_empty(ACTIVE_FILE, ["symbol", "buy_price", "quantity", "buy_time"])
    active_df = active_df[active_df["symbol"].astype(str).str.upper() != str(symbol).upper()]
    active_df.to_csv(ACTIVE_FILE, index=False)

    sold_df = _read_csv_or_empty(SOLD_FILE, ["symbol", "buy_price", "sold_price", "quantity", "profit", "sold_time"])
    profit = (float(sold_price) - float(buy_price)) * int(quantity)
    new_sold = {
        "symbol": symbol,
        "buy_price": buy_price,
        "sold_price": sold_price,
        "quantity": quantity,
        "profit": round(profit, 2),
        "sold_time": current_time,
    }
    sold_df = pd.concat([sold_df, pd.DataFrame([new_sold])], ignore_index=True)
    sold_df.to_csv(SOLD_FILE, index=False)

    cooling_df = _read_csv_or_empty(COOLING_FILE, ["symbol", "time"])
    cooling_df = cooling_df[cooling_df["symbol"].astype(str).str.upper() != str(symbol).upper()]
    cooling_df = pd.concat([cooling_df, pd.DataFrame([{"symbol": symbol, "time": current_time}])], ignore_index=True)
    cooling_df.to_csv(COOLING_FILE, index=False)
