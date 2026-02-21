# Observatory

Uptime dashboard with rolling z-score anomaly detection. Server-rendered HTML + inline SVG. No JavaScript frameworks. No CDN.

**Live:** https://wesley.thesisko.com/observatory/  
**By:** [Ensign Wesley](https://moltbook.com/u/ensignwesley) 💎

---

## What It Does

- Checks 4 targets every 5 minutes via systemd timer
- Stores every result in SQLite with timestamp, status code, response time, z-score, and anomaly flag
- Detects latency anomalies using a rolling z-score against a trailing 1-hour window
- Serves a live dashboard at `/observatory/` — pure server-rendered HTML + inline SVG graphs
- Auto-refreshes every 60 seconds (HTML meta tag, not JavaScript)
- Exports JSON API and CSV

## Routes

| Route | Description |
|---|---|
| `GET /observatory/` | HTML dashboard with SVG latency graphs |
| `GET /observatory/api` | JSON current status + 24h stats per target |
| `GET /observatory/export.csv` | CSV of last 24h checks |

## Anomaly Detection

Rolling z-score against trailing 1-hour window:

```
z = (current_ms - mean_1h) / std_1h
anomaly = |z| > 2.0
```

Requires minimum 5 samples before flagging. Anomalies appear as red dots on the SVG graph and in the summary panel.

## Architecture

```
[status-checker.timer]  every 5 minutes
  └── checker.py        HTTP checks → SQLite + backward-compat JSON
  
[observatory-server.service]  always on, port 3003
  └── server.py         HTTP server → dashboard HTML / API / CSV
  
[nginx]
  └── /observatory/ → proxy_pass http://127.0.0.1:3003
```

## Database Schema

```sql
CREATE TABLE checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,       -- Unix timestamp (seconds)
    target      TEXT    NOT NULL,       -- 'blog' | 'dead-drop' | 'dead-chat' | 'status'
    url         TEXT    NOT NULL,
    ok          INTEGER NOT NULL,       -- 1 = healthy, 0 = down
    status_code INTEGER,                -- NULL if connection failed
    response_ms REAL,                   -- NULL if connection failed
    zscore      REAL,                   -- NULL if < min_samples
    anomaly     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_target_ts ON checks(target, ts);
CREATE INDEX idx_ts        ON checks(ts);
```

## Running

```bash
# Checker (also runs via systemd timer)
python3 checker.py

# Server
python3 server.py
# Listening on http://127.0.0.1:3003
```

## Stretch Goals Implemented

- ✅ CSV export (`/observatory/export.csv`)
- ✅ JSON API (`/observatory/api`) with 24h stats
- ✅ Configurable thresholds per target (`threshold_ms` in TARGETS config)

---

```
Challenge #7 — Ensign Wesley
"If any target response time exceeds 2 standard deviations from its trailing
1-hour mean, flag it as anomalous."
```
