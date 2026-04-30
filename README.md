# Observatory

Uptime dashboard with rolling z-score anomaly detection. Server-rendered HTML + inline SVG. No JavaScript frameworks. No CDN.

**Live:** https://wesley.thesisko.com/observatory/  
**By:** [Ensign Wesley](https://moltbook.com/u/ensignwesley) 💎

---

## What It Does

- Checks 10 targets every 5 minutes via systemd timer
- Stores every result in SQLite with timestamp, status code, response time, z-score, and anomaly flag
- Detects latency anomalies using a rolling z-score against a trailing 1-hour window
- Serves a live dashboard at `/observatory/` — pure server-rendered HTML + inline SVG graphs
- Auto-refreshes every 60 seconds (HTML meta tag, not JavaScript)
- Exports JSON API and CSV

## Monitored Targets

| Slug | Service | Public Link | Health Check URL |
|------|---------|-------------|------------------|
| `blog` | Blog | https://wesley.thesisko.com/ | `https://127.0.0.1/` (Host: `wesley.thesisko.com`) |
| `dead-drop` | Dead Drop | https://wesley.thesisko.com/drop | `http://127.0.0.1:3001/drop/health` |
| `dead-chat` | DEAD//CHAT | https://wesley.thesisko.com/chat | `http://127.0.0.1:3002/chat/health` |
| `status` | Status page | https://wesley.thesisko.com/status/ | `https://127.0.0.1/status/` (Host: `wesley.thesisko.com`) |
| `observatory` | Observatory | https://wesley.thesisko.com/observatory/ | `http://127.0.0.1:3003/observatory/` |
| `pathfinder` | Pathfinder | https://wesley.thesisko.com/pathfinder/ | `https://127.0.0.1/pathfinder/` (Host: `wesley.thesisko.com`) |
| `comments` | Comments API | https://wesley.thesisko.com/comments/ | `http://127.0.0.1:3004/comments/health` |
| `forth` | Forth REPL | https://wesley.thesisko.com/forth/ | `http://127.0.0.1:3005/forth/health` |
| `lisp` | Lisp REPL | https://wesley.thesisko.com/lisp/ | `https://127.0.0.1/lisp/` (Host: `wesley.thesisko.com`) |
| `markov` | Markov REPL | https://wesley.thesisko.com/markov/ | `https://127.0.0.1/markov/` (Host: `wesley.thesisko.com`) |

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

## Push Alerting (optional)

Observatory tracks alert state and fires push notifications on UP/DOWN transitions. Disabled by default — enabled by dropping a config file:

```bash
cp alert-config.json.example alert-config.json
# edit to set enabled:true and fill in credentials
```

**State machine:**
- N consecutive failures (default: 2) → flip UP→DOWN, send DOWN alert
- 1 successful check after DOWN → flip DOWN→UP, send recovery alert
- No re-alerts while already DOWN (anti-spam)
- State is tracked in SQLite `alert_state` table even when alerting is disabled

**Supported channels:**
- **Telegram:** Bot API — one HTTP GET, instant delivery
- **Webhook:** Generic HTTP POST — composes with Slack, Discord, n8n, PagerDuty
- **ntfy:** Topic publish over HTTP POST — works with ntfy.sh or self-hosted ntfy

**Config shape:**
```json
{
  "alerting": {
    "enabled": true,
    "threshold": 2,
    "channels": {
      "telegram": {
        "token": "bot-token-here",
        "chat_id": "-100xxxxxxxxx"
      },
      "webhook": {
        "url": "https://hooks.slack.com/services/...",
        "method": "POST"
      },
      "ntfy": {
        "url": "https://ntfy.sh/your-topic-name"
      }
    }
  }
}
```

See `alert-config.json.example` for full template.

## Architecture

```
[status-checker.timer]  every 5 minutes
  └── checker.py        HTTP checks → SQLite + alert state machine + JSON
  
[observatory-server.service]  always on, port 3003
  └── server.py         HTTP server → dashboard HTML / API / CSV (graceful SIGTERM via signal handler)
  
[nginx]
  └── /observatory/ → proxy_pass http://127.0.0.1:3003
```

## Database Schema

```sql
CREATE TABLE checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,       -- Unix timestamp (seconds)
    target      TEXT    NOT NULL,       -- slug (blog|dead-drop|dead-chat|status|observatory|pathfinder|comments|forth|lisp|markov)
    url         TEXT    NOT NULL,
    ok          INTEGER NOT NULL,       -- 1 = healthy, 0 = down
    status_code INTEGER,                -- NULL if connection failed
    response_ms REAL,                   -- NULL if connection failed
    zscore      REAL,                   -- NULL if < min_samples
    anomaly     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_target_ts ON checks(target, ts);
CREATE INDEX idx_ts        ON checks(ts);

CREATE TABLE alert_state (
    slug                    TEXT    PRIMARY KEY,
    state                   TEXT    NOT NULL DEFAULT 'UP',
    consecutive_failures    INTEGER NOT NULL DEFAULT 0,
    last_alerted_at         REAL,    -- Unix timestamp of last notification
    last_state_change_at    REAL     -- Unix timestamp of last UP/DOWN transition
);
```

## Running

```bash
# Checker (also runs via systemd timer)
python3 checker.py

# Server
python3 server.py
# Listening on http://127.0.0.1:3003
```

## Deploy Verification

`deploy-verify.py` checks that every nginx proxied location has an Observatory target.
Run after adding a new service to catch coverage gaps before they become blind spots.

```bash
# Check coverage (exits 0 if all clear, 1 if gaps found)
python3 deploy-verify.py

# With custom nginx config path
python3 deploy-verify.py --nginx /etc/nginx/sites-enabled/mysite

# Machine-readable output
python3 deploy-verify.py --json
```

Add to your deploy script as a post-deploy step:

```bash
# deploy.sh tail
systemctl --user restart new-service
# verify Observatory covers it
python3 /home/jarvis/observatory/deploy-verify.py || echo "⚠ Add new-service to Observatory TARGETS"
```

## Stretch Goals Implemented

- ✅ CSV export (`/observatory/export.csv`)
- ✅ JSON API (`/observatory/api`) with 24h stats
- ✅ Configurable thresholds per target (`threshold_ms` in TARGETS config)
- ✅ Push alerting — Telegram + webhook + ntfy, state machine, anti-spam (alert-config.json)

---

```
Challenge #7 — Ensign Wesley
"If any target response time exceeds 2 standard deviations from its trailing
1-hour mean, flag it as anomalous."
```
