#!/usr/bin/env python3
"""
Observatory Checker — periodic health monitor.

Runs every 5 minutes via systemd timer.
  - Checks all targets (HTTP, optional Host override for localhost TLS)
  - Writes time-series results to SQLite
  - Computes rolling z-score anomaly detection against trailing 1-hour window
  - Writes backward-compat data.json for existing /status/ page
  - Sends push alerts on state transitions (Telegram / webhook) — optional config
"""

import json
import math
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────

HOME      = Path.home()
DB_PATH   = HOME / 'observatory/observatory.db'
JSON_OUTS = [
    HOME / 'blog/public/status/data.json',
    HOME / 'blog/static/status/data.json',
]
ALERT_CONFIG_PATH = HOME / 'observatory/alert-config.json'

ANOMALY_Z         = 2.0    # z-score threshold for anomaly flag
ANOMALY_WINDOW_S  = 3600   # trailing window in seconds (1 hour)
ANOMALY_MIN_SAMP  = 5      # minimum samples before flagging

ALERT_THRESHOLD   = 2      # consecutive failures before DOWN alert fires

# Targets: url is the backend address; host overrides the HTTP Host header.
# Using localhost IPs avoids external DNS and tests the full local stack.
TARGETS = [
    {
        'slug':        'blog',
        'name':        'Blog',
        'description': 'Reports from the Frontline',
        'link':        'https://wesley.thesisko.com/',
        'url':         'https://127.0.0.1/',
        'host':        'wesley.thesisko.com',
        'threshold_ms': 500,
    },
    {
        'slug':        'dead-drop',
        'name':        'Dead Drop',
        'description': 'Zero-knowledge burn-after-read secret sharing',
        'link':        'https://wesley.thesisko.com/drop',
        'url':         'http://127.0.0.1:3001/drop',
        'threshold_ms': 300,
    },
    {
        'slug':        'dead-chat',
        'name':        'DEAD//CHAT',
        'description': 'Real-time WebSocket chat room',
        'link':        'https://wesley.thesisko.com/chat',
        'url':         'http://127.0.0.1:3002/chat',
        'threshold_ms': 300,
    },
    {
        'slug':        'status',
        'name':        'Status',
        'description': 'Service status page',
        'link':        'https://wesley.thesisko.com/status/',
        'url':         'https://127.0.0.1/status/',
        'host':        'wesley.thesisko.com',
        'threshold_ms': 500,
    },
    {
        'slug':        'observatory',
        'name':        'Observatory',
        'description': 'Uptime dashboard with anomaly detection',
        'link':        'https://wesley.thesisko.com/observatory/',
        'url':         'http://127.0.0.1:3003/observatory/',
        'threshold_ms': 500,
    },
    {
        'slug':        'pathfinder',
        'name':        'Pathfinder',
        'description': 'Interactive A* / Dijkstra / Greedy BFS visualizer',
        'link':        'https://wesley.thesisko.com/pathfinder/',
        'url':         'https://127.0.0.1/pathfinder/',
        'host':        'wesley.thesisko.com',
        'threshold_ms': 500,
    },
    {
        'slug':        'comments',
        'name':        'Comments',
        'description': 'Self-hosted blog comment API',
        'link':        'https://wesley.thesisko.com/posts/day-1-reports-from-the-frontline/#comments',
        'url':         'http://127.0.0.1:3004/comments/health',
        'threshold_ms': 300,
    },
    {
        'slug':        'forth',
        'name':        'Forth REPL',
        'description': 'Stack-based Forth interpreter — RFC 6455 WebSocket server',
        'link':        'https://wesley.thesisko.com/forth/',
        'url':         'http://127.0.0.1:3005/forth/',
        'threshold_ms': 300,
    },
    {
        'slug':        'lisp',
        'name':        'Lisp REPL',
        'description': 'Scheme-ish Lisp interpreter — in-browser eval, zero server',
        'link':        'https://wesley.thesisko.com/lisp/',
        'url':         'https://127.0.0.1/lisp/',
        'host':        'wesley.thesisko.com',
        'threshold_ms': 500,
    },
    {
        'slug':        'markov',
        'name':        'Markov REPL',
        'description': 'Markov chain captain\'s log generator — trains in-browser, zero server round-trip',
        'link':        'https://wesley.thesisko.com/markov/',
        'url':         'https://127.0.0.1/markov/',
        'host':        'wesley.thesisko.com',
        'threshold_ms': 500,
    },
]

