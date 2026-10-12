"""Thread-safe in-memory runtime status feed for the localhost dashboard."""
from collections import deque
from datetime import datetime
from threading import Lock
from zoneinfo import ZoneInfo

_lock = Lock()
_events = deque(maxlen=200)
_ist = ZoneInfo("Asia/Kolkata")


def publish_status(message, level="info"):
    event = {
        "time": datetime.now(_ist).strftime("%H:%M:%S"),
        "timestamp": datetime.now(_ist).isoformat(timespec="seconds"),
        "message": str(message)[:500],
        "level": level if level in {"info", "success", "warning", "error"} else "info",
    }
    with _lock:
        _events.appendleft(event)
    print(f"[{event['timestamp']}] [{event['level'].upper()}] {event['message']}", flush=True)
    return event


def recent_status(limit=50):
    with _lock:
        return list(_events)[:max(1, min(int(limit), 200))]
