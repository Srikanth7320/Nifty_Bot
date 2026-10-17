"""Paper-trading engine for the NSE bot.

This version of the uploaded project uses Yahoo Finance for quotes. It does not
implement Upstox; do not label a scan as Upstox unless a real Upstox fetcher is
added and successfully returns usable data.
"""
import os
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
try:
    import yfinance as yf
except ImportError:
    yf = None
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
SCAN_INTERVAL_MINUTES = max(1, int(os.getenv("SCAN_INTERVAL_MINUTES", "15")))

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
SYMBOLS_FILE = os.path.join(DATA_DIR, "nse_equity_symbols.csv")
ACTIVE_FILE = os.path.join(DATA_DIR, "active_stocks.csv")
SOLD_FILE = os.path.join(DATA_DIR, "sold_stocks.csv")
TRADES_FILE = os.path.join(DATA_DIR, "trades_history.csv")
COOLING_FILE = os.path.join(DATA_DIR, "cooling_period.csv")
SOP_EXTERNAL_FILE = os.getenv("SOP_EXTERNAL_CHECKS_FILE", os.path.join(DATA_DIR, "sop_external_checks.csv"))
TRADING_CAPITAL_INR = max(0.0, float(os.getenv("TRADING_CAPITAL_INR", "100000")))
MAX_RISK_PER_TRADE_PCT = min(1.5, max(0.1, float(os.getenv("MAX_RISK_PER_TRADE_PCT", "1.5"))))
SOP_STOP_MODEL = os.getenv("SOP_STOP_MODEL", "ATR").strip().upper()
MAX_BUYS_PER_SCAN = max(1, int(os.getenv("MAX_BUYS_PER_SCAN", "1")))
MAX_OPEN_POSITIONS = max(1, int(os.getenv("MAX_OPEN_POSITIONS", "15")))
COOLING_HOURS = max(1, int(os.getenv("COOLING_PERIOD_HOURS", "48")))
MIN_AVG_DAILY_TURNOVER_INR = max(0.0, float(os.getenv("MIN_AVG_DAILY_TURNOVER_INR", "20000000")))
MIN_AVG_DAILY_VOLUME = max(0.0, float(os.getenv("MIN_AVG_DAILY_VOLUME", "10000")))
RSI_MAX_BUY_THRESHOLD = float(os.getenv("RSI_MAX_BUY_THRESHOLD", "68"))

ACTIVE_COLUMNS = ["symbol", "buy_price", "quantity", "buy_time"]
SOLD_COLUMNS = ["symbol", "buy_price", "sold_price", "quantity", "profit", "sold_time"]
COOLING_COLUMNS = ["symbol", "time"]
TRADE_COLUMNS = ["time", "symbol", "price", "type"]

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


def refresh_nse_equity_symbols():
    """Validate the cached NSE equity universe without overwriting it.

    The VM already has data/nse_equity_symbols.csv. This function deliberately
    preserves that file rather than replacing it with the smaller Nifty 500 list.
    It validates the cache; it does not download a fresh NSE symbol list.
    """
    if not os.path.exists(SYMBOLS_FILE) or os.path.getsize(SYMBOLS_FILE) == 0:
        print(f"NSE equity symbol file is missing or empty: {SYMBOLS_FILE}")
        return False

    symbols_df = _read_csv_or_empty(SYMBOLS_FILE, ["Symbol"])
    if symbols_df.empty:
        print(f"NSE equity symbol file is invalid or empty: {SYMBOLS_FILE}")
        return False

    symbol_col = "Symbol" if "Symbol" in symbols_df.columns else symbols_df.columns[0]
    symbols = symbols_df[symbol_col].dropna().astype(str).str.strip()
    symbols = symbols[symbols != ""]
    if symbols.empty:
        print(f"NSE equity symbol file has no usable symbols: {SYMBOLS_FILE}")
        return False

    print(f"Validated {symbols.nunique()} unique NSE equity symbols from {SYMBOLS_FILE}")
    return True


def refresh_nifty500_symbols():
    """Backward-compatible name; validates the configured NSE symbol cache."""
    return refresh_nse_equity_symbols()


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
    """Fetch daily history plus a recent intraday quote from Yahoo Finance.

    Yahoo is unofficial and may be delayed. Missing/stale intraday data makes a
    candidate unusable for new entries; it is never replaced with yesterday's close.
    """
    global LAST_QUOTE_PROVIDER, LAST_QUOTE_TIME
    clean_symbol = str(symbol).strip().upper()
    ticker_symbol = clean_symbol if clean_symbol.endswith(".NS") else f"{clean_symbol}.NS"
    if yf is None:
        print("Yahoo Finance unavailable: yfinance package is not installed")
        return None
    try:
        ticker = yf.Ticker(ticker_symbol)
        hist = ticker.history(period="1y", interval="1d", auto_adjust=False)
        if hist is None or hist.empty:
            print(f"Yahoo Finance returned insufficient daily history for {clean_symbol}")
            return None
        # Daily indicators must use completed candles only; the current day's partial
        # daily bar is not allowed into MACD/RSI/SMA/base calculations.
        idx = pd.DatetimeIndex(hist.index)
        if idx.tz is not None:
            idx = idx.tz_convert(IST).tz_localize(None)
        hist.index = idx
        if is_market_open():
            today_ist = datetime.now(IST).date()
            hist = hist.loc[[stamp.date() < today_ist for stamp in hist.index]]
        if len(hist) < 150:
            print(f"Yahoo Finance returned insufficient completed daily history for {clean_symbol}")
            return None
        required = {"Open", "Close", "Low", "High", "Volume"}
        if not required.issubset(hist.columns):
            print(f"Yahoo Finance response missing required OHLCV columns for {clean_symbol}")
            return None
        hist = hist.dropna(subset=["Open", "Close", "Low", "High"])
        if len(hist) < 150:
            return None
        current_price = float(hist["Close"].iloc[-1])
        intraday = pd.DataFrame()
        quote_interval = os.getenv("YFINANCE_QUOTE_INTERVAL", "5m")
        max_age = max(5, int(os.getenv("YFINANCE_MAX_QUOTE_AGE_MINUTES", "30")))
        if is_market_open():
            try:
                intraday = ticker.history(period="5d", interval=quote_interval, auto_adjust=False)
                if intraday is not None and not intraday.empty and "Close" in intraday:
                    intraday = intraday.dropna(subset=["Close"])
                    latest_ts = pd.Timestamp(intraday.index[-1])
                    now_ts = pd.Timestamp.now(tz=IST)
                    if latest_ts.tzinfo is None:
                        latest_ts = latest_ts.tz_localize(IST)
                    else:
                        latest_ts = latest_ts.tz_convert(IST)
                    age_minutes = (now_ts - latest_ts).total_seconds() / 60
                    if age_minutes < -2 or age_minutes > max_age:
                        print(f"Skipping {clean_symbol}: Yahoo intraday quote is stale ({age_minutes:.1f} min)")
                        return None
                    current_price = float(intraday["Close"].iloc[-1])
                else:
                    print(f"Skipping {clean_symbol}: no recent Yahoo intraday quote")
                    return None
            except Exception as quote_exc:
                print(f"Skipping {clean_symbol}: intraday quote unavailable ({type(quote_exc).__name__})")
                return None
        low_52w = float(hist["Low"].tail(252).min())
        high_52w = float(hist["High"].tail(252).max())
        rsi = calculate_rsi(hist)
        if not (current_price > 0 and pd.notna(rsi) and pd.notna(low_52w) and pd.notna(high_52w)):
            return None
        LAST_QUOTE_PROVIDER = "Yahoo Finance daily + intraday (unofficial)"
        LAST_QUOTE_TIME = get_current_ist_time()
        return {"symbol": clean_symbol.removesuffix(".NS"), "price": current_price,
                "low_52w": low_52w, "high_52w": high_52w, "rsi": rsi,
                "history": hist, "intraday": intraday, "provider": LAST_QUOTE_PROVIDER}
    except Exception as exc:
        print(f"Yahoo Finance quote error for {clean_symbol}: {type(exc).__name__}")
        return None