# SSL context for localhost HTTPS (skip hostname verify — we're hitting 127.0.0.1)
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode    = ssl.CERT_NONE


# ── Database ───────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,       -- Unix timestamp (seconds)
    target      TEXT    NOT NULL,       -- target slug
    url         TEXT    NOT NULL,       -- URL checked
    ok          INTEGER NOT NULL,       -- 1 = healthy, 0 = down
    status_code INTEGER,                -- HTTP status (NULL if connection failed)
    response_ms REAL,                   -- ms (NULL if connection failed)
    zscore      REAL,                   -- z relative to trailing window (NULL if < min_samples)
    anomaly     INTEGER NOT NULL DEFAULT 0  -- 1 if |zscore| > threshold
);
CREATE INDEX IF NOT EXISTS idx_target_ts ON checks(target, ts);
CREATE INDEX IF NOT EXISTS idx_ts        ON checks(ts);

CREATE TABLE IF NOT EXISTS alert_state (
    slug                    TEXT    PRIMARY KEY,
    state                   TEXT    NOT NULL DEFAULT 'UP',   -- 'UP' or 'DOWN'
    consecutive_failures    INTEGER NOT NULL DEFAULT 0,
    last_alerted_at         REAL,    -- Unix timestamp of last notification
    last_state_change_at    REAL     -- Unix timestamp of last UP/DOWN transition
);
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ── Alert config ───────────────────────────────────────────────────────────────

def load_alert_config() -> dict | None:
    """Load alert-config.json if it exists and alerting is enabled.
    Returns config dict or None if alerting is disabled / not configured.
    """
    if not ALERT_CONFIG_PATH.exists():
        return None
    try:
        cfg = json.loads(ALERT_CONFIG_PATH.read_text())
        if not cfg.get('alerting', {}).get('enabled', False):
            return None
        return cfg['alerting']
    except Exception as exc:
        print(f"[observatory] alert-config.json load error: {exc}")
        return None


# ── Alert dispatch ─────────────────────────────────────────────────────────────

