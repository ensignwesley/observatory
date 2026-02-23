#!/usr/bin/env python3
"""
Observatory Checker — periodic health monitor.

Runs every 5 minutes via systemd timer.
  - Checks all targets (HTTP, optional Host override for localhost TLS)
  - Writes time-series results to SQLite
  - Computes rolling z-score anomaly detection against trailing 1-hour window
  - Writes backward-compat data.json for existing /status/ page
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

ANOMALY_Z         = 2.0    # z-score threshold for anomaly flag
ANOMALY_WINDOW_S  = 3600   # trailing window in seconds (1 hour)
ANOMALY_MIN_SAMP  = 5      # minimum samples before flagging

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
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


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
    now_ts  = int(time.time())
    now_iso = datetime.now(timezone.utc).isoformat()

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