def run_api_health_checks(include_rss=True):
    """Check required integrations before startup or before the trading day.

    Google News RSS is supplementary, so an RSS outage is reported but does not
    make the required integrations unhealthy. Returns (all_required_ok, results).
    """
    results = {}
    # Quote provider health: require a recent response with usable OHLCV data.
    try:
        if yf is None:
            raise RuntimeError("yfinance package is not installed")
        ticker = yf.Ticker("RELIANCE.NS")
        hist = ticker.history(period="5d", interval="1d", auto_adjust=False)
        daily_ok = bool(hist is not None and not hist.empty and "Close" in hist.columns and hist["Close"].dropna().size)
        if is_market_open():
            intraday = ticker.history(period="1d", interval=os.getenv("YFINANCE_QUOTE_INTERVAL", "5m"), auto_adjust=False)
            intraday_ok = bool(intraday is not None and not intraday.empty and "Close" in intraday.columns and intraday["Close"].dropna().size)
            if intraday_ok:
                latest = pd.Timestamp(intraday.index[-1])
                if latest.tzinfo is None: latest = latest.tz_localize(IST)
                else: latest = latest.tz_convert(IST)
                age = (pd.Timestamp.now(tz=IST) - latest).total_seconds() / 60
                intraday_ok = -2 <= age <= max(5, int(os.getenv("YFINANCE_MAX_QUOTE_AGE_MINUTES", "30")))
            results["Yahoo Finance"] = daily_ok and intraday_ok
        else:
            results["Yahoo Finance"] = daily_ok
    except Exception as exc:
        results["Yahoo Finance"] = False
        print(f"Startup health check Yahoo Finance failed: {type(exc).__name__}")

    if NEWS_API_KEY:
        try:
            r = requests.get("https://newsapi.org/v2/everything", params={"q": "NSE India", "pageSize": 1, "apiKey": NEWS_API_KEY}, timeout=10)
            r.raise_for_status()
            results["NewsAPI"] = r.json().get("status") == "ok"
        except Exception as exc:
            results["NewsAPI"] = False
            print(f"API health check NewsAPI failed: {type(exc).__name__}")
    else:
        results["NewsAPI"] = False

    if GEMINI_API_KEY and client is not None:
        try:
            response = client.models.generate_content(model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"), contents="Reply with exactly: HEALTH_OK")
            results["Gemini"] = "HEALTH_OK" in (getattr(response, "text", "") or "").upper()
        except Exception as exc:
            results["Gemini"] = False
            print(f"API health check Gemini failed: {type(exc).__name__}")
    else:
        results["Gemini"] = False

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=10)
            r.raise_for_status()
            bot_ok = bool(r.json().get("ok"))
            chat = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getChat", params={"chat_id": TELEGRAM_CHAT_ID}, timeout=10)
            chat.raise_for_status()
            results["Telegram"] = bot_ok and bool(chat.json().get("ok"))
        except Exception as exc:
            results["Telegram"] = False
            print(f"API health check Telegram failed: {type(exc).__name__}")
    else:
        results["Telegram"] = False

    if include_rss:
        _, rss_status = fetch_google_news_rss("RELIANCE")
        results["Google News RSS (supplementary)"] = not rss_status.startswith("Google News RSS unavailable")

    required = [results.get(name, False) for name in ("Yahoo Finance", "NewsAPI", "Gemini", "Telegram")]
    return all(required), results


def report_api_health(results, prefix="API health check"):
    details = "; ".join(f"{name}: {'OK' if ok else 'FAILED'}" for name, ok in results.items())
    print(f"[{prefix}] {details}")
    try:
        from bot_status import publish_status
        publish_status(f"{prefix}: {details}", "success" if all(results.get(n, False) for n in ("Yahoo Finance", "NewsAPI", "Gemini", "Telegram")) else "warning")
    except Exception:
        pass
    return details


def fetch_google_news_rss(symbol):
    """Fetch Google News RSS headlines as supplementary context, never as proof
    that the required NewsAPI check succeeded. Returns (headlines, status).
    """
    query = quote_plus(f'"{symbol}" (NSE OR shares OR stock OR company)')
    url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        response = requests.get(url, headers={"User-Agent": "NSE-Momentum-PaperBot/1.0"}, timeout=10)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        headlines = []
        for item in root.findall(".//item")[:8]:
            title = (item.findtext("title") or "").strip()
            published = (item.findtext("pubDate") or "").strip()
            source = (item.findtext("source") or "Google News RSS").strip()
            if title and title.lower() != "[removed]":
                headlines.append({"title": title, "published": published, "source": source, "provider": "Google News RSS"})
        return headlines, f"Google News RSS returned {len(headlines)} headline(s)"
    except Exception as exc:
        status = f"Google News RSS unavailable ({type(exc).__name__})"
        print(f"Google News RSS request failed for {symbol}: {type(exc).__name__}")
        return [], status