def _send_telegram(token: str, chat_id: str, text: str):
    """Send a Telegram message via Bot API."""
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}).encode()
    req  = urllib.request.Request(url, data=body,
                                  headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if not result.get('ok'):
                print(f"[observatory] telegram error: {result}")
    except Exception as exc:
        print(f"[observatory] telegram send failed: {exc}")


def _send_webhook(url: str, method: str, payload: dict):
    """POST (or GET) a webhook with a JSON payload."""
    method = method.upper()
    body   = json.dumps(payload).encode()
    req    = urllib.request.Request(url, data=body,
                                    headers={'Content-Type': 'application/json'},
                                    method=method)
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as exc:
        print(f"[observatory] webhook send failed: {exc}")


def dispatch_alert(cfg: dict, tgt: dict, new_state: str,
                   consecutive_failures: int, down_since: float | None):
    """Fire configured alert channels for a state transition."""
    name = tgt['name']
    link = tgt['link']
    now  = time.time()

    if new_state == 'DOWN':
        emoji   = '🔴'
        subject = f"{emoji} {name} — DOWN"
        detail  = f"Unreachable after {consecutive_failures} consecutive failures."
        body    = f"{subject}\n{detail}\n{link}"
    else:  # UP (recovery)
        emoji   = '🟢'
        subject = f"{emoji} {name} — UP (recovered)"
        if down_since:
            minutes = int((now - down_since) / 60)
            detail  = f"Was down approximately {minutes} min."
        else:
            detail = "Service restored."
        body = f"{subject}\n{detail}\n{link}"

    # Telegram
    tg = cfg.get('channels', {}).get('telegram', {})
    if tg.get('token') and tg.get('chat_id'):
        _send_telegram(tg['token'], tg['chat_id'], body)
        print(f"[observatory] alert → telegram: {subject}")

    # Webhook
    wh = cfg.get('channels', {}).get('webhook', {})
    if wh.get('url'):
        _send_webhook(
            wh['url'],
            wh.get('method', 'POST'),
            {
                'service':    name,
                'slug':       tgt['slug'],
                'state':      new_state,
                'message':    body,
                'link':       link,
                'timestamp':  datetime.now(timezone.utc).isoformat(),
            }
        )
        print(f"[observatory] alert → webhook: {subject}")


# ── Alert state machine ────────────────────────────────────────────────────────

def update_alert_state(conn, tgt: dict, ok: bool,
                       alert_cfg: dict | None, now_ts: int):
    """Update per-target alert state and fire transitions when warranted.

    State machine:
      UP  + failure  → increment consecutive_failures
                        if failures >= ALERT_THRESHOLD → flip to DOWN, alert
      UP  + success  → reset consecutive_failures to 0 (stay UP)
      DOWN + failure → stay DOWN (already alerted, no spam)
      DOWN + success → flip to UP, alert (recovery)
    """
    slug = tgt['slug']

    row = conn.execute(
        "SELECT state, consecutive_failures, last_state_change_at "
        "FROM alert_state WHERE slug=?", (slug,)
    ).fetchone()

    if row is None:
        # First time we've seen this slug — seed it
        conn.execute(
            "INSERT INTO alert_state (slug, state, consecutive_failures, "
            "last_alerted_at, last_state_change_at) VALUES (?,?,?,?,?)",
            (slug, 'UP', 0, None, None)
        )
        conn.commit()
        row = ('UP', 0, None)

    current_state, consec, last_change_at = row

    if current_state == 'UP':
        if ok:
            # Healthy — reset counter if it drifted up
            if consec > 0:
                conn.execute(
                    "UPDATE alert_state SET consecutive_failures=0 WHERE slug=?",
                    (slug,)
                )
                conn.commit()
        else:
            # Failed — increment counter
            new_consec = consec + 1
            if new_consec >= ALERT_THRESHOLD:
                # Flip to DOWN
                conn.execute(
                    "UPDATE alert_state SET state='DOWN', consecutive_failures=?, "
                    "last_alerted_at=?, last_state_change_at=? WHERE slug=?",
                    (new_consec, now_ts, now_ts, slug)
                )
                conn.commit()
                print(f"[observatory] ⚡ STATE CHANGE: {slug} UP → DOWN "
                      f"({new_consec} consecutive failures)")
                if alert_cfg:
                    dispatch_alert(alert_cfg, tgt, 'DOWN', new_consec, None)
            else:
                conn.execute(
                    "UPDATE alert_state SET consecutive_failures=? WHERE slug=?",
                    (new_consec, slug)
                )
                conn.commit()
                print(f"[observatory]   {slug} failure {new_consec}/{ALERT_THRESHOLD} "
                      f"(threshold not reached)")

    else:  # current_state == 'DOWN'
        if ok:
            # Recovery — flip back to UP
            conn.execute(
                "UPDATE alert_state SET state='UP', consecutive_failures=0, "
                "last_alerted_at=?, last_state_change_at=? WHERE slug=?",
                (now_ts, now_ts, slug)
            )
            conn.commit()
            print(f"[observatory] ✅ STATE CHANGE: {slug} DOWN → UP (recovered)")
            if alert_cfg:
                dispatch_alert(alert_cfg, tgt, 'UP', 0, last_change_at)
        else:
            # Still down — stay DOWN, no re-alert (anti-spam)
            new_consec = consec + 1
            conn.execute(
                "UPDATE alert_state SET consecutive_failures=? WHERE slug=?",
                (new_consec, slug)
            )
            conn.commit()


# ── Checks ─────────────────────────────────────────────────────────────────────

def check_target(target: dict):
    """Returns (ok, status_code, response_ms)."""
    url  = target['url']
    host = target.get('host')
    hdrs = {'Host': host} if host else {}

    t0 = time.monotonic()
    try:
        req  = urllib.request.Request(url, headers=hdrs)
        ctx  = _SSL if url.startswith('https') else None
        with urllib.request.urlopen(req, context=ctx, timeout=5) as resp:
            ms = (time.monotonic() - t0) * 1000
            return (resp.status < 500), resp.status, ms

    except urllib.error.HTTPError as exc:
        ms = (time.monotonic() - t0) * 1000
        return (exc.code < 500), exc.code, ms

    except Exception:
        ms = (time.monotonic() - t0) * 1000
        return False, None, None   # total failure — ms is meaningless


# ── Anomaly detection ──────────────────────────────────────────────────────────

def compute_anomaly(conn, slug: str, now_ts: int, current_ms: float):
    """Rolling z-score against trailing ANOMALY_WINDOW_S seconds of data.
    Returns (zscore: float|None, anomaly: int).
    """
    rows = conn.execute(
        """SELECT response_ms FROM checks
           WHERE target=? AND ts>=? AND response_ms IS NOT NULL
           ORDER BY ts""",
        (slug, now_ts - ANOMALY_WINDOW_S),
    ).fetchall()

    values = [r[0] for r in rows]

    if len(values) < ANOMALY_MIN_SAMP:
        return None, 0

    mean = sum(values) / len(values)
    std  = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))

    if std == 0:
        return 0.0, 0

    z       = (current_ms - mean) / std
    anomaly = 1 if abs(z) > ANOMALY_Z else 0
    return round(z, 3), anomaly


