"""Push notifications via ntfy (https://ntfy.sh) — dead-simple phone push.

Setup: pick an unguessable topic, put NTFY_TOPIC in .env, install the ntfy app (iOS/Android),
and subscribe to that topic. The backend publishes the message and it lands on your phone. No
topic set -> no-op (so it's safe to leave unconfigured). Self-host ntfy on the Pi later and point
NTFY_SERVER at it; nothing else changes.

We publish via ntfy's JSON API (a UTF-8 body posted to the server root with a "topic" field)
rather than the simpler header-based API, because the title/message routinely contain non-ASCII
characters (e.g. "·" and "→") and HTTP headers are ASCII-only — the header form silently failed on
every titled push. The send runs fire-and-forget on a daemon thread with a small retry so a
transient network blip doesn't drop the notification, and it never blocks the scan/monitor loops.
"""

import logging
import os
import threading
import time

import httpx

log = logging.getLogger(__name__)

# ntfy priority names -> the integer the JSON API expects (1 min .. 5 max).
_PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5}


def _topic() -> str:
    return os.environ.get("NTFY_TOPIC", "").strip()


def _server() -> str:
    return os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")


def send(message: str, title: str = "Bellwether", tags: str = "", priority: str = "default") -> None:
    """Push a notification to the configured ntfy topic. No-op if NTFY_TOPIC is unset."""
    topic = _topic()
    if not topic:
        return
    payload = {"topic": topic, "message": message, "title": title,
               "priority": _PRIORITY.get(priority, 3)}
    if tags:
        payload["tags"] = [t.strip() for t in tags.split(",") if t.strip()]  # ntfy renders these as emoji
    url = _server()  # JSON publishing posts to the server root, topic is in the body

    def _post() -> None:
        for attempt in range(3):
            try:
                r = httpx.post(url, json=payload, timeout=10)
                r.raise_for_status()
                return
            except Exception:
                if attempt == 2:
                    log.warning("ntfy push failed after retries", exc_info=True)
                else:
                    time.sleep(1.5 * (attempt + 1))

    threading.Thread(target=_post, daemon=True).start()