def fetch_news_and_fundamentals(symbol):
    """Query NewsAPI and Google News RSS on every candidate, then ask Gemini.

    RSS is supplementary only. With REQUIRE_AI_NEWS_CONFIRMATION enabled, a
    successful NewsAPI response with at least one usable headline AND a clear
    Gemini PASS are required. RSS-only evidence can never satisfy that gate.
    Returns (passed, explanation).
    """
    require_confirmation = os.getenv("REQUIRE_AI_NEWS_CONFIRMATION", "true").strip().lower() in {
        "1", "true", "yes", "on"
    }
    newsapi_headlines = []
    newsapi_status = "NewsAPI disabled"
    newsapi_ok = False

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
            newsapi_ok = True
            for article in body.get("articles", []):
                title = (article.get("title") or "").strip()
                published = (article.get("publishedAt") or "").strip()
                source = ((article.get("source") or {}).get("name") or "NewsAPI").strip()
                if title and title.lower() != "[removed]":
                    newsapi_headlines.append({"title": title, "published": published, "source": source, "provider": "NewsAPI"})
            newsapi_status = f"NewsAPI returned {len(newsapi_headlines)} headline(s)"
        except Exception as exc:
            newsapi_status = f"NewsAPI unavailable ({type(exc).__name__})"
            print(f"NewsAPI request failed for {symbol}: {type(exc).__name__}")
    else:
        newsapi_status = "NewsAPI unavailable (API key not configured)"

    # Always fetch RSS, even when NewsAPI worked; it is a separate, supplementary source.
    rss_headlines, rss_status = fetch_google_news_rss(symbol)

    # Merge and deduplicate on normalized title while retaining provenance.
    merged = []
    seen = set()
    for item in newsapi_headlines + rss_headlines:
        key = " ".join(item["title"].casefold().split())
        if key and key not in seen:
            seen.add(key)
            merged.append(item)
    headline_lines = [
        f"[{item['provider']} | {item['source']} | {item['published'] or 'date unavailable'}] {item['title']}"
        for item in merged
    ]

    source_status = f"{newsapi_status}; {rss_status} (RSS is supplementary, not a NewsAPI substitute)"
    if not GEMINI_API_KEY or client is None:
        reason = f"Gemini unavailable/not configured; {source_status}"
        return (not require_confirmation, "Technical-only mode: " + reason) if not require_confirmation else (False, "ANALYSIS INCOMPLETE: " + reason)

    prompt = f"""You are a cautious research assistant supporting a PAPER-TRADING screen for NSE stock {symbol}.
Use ONLY the supplied evidence. Do not claim to have live market data, browse the web, or invent financial metrics.
Source status: {source_status}
NewsAPI succeeded: {newsapi_ok}
Usable NewsAPI headlines: {len(newsapi_headlines)}
Google News RSS headlines are supplementary and MUST NOT be treated as proof that NewsAPI succeeded.
Recent headlines (source is identified per item):
{chr(10).join(headline_lines) if headline_lines else "No usable headlines supplied."}

Evaluate whether supplied headlines show material positive/negative risks or insufficient evidence.
Return exactly this format:
STATUS: PASS or FAIL
SENTIMENT: positive, neutral, negative, or insufficient
RISK_FLAGS: comma-separated flags or none
REASON: one concise sentence
PASS only when evidence is sufficient and there is no clear material adverse signal. If evidence is insufficient, return FAIL.
"""
    gemini_pass = False
    answer = ""
    try:
        response = client.models.generate_content(model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"), contents=prompt)
        answer = (getattr(response, "text", None) or "").strip()
        upper = answer.upper()
        gemini_pass = bool(answer and "STATUS: PASS" in upper and "STATUS: FAIL" not in upper)
        if not gemini_pass:
            print(f"Gemini did not confirm {symbol}: {(answer[:200] or 'empty response')}")
    except Exception as exc:
        print(f"Gemini evaluation failed for {symbol}: {type(exc).__name__}")
        answer = f"Gemini request failed ({type(exc).__name__})"

    # Critical: RSS and Gemini cannot override an unavailable/empty NewsAPI result
    # while strict confirmation is enabled. This prevents an RSS-only false pass.
    newsapi_gate_pass = newsapi_ok and bool(newsapi_headlines)
    if require_confirmation and not newsapi_gate_pass:
        return False, f"ANALYSIS INCOMPLETE: required NewsAPI check not satisfied ({newsapi_status}); {rss_status}; Gemini: {answer[:250]}"
    if require_confirmation and not gemini_pass:
        return False, f"ANALYSIS INCOMPLETE: Gemini did not provide an unambiguous PASS; {source_status}; {answer[:250]}"
    if not require_confirmation:
        return True, f"AI/news confirmation bypassed by REQUIRE_AI_NEWS_CONFIRMATION=false; {source_status}; Gemini: {answer[:180]}"
    return True, f"{source_status}; Gemini confirmed: {answer[:350]}"



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
    """Count trading sessions held, counting the entry session as session one."""
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
        dates = sorted(set(x.date() for x in idx))
        # Include the entry date even when today's partial candle is not in history.
        sessions = [d for d in dates if d >= entry_date]
        if entry_date not in sessions:
            sessions.insert(0, entry_date)
        current_date = pd.Timestamp.now(tz=IST).date()
        # Count the current session when entry was on the latest available candle;
        # do not add it for an entry date that is newer than the history snapshot.
        if current_date in dates and current_date not in sessions:
            sessions.append(current_date)
        elif dates and entry_date == dates[-1] and current_date > dates[-1] and current_date not in sessions:
            sessions.append(current_date)
        return max(1, sum(1 for d in sessions if entry_date <= d <= current_date))
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


SOP_EXTERNAL_COLUMNS = [
    "symbol", "as_of_date", "circuit_band_pct", "promoter_pledge_pct",
    "institutional_status", "surveillance_stage", "sector_tailwind_status",
    "catalyst_status", "catalyst_type", "contract_value_pct_revenue",
    "yoy_profit_growth_pct", "opm_expansion_bps", "guidance_status",
    "policy_catalyst_status", "board_meeting_date", "equity_dilution_status",
    "delivery_volume_ratio", "notes",
]


def load_sop_external_check(symbol, as_of=None):
    """Read manually verified NSE/company-data checks. Missing values stay UNKNOWN.

    Yahoo/Google RSS cannot establish circuit bands, pledging, FII/DII holdings,
    surveillance status, board dates, dilution, or NSE delivery data.
    """
    as_of = as_of or datetime.now(IST).date().isoformat()
    frame = _read_csv_or_empty(SOP_EXTERNAL_FILE, SOP_EXTERNAL_COLUMNS)
    if frame.empty or "symbol" not in frame:
        return {"status": "UNKNOWN", "reason": f"No external SOP checks in {SOP_EXTERNAL_FILE}"}
    rows = frame[frame["symbol"].astype(str).str.upper() == str(symbol).upper()]
    if rows.empty:
        return {"status": "UNKNOWN", "reason": f"No external SOP row for {symbol}"}
    rows = rows.copy()
    rows["as_of_date"] = rows.get("as_of_date", "").astype(str)
    rows = rows[rows["as_of_date"] == as_of]
    if rows.empty:
        return {"status": "UNKNOWN", "reason": f"No current-date external SOP row for {symbol} ({as_of})"}
    return rows.iloc[-1].to_dict()


def evaluate_sop_external_filters(symbol, as_of=None):
    """Fail closed on unavailable non-price SOP requirements; return audit detail."""
    row = load_sop_external_check(symbol, as_of)
    if row.get("status") == "UNKNOWN":
        return False, [row["reason"]]
    failures = []
    def val(key):
        value = row.get(key, "")
        return "" if pd.isna(value) else str(value).strip()
    try:
        band = float(val("circuit_band_pct"))
        if band not in (10.0, 20.0): failures.append("Circuit band must be 10% or 20%")
    except ValueError:
        failures.append("Circuit band UNKNOWN")
    try:
        pledge = float(val("promoter_pledge_pct"))
        if pledge >= 10: failures.append("Promoter pledge must be below 10%")
    except ValueError:
        failures.append("Promoter pledge UNKNOWN")
    if val("institutional_status").upper() != "PASS": failures.append("FII/DII holding status is not PASS/UNKNOWN")
    stage = val("surveillance_stage").upper().replace("STAGE ", "")
    if not stage: failures.append("ASM/GSM surveillance stage UNKNOWN")
    elif stage not in ("NONE", "0", "1", "NOT LISTED", "NA", "N/A"):
        try:
            if float(stage) >= 2: failures.append("ASM/GSM Stage 2+ is disallowed")
        except ValueError:
            failures.append("ASM/GSM status is unrecognized")
    if val("sector_tailwind_status").upper() != "PASS": failures.append("Sector/thematic tailwind not verified")
    catalyst_type = val("catalyst_type").upper()
    catalyst_pass = False
    if catalyst_type in ("ORDER", "CONTRACT", "ORDER_BOOK"):
        try:
            catalyst_pass = float(val("contract_value_pct_revenue")) >= 10.0
            if not catalyst_pass: failures.append("Order/contract must be at least 10% of prior FY revenue")
        except ValueError:
            failures.append("Contract value as % of prior FY revenue UNKNOWN")
    elif catalyst_type in ("EARNINGS", "RESULTS"):
        try:
            profit_growth = float(val("yoy_profit_growth_pct"))
            opm_bps = float(val("opm_expansion_bps"))
            catalyst_pass = profit_growth >= 20.0 and opm_bps >= 150.0
            if profit_growth < 20.0: failures.append("Earnings catalyst requires >=20% YoY net-profit growth")
            if opm_bps < 150.0: failures.append("Earnings catalyst requires >=150 bps OPM expansion")
            if val("guidance_status").upper() not in ("PASS", "UPGRADED", "CLEAR"):
                failures.append("Management guidance/backlog status not verified")
        except ValueError:
            failures.append("Earnings growth/OPM expansion UNKNOWN")
    elif catalyst_type in ("POLICY", "REGULATORY"):
        catalyst_pass = val("policy_catalyst_status").upper() == "PASS"
        if not catalyst_pass: failures.append("Policy/regulatory catalyst not verified")
    else:
        failures.append("Catalyst type must be ORDER, EARNINGS, or POLICY")
    if val("catalyst_status").upper() != "PASS" or not catalyst_pass:
        failures.append("Qualifying positive catalyst not verified against SOP thresholds")
    if val("equity_dilution_status").upper() not in ("CLEAR", "PASS", "NONE"):
        failures.append("Equity dilution status is not clear")
    meeting = val("board_meeting_date")
    if meeting:
        try:
            meeting_date = pd.Timestamp(meeting).date()
            today = pd.Timestamp(as_of).date()
            if meeting_date >= today:
                trading_days = len(pd.bdate_range(today + timedelta(days=1), meeting_date))
                if trading_days <= 3:
                    failures.append("Scheduled earnings board meeting within 3 trading days")
        except Exception:
            failures.append("Board meeting date is unrecognized")
    else:
        failures.append("Board meeting date UNKNOWN")
    try:
        delivery_ratio = float(val("delivery_volume_ratio"))
        if delivery_ratio < float(os.getenv("SOP_MIN_DELIVERY_VOLUME_RATIO", "1.2")):
            failures.append("Delivery-volume surge below configured minimum")
    except ValueError:
        failures.append("NSE delivery-volume ratio UNKNOWN")
    return not failures, failures or ["All supplied external SOP checks passed"]


def evaluate_sop_technical_setup(hist, current_price, rsi, intraday=None, now=None):
    """Strict daily/weekly SOP setup gate. Returns (passed, setup, reasons, metrics)."""
    reasons, metrics = [], {}
    now = now or datetime.now(IST)
    if hist is None or len(hist) < 160:
        return False, "NONE", ["At least 160 daily candles are required for the 30-week SMA"], {}
    needed = {"Open", "High", "Low", "Close", "Volume"}
    if not needed.issubset(hist.columns):
        return False, "NONE", ["OHLCV columns missing"], {}
    h = hist.dropna(subset=["Open", "High", "Low", "Close", "Volume"]).copy()
    close = h["Close"].astype(float); high = h["High"].astype(float); low = h["Low"].astype(float)
    vol = h["Volume"].astype(float); op = h["Open"].astype(float)
    if len(h) < 150: return False, "NONE", ["Insufficient clean daily candles"], {}
    weekly = close.resample("W-FRI").last().dropna()
    sma30w = weekly.rolling(30).mean().iloc[-1] if len(weekly) >= 30 else float("nan")
    ema9 = close.ewm(span=9, adjust=False).mean(); ema20 = close.ewm(span=20, adjust=False).mean()
    sma50 = close.rolling(50).mean(); sma150 = close.rolling(150).mean()
    metrics.update({"ema9": float(ema9.iloc[-1]), "ema20": float(ema20.iloc[-1]),
                    "sma50": float(sma50.iloc[-1]), "sma30w": float(sma30w) if pd.notna(sma30w) else None})
    if pd.isna(sma30w) or current_price <= sma30w: reasons.append("Price must be above 30-week SMA")
    if not (current_price > ema9.iloc[-1] > ema20.iloc[-1] > sma50.iloc[-1]): reasons.append("Price > EMA9 > EMA20 > SMA50 stack failed")
    if not (ema9.iloc[-1] > ema9.iloc[-4] and ema20.iloc[-1] > ema20.iloc[-4]): reasons.append("EMA9 and EMA20 must slope upward")
    if not (55 <= float(rsi) <= 68): reasons.append("RSI must be in 55–68 breakout sweet spot")
    if float(rsi) >= 75: reasons.append("RSI >= 75: do not chase initial spike")
    # MACD(12,26,9): line above signal and histogram expanding above zero.
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    signal = macd.ewm(span=9, adjust=False).mean(); histo = macd - signal
    if not (macd.iloc[-1] > signal.iloc[-1] and histo.iloc[-1] > 0 and histo.iloc[-1] > histo.iloc[-2]):
        reasons.append("MACD bullish crossover/expanding positive histogram failed")
    # Bollinger squeeze followed by expansion outside upper band.
    mid = close.rolling(20).mean(); std = close.rolling(20).std(ddof=0)
    upper, lower = mid + 2*std, mid - 2*std
    bandwidth = (upper-lower) / mid.replace(0, float("nan"))
    squeeze_floor = bandwidth.iloc[-25:-5].min() if len(bandwidth) >= 25 else float("nan")
    squeeze_seen = pd.notna(squeeze_floor) and bandwidth.iloc[-5:-1].min() <= squeeze_floor * 1.15
    if not (squeeze_seen and current_price > upper.iloc[-1] and upper.iloc[-1] > upper.iloc[-2] and lower.iloc[-1] < lower.iloc[-2]):
        reasons.append("Bollinger squeeze/upper-band expansion not confirmed")
    avg_vol = vol.iloc[-21:-1].mean()
    # Latest completed daily candle must be the breakout candle; stale historical volume isn't a trigger.
    prior_resistance = float(high.iloc[-31:-1].max())
    breakout = current_price > prior_resistance
    breakout_volume_ratio = float(vol.iloc[-1] / avg_vol) if avg_vol > 0 else 0
    metrics.update({"prior_resistance": prior_resistance, "avg_volume_20": float(avg_vol), "daily_volume_ratio": breakout_volume_ratio})
    # A fresh intraday volume ratio is estimated from summed bars since 09:15.
    intraday_ratio = 0.0; green_reversal = False
    if intraday is not None and not intraday.empty and "Volume" in intraday:
        try:
            idx = pd.DatetimeIndex(intraday.index)
            if idx.tz is not None: idx = idx.tz_convert(IST).tz_localize(None)
            today = pd.Timestamp(now.date())
            daybars = intraday.loc[idx.date == today.date()]
            if not daybars.empty:
                intraday_ratio = float(daybars["Volume"].sum() / avg_vol) if avg_vol > 0 else 0
                last = daybars.iloc[-1]
                green_reversal = float(last["Close"]) > float(last.get("Open", last["Close"]))
        except Exception: pass
    metrics["intraday_volume_ratio"] = intraday_ratio
    # Approximate stair-step structure: last 20 closes and swing lows should rise.
    if close.iloc[-1] <= close.iloc[-6] or low.iloc[-1] <= low.iloc[-6]:
        reasons.append("Higher-high/higher-low stair-step structure not confirmed")
    # 2–6 week base: 10–30 sessions, compressed range <= 12%; breakout above resistance.
    base_ok = False
    for n in range(10, 31):
        base = h.iloc[-(n+1):-1]
        if len(base) == n and float(base["High"].max()) > 0:
            width = (float(base["High"].max()) - float(base["Low"].min())) / float(base["Low"].min())
            ranges = (base["High"] - base["Low"]).astype(float)
            contraction = len(ranges) >= 10 and float(ranges.tail(5).mean()) <= float(ranges.iloc[:-5].mean())
            if width <= float(os.getenv("SOP_MAX_BASE_WIDTH_PCT", "12"))/100 and contraction and current_price > float(base["High"].max()):
                base_ok = True; break
    # Trigger candle wick ratio under 15% of candle range and bullish close.
    candle_range = float(high.iloc[-1] - low.iloc[-1])
    candle_body = abs(float(close.iloc[-1] - op.iloc[-1]))
    upper_wick = float(high.iloc[-1] - max(op.iloc[-1], close.iloc[-1]))
    wick_ok = candle_range > 0 and candle_body > 0 and upper_wick < 0.15 * candle_body and close.iloc[-1] > op.iloc[-1]
    # Entry A: shelf breakout + live cumulative volume at least 2x average, after opening volatility.
    entry_time_ok = now.time() >= dtime(10, 0) and now.time() <= dtime(14, 30)
    entry_a = breakout and base_ok and entry_time_ok and intraday_ratio >= float(os.getenv("SOP_BREAKOUT_VOLUME_MULTIPLE", "2.0")) and (wick_ok or green_reversal)
    # Entry B: prior RSI spike, 2–4 day pullback with falling volume, retest of rising EMA9 / shelf, green reversal.
    rsi_series = pd.Series([calculate_rsi(h.iloc[:i+1]) for i in range(max(15, len(h)-8), len(h))], index=h.index[max(15, len(h)-8):])
    prior_spike = bool((rsi_series.iloc[:-3] > 75).any()) if len(rsi_series) > 3 else False
    pullback = len(vol) >= 5 and all(close.iloc[-i] < close.iloc[-i-1] for i in range(2, 5)) and vol.iloc[-1] < vol.iloc[-2] < vol.iloc[-3]
    support_touch = abs(current_price-float(ema9.iloc[-1]))/max(current_price, 0.01) <= 0.015 or abs(current_price-prior_resistance)/max(current_price,0.01) <= 0.015
    entry_b = prior_spike and pullback and support_touch and green_reversal and entry_time_ok
    setup = "A" if entry_a else ("B" if entry_b else "NONE")
    if setup == "NONE": reasons.append("Neither approved entry type A (high-volume shelf breakout) nor B (low-volume EMA retest) passed")
    if intraday_ratio < 2.0 and setup != "B": reasons.append("Breakout-day volume must be at least 2x 20-day average")
    if not (0 < calculate_atr(h) < current_price): reasons.append("ATR(14) unavailable/invalid")
    # Only remove generic trigger-related reason if the setup itself passes; all other SOP checks remain hard gates.
    if setup != "NONE": reasons = [r for r in reasons if not r.startswith("Neither approved entry type")]
    return not reasons, setup, reasons or [f"SOP technical setup {setup} passed"], metrics


def calculate_sop_stop(entry_price, hist):
    atr = calculate_atr(hist)
    if SOP_STOP_MODEL == "FIXED":
        stop = entry_price * (1 - float(os.getenv("SOP_FIXED_STOP_PCT", "4.5"))/100)
    else:
        stop = entry_price - 1.5 * atr
        swing_low = float(hist["Low"].tail(10).min())
        ema20 = float(hist["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
        # Model B's stated alternatives: use the tighter valid support stop, but below entry.
        candidates = [stop, swing_low * 0.995, ema20 * 0.995]
        valid = [x for x in candidates if 0 < x < entry_price]
        stop = max(valid) if valid else entry_price * 0.95
    return float(stop), float(atr)


def calculate_sop_position_size(entry_price, stop_price):
    risk_cash = TRADING_CAPITAL_INR * (MAX_RISK_PER_TRADE_PCT / 100.0)
    risk_per_share = max(0.01, entry_price - stop_price)
    qty = int(risk_cash // risk_per_share)
    # Do not exceed deployable capital even if stop distance is tiny.
    qty = min(qty, int(TRADING_CAPITAL_INR // max(entry_price, 0.01)))
    return max(0, qty)


def evaluate_emergency_exit(hist):
    """Return emergency exit reason when SOP's technical overrides trigger."""
    if hist is None or len(hist) < 30: return None
    c = hist["Close"].astype(float); o = hist["Open"].astype(float); h = hist["High"].astype(float); l = hist["Low"].astype(float); v = hist["Volume"].astype(float)
    rsi_now = calculate_rsi(hist)
    rsi_prev = calculate_rsi(hist.iloc[:-1])
    if len(c) >= 6:
        # RSI divergence: new 5-session high while RSI fails to confirm recent RSI high.
        recent_rsi = [calculate_rsi(hist.iloc[:i]) for i in range(max(15,len(hist)-10),len(hist))]
        if c.iloc[-1] > c.iloc[-6:-1].max() and len(recent_rsi)>=5 and rsi_now < max(recent_rsi[:-1]):
            return "Emergency exit: bearish RSI divergence"
    if rsi_prev > 80 and rsi_now < 70 and v.iloc[-1] >= 1.2*v.iloc[-21:-1].mean():
        return "Emergency exit: RSI exhaustion reversal"
    macd = c.ewm(span=12, adjust=False).mean()-c.ewm(span=26, adjust=False).mean(); sig=macd.ewm(span=9,adjust=False).mean()
    if macd.iloc[-2] >= sig.iloc[-2] and macd.iloc[-1] < sig.iloc[-1] and macd.iloc[-1] > 0:
        return "Emergency exit: bearish MACD cross in positive territory"
    avg_v = v.iloc[-21:-1].mean()
    if c.iloc[-1] < o.iloc[-1] and v.iloc[-1] >= 2*avg_v and c.iloc[-1] < min(c.iloc[-2], c.iloc[-3], c.iloc[-4]):
        return "Emergency exit: high-volume institutional distribution candle"
    return None


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


def _read_csv(path, columns=None):
    """Compatibility wrapper used by app/dashboard code."""
    return _read_csv_or_empty(path, columns or [])


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
    """Apply the mandatory SOP gates to paper entries and manage staged exits."""
    now = datetime.now(IST)
    if not is_market_open():
        set_scan_status("Market closed", "Scheduled scan skipped outside NSE market hours")
        return {"status": "skipped", "reason": "market closed"}
    scan_started = get_current_ist_time()
    quote_providers = set(); quote_successes = 0; quote_failures = 0; buy_count = 0; sell_count = 0
    if not refresh_nse_equity_symbols():
        set_scan_status("No data", "NSE equity symbol list is missing or invalid; scan skipped", scan_started)
        return {"status": "incomplete", "reason": "symbol list invalid"}
    symbols_df = _read_csv_or_empty(SYMBOLS_FILE, ["Symbol"])
    if symbols_df.empty:
        set_scan_status("No data", "Symbol list is empty; scan not performed", scan_started)
        return {"status": "incomplete", "reason": "symbol list empty"}
    symbol_col = "Symbol" if "Symbol" in symbols_df.columns else symbols_df.columns[0]
    all_symbols = list(dict.fromkeys(str(x).strip().upper() for x in symbols_df[symbol_col].dropna() if str(x).strip()))
    symbols, _ = rotating_scan_batch(all_symbols, MAX_SCAN_SYMBOLS)
    cooling_df = _read_csv_or_empty(COOLING_FILE, ["symbol", "time"])
    cooling_map = {}
    if not cooling_df.empty:
        for _, cooling_row in cooling_df.iterrows():
            try:
                sold_at = pd.Timestamp(cooling_row["time"])
                if sold_at.tzinfo is None: sold_at = sold_at.tz_localize(IST)
                else: sold_at = sold_at.tz_convert(IST)
                if (now - sold_at.to_pydatetime()).total_seconds() < COOLING_HOURS * 3600:
                    cooling_map[str(cooling_row["symbol"]).upper()] = str(cooling_row["time"])
            except Exception:
                # Malformed cooldown timestamps block re-entry rather than being ignored.
                cooling_map[str(cooling_row.get("symbol", "")).upper()] = str(cooling_row.get("time", ""))
    active_df = _read_csv_or_empty(ACTIVE_FILE, ACTIVE_COLUMNS)
    active_symbols = set(active_df["symbol"].astype(str).str.upper()) if not active_df.empty else set()

    # Manage current positions using the SOP's stop, partial targets, runner and emergency exits.
    for _, row in active_df.copy().iterrows():
        sym = str(row.get("symbol", "")).strip().upper()
        if not sym: continue
        try: buy = float(row["buy_price"]); qty = int(row.get("quantity", 0))
        except (TypeError, ValueError, KeyError): continue
        data = fetch_stock_data_with_fallback(sym)
        if not data:
            quote_failures += 1; continue
        quote_successes += 1; quote_providers.add(data["provider"])
        price, hist = data["price"], data["history"]
        atr = calculate_atr(hist)
        stop = float(row.get("stop_loss", buy * 0.955)) if pd.notna(row.get("stop_loss", buy * 0.955)) else buy * 0.955
        target1 = float(row.get("target1", buy * 1.09)) if pd.notna(row.get("target1", buy * 1.09)) else buy * 1.09
        target1_hit = str(row.get("target1_hit", "false")).lower() == "true"
        round_target_hit = str(row.get("round_target_hit", "false")).lower() == "true"
        peak = max(float(row.get("peak_price", buy) or buy), price)
        held = trading_sessions_held(hist, str(row.get("buy_time", get_current_ist_time())))
        emergency = evaluate_emergency_exit(hist)
        ema20 = float(hist["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
        ema9 = float(hist["Close"].ewm(span=9, adjust=False).mean().iloc[-1])
        parabolic = ((price / buy - 1) >= 0.15 and held <= 5)
        exit_reason = None
        if price <= stop: exit_reason = "SOP stop-loss breached"
        elif emergency: exit_reason = emergency
        elif (parabolic and (float(hist["Close"].iloc[-1]) < ema9 or float(hist["Close"].iloc[-1]) < float(hist["Low"].iloc[-2]))) or (not parabolic and float(hist["Close"].iloc[-1]) < ema20):
            exit_reason = "SOP trailing exit: completed daily close below EMA9/EMA20 or prior-session low"
        if exit_reason:
            record_sell_trade(sym, buy, price, qty, reason=exit_reason)
            sell_count += 1
            send_telegram(f"🔴 *SOP PAPER EXIT — 100%*\nStock: `{sym}`\nEntry: ₹{buy:.2f}\nPrice: ₹{price:.2f}\nQuantity: {qty}\nReason: {exit_reason}")
            continue
        # Step 2: book half at 8–10%; set remaining stop to breakeven.
        if not target1_hit and price >= target1 and qty >= 4:
            partial = max(1, qty // 2); remaining = qty - partial
            record_partial_sell(sym, buy, price, partial, "SOP Target 1: 50% profit booked")
            update_active_position(sym, quantity=remaining, stop_loss=buy, target1_hit="true", peak_price=peak)
            sell_count += 1
            send_telegram(f"🟡 *SOP TARGET 1 — PARTIAL PAPER SELL*\n{sym}: sold {partial}; remaining {remaining}. Stop moved to breakeven ₹{buy:.2f}.")
            continue
        # Step 3: sell an additional 25% of original quantity near a round number with rejection wick.
        original_qty = int(float(row.get("original_quantity", qty) or qty))
        if target1_hit and not round_target_hit and qty >= 2:
            round_num = round(price / 50.0) * 50.0
            near_round = round_num > buy and abs(price-round_num)/max(price, 0.01) <= 0.01
            last = hist.iloc[-1]; candle_range = float(last["High"]-last["Low"])
            rejection = candle_range > 0 and float(last["High"]-max(last["Open"],last["Close"]))/candle_range >= 0.35 and float(last["Volume"]) >= 1.5*float(hist["Volume"].iloc[-21:-1].mean())
            if near_round and rejection:
                partial = min(qty-1, max(1, int(round(original_qty*0.25))))
                record_partial_sell(sym, buy, price, partial, "SOP round-number rejection: additional 25% of original position")
                update_active_position(sym, quantity=qty-partial, round_target_hit="true", peak_price=peak)
                sell_count += 1
                send_telegram(f"🟡 *SOP ROUND-NUMBER PAPER SELL*\n{sym}: sold {partial} on rejection near ₹{round_num:.2f}.")
                continue
        update_active_position(sym, peak_price=peak)
        if held >= 15:
            record_sell_trade(sym, buy, price, qty, reason="SOP maximum 15-session holding period")
            sell_count += 1
            send_telegram(f"🔴 *SOP TIME EXIT*\n{sym}: exited after {held} trading sessions.")

    active_df = _read_csv_or_empty(ACTIVE_FILE, ACTIVE_COLUMNS)
    active_symbols = set(active_df["symbol"].astype(str).str.upper()) if not active_df.empty else set()
    if len(active_symbols) >= MAX_OPEN_POSITIONS:
        print(f"SOP position cap reached ({len(active_symbols)}/{MAX_OPEN_POSITIONS}); no new entries")
        symbols = []
    for sym in symbols:
        if buy_count >= MAX_BUYS_PER_SCAN: break
        if sym in cooling_map or sym in active_symbols: continue
        # New entry only after opening volatility and before 14:30; setup evaluator enforces this too.
        if not (dtime(10, 0) <= now.time() <= dtime(14, 30)): continue
        data = fetch_stock_data_with_fallback(sym)
        if not data:
            quote_failures += 1; continue
        quote_successes += 1; quote_providers.add(data["provider"])
        price, low52, high52, rsi, hist = data["price"], data["low_52w"], data["high_52w"], data["rsi"], data["history"]
        if not (MIN_PRICE <= price <= MAX_PRICE) or high52 <= low52: continue
        # Liquidity requirements are a hard filter.
        avg_volume = float(hist["Volume"].iloc[-21:-1].mean())
        avg_turnover = float((hist["Close"].iloc[-21:-1] * hist["Volume"].iloc[-21:-1]).mean())
        if avg_volume < MIN_AVG_DAILY_VOLUME or avg_turnover < MIN_AVG_DAILY_TURNOVER_INR: continue
        tech_pass, setup, tech_reasons, metrics = evaluate_sop_technical_setup(hist, price, rsi, data.get("intraday"), now)
        if not tech_pass:
            print(f"SOP REJECT {sym}: {'; '.join(tech_reasons)}")
            continue
        external_pass, external_reasons = evaluate_sop_external_filters(sym, now.date().isoformat())
        if not external_pass:
            print(f"SOP REJECT {sym}: external checks UNKNOWN/FAIL: {'; '.join(external_reasons)}")
            continue
        passed_ai, news_reason = fetch_news_and_fundamentals(sym)
        if not passed_ai:
            print(f"SOP REJECT {sym}: news confirmation: {news_reason}")
            continue
        stop, atr = calculate_sop_stop(price, hist)
        qty = calculate_sop_position_size(price, stop)
        if qty < 4:
            print(f"SOP REJECT {sym}: risk-based quantity {qty} is below 4 shares needed for staged 50%/25%/25% exits")
            continue
        target1 = price * (1.09 if SOP_STOP_MODEL == "FIXED" else 1 + 2*((price-stop)/price))
        target2 = price * (1.18 if SOP_STOP_MODEL == "FIXED" else 1 + 3*((price-stop)/price))
        record_buy_trade(sym, price, quantity=qty, stop_loss=stop, target1=target1, target2=target2,
                         entry_atr=atr, setup_type=setup, external_checks="PASS", news_status=news_reason[:240])
        active_symbols.add(sym); buy_count += 1
        send_telegram(f"🟢 *SOP PAPER BUY — SETUP {setup}*\nStock: `{sym}`\nEntry: ₹{price:.2f}\nQty: {qty}\nStop: ₹{stop:.2f}\nTarget 1: ₹{target1:.2f} (book 50%)\nTarget 2: ₹{target2:.2f}\nRisk: {MAX_RISK_PER_TRADE_PCT:.2f}% of ₹{TRADING_CAPITAL_INR:,.0f}\nRSI: {rsi:.1f}; ATR: {atr:.2f}\nTechnicals: {metrics}\nExternal checks: PASS\nNews: {news_reason[:200]}")
    if quote_providers:
        note = f"{quote_successes} usable quotes; {quote_failures} quote failures; {buy_count} buy(s), {sell_count} sell(s); SOP strict gates active"
        set_scan_status(" + ".join(sorted(quote_providers)), note, scan_started)
    else:
        set_scan_status("No usable quotes", f"{quote_failures} quote failure(s); no decisions made", scan_started)
    return {"status": "complete", "buys": buy_count, "sells": sell_count}

def record_buy_trade(symbol, price, quantity=1, stop_loss=None, target1=None, target2=None,
                     entry_atr=None, setup_type="legacy", external_checks="UNKNOWN", news_status=""):
    current_time = get_current_ist_time()
    trade_entry = {"time": current_time, "symbol": symbol, "price": price, "type": "BUY", "quantity": quantity,
                   "stop_loss": stop_loss, "target1": target1, "target2": target2, "setup_type": setup_type}
    df_trades = _read_csv_or_empty(TRADES_FILE, ["time", "symbol", "price", "type"])
    df_trades = pd.concat([df_trades, pd.DataFrame([trade_entry])], ignore_index=True); df_trades.to_csv(TRADES_FILE, index=False)
    active_columns = ACTIVE_COLUMNS + ["stop_loss", "target1", "target2", "target1_hit", "round_target_hit", "peak_price", "entry_atr", "original_quantity", "setup_type", "external_checks", "news_status"]
    active_df = _read_csv_or_empty(ACTIVE_FILE, active_columns)
    if symbol not in active_df["symbol"].astype(str).values:
        new_row = {"symbol": symbol, "buy_price": price, "quantity": quantity, "buy_time": current_time,
                   "stop_loss": stop_loss if stop_loss is not None else price*0.955,
                   "target1": target1 if target1 is not None else price*1.09,
                   "target2": target2 if target2 is not None else price*1.18,
                   "target1_hit": "false", "round_target_hit": "false", "peak_price": price,
                   "entry_atr": entry_atr, "original_quantity": quantity, "setup_type": setup_type,
                   "external_checks": external_checks, "news_status": news_status}
        active_df = pd.concat([active_df, pd.DataFrame([new_row])], ignore_index=True)
        active_df.to_csv(ACTIVE_FILE, index=False)


def update_active_position(symbol, **updates):
    active_df = _read_csv_or_empty(ACTIVE_FILE, ACTIVE_COLUMNS)
    for key in updates: 
        if key not in active_df.columns: active_df[key] = ""
    mask = active_df["symbol"].astype(str).str.upper() == str(symbol).upper()
    for key, value in updates.items(): active_df.loc[mask, key] = value
    active_df.to_csv(ACTIVE_FILE, index=False)


def record_partial_sell(symbol, buy_price, sold_price, quantity, reason):
    current_time = get_current_ist_time()
    trades = _read_csv_or_empty(TRADES_FILE, ["time", "symbol", "price", "type"])
    row = {"time": current_time, "symbol": symbol, "price": sold_price, "type": "PARTIAL_SELL", "quantity": quantity, "reason": reason}
    pd.concat([trades, pd.DataFrame([row])], ignore_index=True).to_csv(TRADES_FILE, index=False)
    sold = _read_csv_or_empty(SOLD_FILE, ["symbol", "buy_price", "sold_price", "quantity", "profit", "sold_time"])
    sold_row = {"symbol": symbol, "buy_price": buy_price, "sold_price": sold_price, "quantity": quantity,
                "profit": round((float(sold_price)-float(buy_price))*int(quantity),2), "sold_time": current_time, "reason": reason}
    pd.concat([sold, pd.DataFrame([sold_row])], ignore_index=True).to_csv(SOLD_FILE, index=False)

def record_sell_trade(symbol, buy_price, sold_price, quantity, reason="SOP exit"):
    current_time = get_current_ist_time()
    trade_entry = {"time": current_time, "symbol": symbol, "price": sold_price, "type": "SELL", "quantity": quantity, "reason": reason}
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


# Shared lock and atomic writer used by the Flask dashboard's paper portfolio edits.
import threading
import tempfile

DATA_LOCK = threading.RLock()

def _write_csv_atomic(dataframe, destination):
    """Write a DataFrame to CSV via a temporary file and atomic replacement."""
    destination = os.path.abspath(os.fspath(destination))
    directory = os.path.dirname(destination) or "."
    os.makedirs(directory, exist_ok=True)

    fd, temp_path = tempfile.mkstemp(
        prefix=".csv_update_", suffix=".tmp", dir=directory
    )
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