# ── Main ───────────────────────────────────────────────────────────────────────

def run():
    now_ts    = int(time.time())
    now_iso   = datetime.now(timezone.utc).isoformat()
    alert_cfg = load_alert_config()

    if alert_cfg:
        threshold = alert_cfg.get('threshold', ALERT_THRESHOLD)
        # Allow config to override the module-level constant
        globals()['ALERT_THRESHOLD'] = threshold
        print(f"[observatory] alerting ENABLED — threshold={threshold} failures")
    else:
        print("[observatory] alerting disabled (no alert-config.json or enabled:false)")

    conn    = open_db(DB_PATH)
    results = []

    for tgt in TARGETS:
        ok, status_code, response_ms = check_target(tgt)

        # Anomaly detection — only on successful checks with valid timing
        if ok and response_ms is not None:
            z, anomaly = compute_anomaly(conn, tgt['slug'], now_ts, response_ms)
        else:
            z, anomaly = None, 0

        conn.execute(
            """INSERT INTO checks
               (ts, target, url, ok, status_code, response_ms, zscore, anomaly)
               VALUES (?,?,?,?,?,?,?,?)""",
            (now_ts, tgt['slug'], tgt['url'],
             int(ok), status_code,
             round(response_ms, 1) if response_ms is not None else None,
             z, anomaly),
        )
        conn.commit()

        ms_str = f"{response_ms:6.0f}ms" if response_ms is not None else "  ---  "
        flag   = "  ⚠ ANOMALY" if anomaly else ""
        print(f"[observatory] {'✓' if ok else '✗'} {tgt['name']:<12} {ms_str}{flag}")

        # Alert state machine — runs regardless of alerting being enabled
        # (state is tracked even when disabled, so it's accurate when you enable it)
        update_alert_state(conn, tgt, ok, alert_cfg, now_ts)

        results.append({
            'name':        tgt['name'],
            'slug':        tgt['slug'],
            'description': tgt['description'],
            'link':        tgt['link'],
            'up':          ok,
            'status_code': status_code,
            'response_ms': int(response_ms) if response_ms is not None else None,
            'anomaly':     bool(anomaly),
            'zscore':      z,
            'checked_at':  now_iso,
        })

    conn.close()

    # Backward-compat JSON for /status/ page
    payload = json.dumps({
        'generated_at': now_iso,
        'services':     results,
        'all_up':       all(r['up'] for r in results),
    }, indent=2)

    for p in JSON_OUTS:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(payload)

    status = "all up" if all(r['up'] for r in results) else "DEGRADED"
    print(f"[observatory] {now_iso} {status}")


if __name__ == '__main__':
    run()
