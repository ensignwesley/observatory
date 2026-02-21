#!/usr/bin/env python3
"""
Observatory Server — uptime dashboard with anomaly detection.

Serves at http://127.0.0.1:3003 (nginx proxies /observatory/)
No JavaScript frameworks. No CDN. Pure server-rendered HTML + inline SVG.

Routes:
  GET /observatory/         → HTML dashboard
  GET /observatory/api      → JSON current status + 24h stats
  GET /observatory/export.csv → CSV of last 24h checks
"""

import csv
import io
import json
import math
import sqlite3
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DB_PATH = Path.home() / 'observatory/observatory.db'
PORT    = 3003

TARGETS = ['blog', 'dead-drop', 'dead-chat', 'status']
TARGET_NAMES = {
    'blog':      'Blog',
    'dead-drop': 'Dead Drop',
    'dead-chat': 'DEAD//CHAT',
    'status':    'Status',
}
TARGET_LINKS = {
    'blog':      'https://wesley.thesisko.com/',
    'dead-drop': 'https://wesley.thesisko.com/drop',
    'dead-chat': 'https://wesley.thesisko.com/chat',
    'status':    'https://wesley.thesisko.com/status/',
}

GRAPH_HOURS = 6     # hours of data to show in graph
CSV_HOURS   = 24    # hours for CSV export


# ── DB helpers ─────────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def latest_per_target(conn):
    """Most recent check for every target."""
    rows = {}
    for slug in TARGETS:
        r = conn.execute(
            "SELECT * FROM checks WHERE target=? ORDER BY ts DESC LIMIT 1", (slug,)
        ).fetchone()
        rows[slug] = dict(r) if r else None
    return rows


def graph_data(conn, slug: str, hours: int = GRAPH_HOURS):
    """Time-series data for the graph: (ts, response_ms, ok, anomaly)."""
    since = int(time.time()) - hours * 3600
    return conn.execute(
        """SELECT ts, response_ms, ok, anomaly FROM checks
           WHERE target=? AND ts>=? ORDER BY ts""",
        (slug, since),
    ).fetchall()


def uptime_stats(conn, slug: str, hours: int = 24):
    """Returns (total_checks, up_count, avg_ms, max_ms, anomaly_count)."""
    since = int(time.time()) - hours * 3600
    r = conn.execute(
        """SELECT
             COUNT(*)                                      AS total,
             SUM(ok)                                       AS up,
             AVG(CASE WHEN ok=1 THEN response_ms END)     AS avg_ms,
             MAX(CASE WHEN ok=1 THEN response_ms END)      AS max_ms,
             SUM(anomaly)                                  AS anomalies
           FROM checks WHERE target=? AND ts>=?""",
        (slug, since),
    ).fetchone()
    return dict(r)


def recent_anomalies(conn, hours: int = 1):
    """All anomaly=1 checks in the last `hours` hours."""
    since = int(time.time()) - hours * 3600
    return conn.execute(
        """SELECT target, ts, response_ms, zscore FROM checks
           WHERE anomaly=1 AND ts>=? ORDER BY ts DESC""",
        (since,),
    ).fetchall()


# ── SVG generation ─────────────────────────────────────────────────────────────

# Graph canvas constants
W, H               = 800, 175
PAD_L, PAD_R       = 55, 10
PAD_T, PAD_B       = 15, 35
GW                 = W - PAD_L - PAD_R   # graph width  = 735
GH                 = H - PAD_T - PAD_B   # graph height = 125


def _tx(ts, ts_min, ts_max):
    if ts_max == ts_min:
        return PAD_L + GW // 2
    return PAD_L + GW * (ts - ts_min) / (ts_max - ts_min)


def _ty(ms, ms_max):
    """SVG y: 0ms = bottom (PAD_T+GH), ms_max = top (PAD_T). Clamped."""
    if ms_max == 0:
        return PAD_T + GH // 2
    frac = min(ms / ms_max, 1.0)
    return PAD_T + GH - GH * frac


def make_svg(rows):
    """
    rows: list of sqlite3.Row with (ts, response_ms, ok, anomaly)
    Returns inline SVG string.
    """
    if not rows:
        return (
            f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
            f'style="width:100%;display:block">'
            f'<rect width="{W}" height="{H}" fill="#0d1520"/>'
            f'<text x="{W//2}" y="{H//2+5}" text-anchor="middle" '
            f'fill="#475569" font-size="13" font-family="monospace">No data yet — waiting for first check</text>'
            f'</svg>'
        )

    ts_min   = rows[0]['ts']
    ts_max   = rows[-1]['ts']
    if ts_max == ts_min:
        ts_max += 1

    ok_ms    = [r['response_ms'] for r in rows if r['ok'] and r['response_ms'] is not None]
    ms_max   = max(ok_ms) * 1.25 if ok_ms else 500
    ms_max   = max(ms_max, 10)

    lines = [
        f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" style="width:100%;display:block">',
        f'<rect width="{W}" height="{H}" fill="#0d1520"/>',
    ]

    # ── Y-axis grid + labels ────────────────────────────────────────────────────
    for pct in (0, 25, 50, 75, 100):
        ms_val = ms_max * (100 - pct) / 100
        y      = PAD_T + GH * pct / 100
        lines.append(
            f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W-PAD_R}" y2="{y:.1f}" '
            f'stroke="#1a2a3a" stroke-width="1"/>'
        )
        lines.append(
            f'<text x="{PAD_L-6}" y="{y+4:.1f}" text-anchor="end" '
            f'fill="#475569" font-size="9" font-family="monospace">{ms_val:.0f}</text>'
        )

    # ── X-axis: hourly tick marks ───────────────────────────────────────────────
    hour_s = 3600
    tick_ts = (ts_min // hour_s + 1) * hour_s
    while tick_ts <= ts_max:
        x     = _tx(tick_ts, ts_min, ts_max)
        label = datetime.fromtimestamp(tick_ts, tz=timezone.utc).strftime('%H:%M')
        lines.append(
            f'<line x1="{x:.1f}" y1="{PAD_T}" x2="{x:.1f}" y2="{PAD_T+GH}" '
            f'stroke="#1a2a3a" stroke-width="1" stroke-dasharray="3,5"/>'
        )
        lines.append(
            f'<text x="{x:.1f}" y="{PAD_T+GH+22}" text-anchor="middle" '
            f'fill="#475569" font-size="9" font-family="monospace">{label}</text>'
        )
        tick_ts += hour_s

    # ── Axes ────────────────────────────────────────────────────────────────────
    lines.append(
        f'<line x1="{PAD_L}" y1="{PAD_T}" x2="{PAD_L}" y2="{PAD_T+GH}" '
        f'stroke="#2dd4bf" stroke-width="1" opacity="0.3"/>'
    )
    lines.append(
        f'<line x1="{PAD_L}" y1="{PAD_T+GH}" x2="{W-PAD_R}" y2="{PAD_T+GH}" '
        f'stroke="#2dd4bf" stroke-width="1" opacity="0.3"/>'
    )

    # ── Latency line (OK checks only) ───────────────────────────────────────────
    ok_pts = [
        (r['ts'], r['response_ms'])
        for r in rows if r['ok'] and r['response_ms'] is not None
    ]
    if len(ok_pts) >= 2:
        pts = ' '.join(
            f"{_tx(ts, ts_min, ts_max):.1f},{_ty(ms, ms_max):.1f}"
            for ts, ms in ok_pts
        )
        lines.append(
            f'<polyline points="{pts}" fill="none" stroke="#2dd4bf" '
            f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
        )

    # ── Down markers (red ×) ────────────────────────────────────────────────────
    for r in rows:
        if not r['ok']:
            x  = _tx(r['ts'], ts_min, ts_max)
            y  = PAD_T + GH - 6
            d  = 4
            lines.append(
                f'<line x1="{x-d:.1f}" y1="{y-d:.1f}" x2="{x+d:.1f}" y2="{y+d:.1f}" '
                f'stroke="#f87171" stroke-width="2"/>'
            )
            lines.append(
                f'<line x1="{x+d:.1f}" y1="{y-d:.1f}" x2="{x-d:.1f}" y2="{y+d:.1f}" '
                f'stroke="#f87171" stroke-width="2"/>'
            )

    # ── Normal dots (teal, small) ───────────────────────────────────────────────
    for r in rows:
        if r['ok'] and r['response_ms'] is not None and not r['anomaly']:
            x = _tx(r['ts'], ts_min, ts_max)
            y = _ty(r['response_ms'], ms_max)
            lines.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2" fill="#2dd4bf"/>')

    # ── Anomaly dots (red, larger) ──────────────────────────────────────────────
    for r in rows:
        if r['anomaly'] and r['response_ms'] is not None:
            x = _tx(r['ts'], ts_min, ts_max)
            y = _ty(r['response_ms'], ms_max)
            lines.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#f87171" '
                f'stroke="#7f1d1d" stroke-width="1" opacity="0.9"/>'
            )

    # ── Y-axis label ─────────────────────────────────────────────────────────────
    cy = PAD_T + GH // 2
    lines.append(
        f'<text x="10" y="{cy}" text-anchor="middle" fill="#334155" '
        f'font-size="9" font-family="monospace" '
        f'transform="rotate(-90 10 {cy})">ms</text>'
    )

    lines.append('</svg>')
    return '\n'.join(lines)


# ── HTML generation ────────────────────────────────────────────────────────────

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:     #0a0e12;
  --bg2:    #0d1520;
  --bg3:    #111e2e;
  --teal:   #2dd4bf;
  --teal2:  #14b8a6;
  --text:   #e2e8f0;
  --muted:  #64748b;
  --border: #1a2a3a;
  --red:    #f87171;
  --green:  #4ade80;
  --font:   -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --mono:   "JetBrains Mono", "Fira Code", "Courier New", monospace;
}
body { background:var(--bg); color:var(--text); font-family:var(--font);
       line-height:1.6; min-height:100vh; }
a { color:var(--teal); text-decoration:none; }
a:hover { text-decoration:underline; }

.container { max-width:1100px; margin:0 auto; padding:0 1.5rem; }

header { border-bottom:1px solid var(--border); padding:1.5rem 0; }
.header-inner { display:flex; align-items:baseline; gap:1rem; flex-wrap:wrap; }
.header-title { font-size:1.5rem; font-weight:700; color:var(--text); }
.header-sub   { font-size:.8rem; color:var(--muted); font-family:var(--mono); }
.header-nav   { margin-left:auto; display:flex; gap:1.5rem; font-size:.85rem; }

.summary-bar {
  margin:1.5rem 0 1rem;
  padding:.75rem 1.25rem;
  border-radius:6px;
  border:1px solid var(--border);
  display:flex; align-items:center; gap:1rem; flex-wrap:wrap;
  font-family:var(--mono); font-size:.8rem;
}
.summary-bar.all-up  { border-color: rgba(74,222,128,.3); background:rgba(74,222,128,.05); }
.summary-bar.degraded { border-color: rgba(248,113,113,.3); background:rgba(248,113,113,.05); }
.status-dot { width:10px; height:10px; border-radius:50%; flex-shrink:0; }
.dot-up   { background:var(--green); box-shadow:0 0 6px var(--green); }
.dot-down { background:var(--red);   box-shadow:0 0 6px var(--red); }
.dot-warn { background:#fb923c;      box-shadow:0 0 6px #fb923c; }
.summary-ts { margin-left:auto; color:var(--muted); }

.anomaly-panel {
  background: rgba(248,113,113,.08);
  border:1px solid rgba(248,113,113,.3);
  border-left:4px solid var(--red);
  border-radius:4px;
  padding:1rem 1.25rem;
  margin-bottom:1.5rem;
  font-size:.85rem;
}
.anomaly-panel h3 { color:var(--red); font-size:.75rem; letter-spacing:.1em;
                     text-transform:uppercase; margin-bottom:.75rem; font-family:var(--mono); }
.anomaly-row { display:flex; gap:1rem; color:var(--text); margin-bottom:.3rem; flex-wrap:wrap; }
.anomaly-target { color:var(--red); font-family:var(--mono); font-weight:600; min-width:7rem; }
.anomaly-z  { color:var(--muted); font-family:var(--mono); }

.grid { display:grid; gap:1.25rem; }

.card {
  background:var(--bg2);
  border:1px solid var(--border);
  border-radius:6px;
  overflow:hidden;
}
.card-header {
  padding:.85rem 1.25rem;
  display:flex; align-items:center; gap:.75rem; flex-wrap:wrap;
  border-bottom:1px solid var(--border);
}
.card-name { font-weight:600; font-size:1rem; }
.card-link { font-size:.75rem; color:var(--muted); margin-left:auto; }
.badge {
  display:inline-block; padding:.2rem .55rem; border-radius:3px;
  font-size:.65rem; font-weight:700; text-transform:uppercase;
  letter-spacing:.08em; font-family:var(--mono);
}
.badge-up      { background:rgba(74,222,128,.12);  color:var(--green);  border:1px solid rgba(74,222,128,.3); }
.badge-down    { background:rgba(248,113,113,.12); color:var(--red);    border:1px solid rgba(248,113,113,.3); }
.badge-anomaly { background:rgba(248,113,113,.12); color:var(--red);    border:1px solid rgba(248,113,113,.3); }

.stats-row {
  padding:.6rem 1.25rem;
  display:flex; gap:2rem; flex-wrap:wrap;
  border-bottom:1px solid var(--border);
  font-family:var(--mono); font-size:.75rem;
}
.stat-label { color:var(--muted); margin-right:.3rem; }
.stat-val   { color:var(--text); font-weight:600; }
.stat-val.anomaly-val { color:var(--red); }

.svg-wrap { padding:.5rem 0 0; background:var(--bg3); }

footer { border-top:1px solid var(--border); padding:1.5rem 0;
         text-align:center; font-size:.8rem; color:var(--muted); margin-top:2rem; }
.footer-links { margin-top:.4rem; display:flex; justify-content:center; gap:1.5rem; }
"""


def pct_bar(pct: float) -> str:
    color = '#4ade80' if pct >= 99 else '#fb923c' if pct >= 90 else '#f87171'
    return (
        f'<svg width="60" height="8" style="vertical-align:middle;margin-right:.4rem">'
        f'<rect width="60" height="8" rx="2" fill="#1a2a3a"/>'
        f'<rect width="{pct * .6:.1f}" height="8" rx="2" fill="{color}"/>'
        f'</svg>'
    )


def format_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def render_dashboard(conn) -> str:
    now        = int(time.time())
    latest     = latest_per_target(conn)
    anomalies  = recent_anomalies(conn, hours=1)
    all_up     = all(v and v['ok'] for v in latest.values())
    has_anomaly= len(anomalies) > 0

    # ── Summary bar ─────────────────────────────────────────────────────────────
    if all_up and not has_anomaly:
        bar_class, dot_class, bar_text = 'all-up',   'dot-up',   'ALL SYSTEMS OPERATIONAL'
    elif all_up and has_anomaly:
        bar_class, dot_class, bar_text = 'degraded', 'dot-warn', 'OPERATIONAL — LATENCY ANOMALIES DETECTED'
    else:
        bar_class, dot_class, bar_text = 'degraded', 'dot-down', 'DEGRADED — SERVICE OUTAGE DETECTED'

    summary_html = (
        f'<div class="summary-bar {bar_class}">'
        f'<div class="status-dot {dot_class}"></div>'
        f'<span>{bar_text}</span>'
        f'<span class="summary-ts">checked {format_ts(now)}</span>'
        f'</div>'
    )

    # ── Anomaly panel ────────────────────────────────────────────────────────────
    anomaly_html = ''
    if anomalies:
        rows_html = ''
        for a in anomalies:
            t = format_ts(a['ts'])
            z = f"{a['zscore']:+.2f}σ" if a['zscore'] is not None else '—'
            ms = f"{a['response_ms']:.0f}ms" if a['response_ms'] else '—'
            rows_html += (
                f'<div class="anomaly-row">'
                f'<span class="anomaly-target">{TARGET_NAMES.get(a["target"], a["target"])}</span>'
                f'<span>{ms}</span>'
                f'<span class="anomaly-z">{z} from mean</span>'
                f'<span class="anomaly-z">{t}</span>'
                f'</div>'
            )
        anomaly_html = (
            f'<div class="anomaly-panel">'
            f'<h3>⚠ Latency Anomalies — Last Hour</h3>'
            f'{rows_html}'
            f'</div>'
        )

    # ── Target cards ─────────────────────────────────────────────────────────────
    cards = []
    for slug in TARGETS:
        cur   = latest.get(slug)
        stats = uptime_stats(conn, slug)
        gdata = graph_data(conn, slug)

        name  = TARGET_NAMES[slug]
        link  = TARGET_LINKS[slug]

        # Status badge
        if cur is None:
            badge = '<span class="badge badge-down">no data</span>'
        elif not cur['ok']:
            badge = '<span class="badge badge-down">down</span>'
        elif cur['anomaly']:
            badge = '<span class="badge badge-anomaly">anomaly</span>'
        else:
            badge = '<span class="badge badge-up">up</span>'

        # Stats row
        total   = stats['total'] or 0
        up_cnt  = stats['up']    or 0
        uptime  = (up_cnt / total * 100) if total else 0
        avg_ms  = stats['avg_ms']
        max_ms  = stats['max_ms']
        anomaly_cnt = stats['anomalies'] or 0

        cur_ms = cur['response_ms'] if cur else None
        ms_display = f"{cur_ms:.0f}ms" if cur_ms is not None else '—'

        anomaly_cls = ' anomaly-val' if (cur and cur['anomaly']) else ''

        stats_html = (
            f'<div class="stats-row">'
            f'<span><span class="stat-label">current</span><span class="stat-val{anomaly_cls}">{ms_display}</span></span>'
            f'<span><span class="stat-label">24h avg</span><span class="stat-val">{f"{avg_ms:.0f}ms" if avg_ms else "—"}</span></span>'
            f'<span><span class="stat-label">24h max</span><span class="stat-val">{f"{max_ms:.0f}ms" if max_ms else "—"}</span></span>'
            f'<span>{pct_bar(uptime)}<span class="stat-label">uptime</span><span class="stat-val">{uptime:.1f}%</span></span>'
            f'<span><span class="stat-label">anomalies</span><span class="stat-val{" anomaly-val" if anomaly_cnt else ""}">{anomaly_cnt}</span></span>'
            f'</div>'
        )

        svg = make_svg(gdata)

        cards.append(
            f'<div class="card">'
            f'<div class="card-header">'
            f'  <span class="card-name">{name}</span>'
            f'  {badge}'
            f'  <a href="{link}" class="card-link" target="_blank" rel="noopener">↗ {link}</a>'
            f'</div>'
            f'{stats_html}'
            f'<div class="svg-wrap">{svg}</div>'
            f'</div>'
        )

    grid_html = f'<div class="grid">{"".join(cards)}</div>'

    # ── Full page ────────────────────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Observatory — wesley.thesisko.com</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <div class="container">
    <div class="header-inner">
      <span class="header-title">☽ Observatory</span>
      <span class="header-sub">wesley.thesisko.com · last {GRAPH_HOURS}h · refreshes every 60s</span>
      <nav class="header-nav">
        <a href="/">Blog</a>
        <a href="/observatory/api">API</a>
        <a href="/observatory/export.csv">CSV</a>
        <a href="/status/">Status</a>
      </nav>
    </div>
  </div>
</header>

<div class="container" style="padding-top:0;padding-bottom:2rem">
  {summary_html}
  {anomaly_html}
  {grid_html}
</div>

<footer>
  <div class="container">
    <div>Observatory — server-rendered HTML + inline SVG · no JS frameworks · no CDN</div>
    <div class="footer-links">
      <a href="/observatory/api">JSON API</a>
      <a href="/observatory/export.csv">Export CSV</a>
      <a href="https://github.com/ensignwesley">GitHub</a>
    </div>
  </div>
</footer>
</body>
</html>"""


def render_api(conn) -> dict:
    now    = int(time.time())
    latest = latest_per_target(conn)
    result = {}
    for slug in TARGETS:
        cur   = latest.get(slug)
        stats = uptime_stats(conn, slug)
        result[slug] = {
            'name':        TARGET_NAMES[slug],
            'link':        TARGET_LINKS[slug],
            'current': {
                'ok':          bool(cur['ok'])          if cur else None,
                'status_code': cur['status_code']       if cur else None,
                'response_ms': cur['response_ms']       if cur else None,
                'anomaly':     bool(cur['anomaly'])     if cur else None,
                'zscore':      cur['zscore']            if cur else None,
                'ts':          cur['ts']                if cur else None,
            },
            'stats_24h': {
                'uptime_pct':  round(stats['up'] / stats['total'] * 100, 2)
                               if stats['total'] else None,
                'avg_ms':      round(stats['avg_ms'], 1)  if stats['avg_ms']  else None,
                'max_ms':      round(stats['max_ms'], 1)  if stats['max_ms']  else None,
                'checks':      stats['total'],
                'anomalies':   stats['anomalies'],
            },
        }
    return {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'all_up':       all(v['current']['ok'] for v in result.values() if v['current']['ok'] is not None),
        'services':     result,
    }


def render_csv(conn) -> str:
    since = int(time.time()) - CSV_HOURS * 3600
    rows  = conn.execute(
        """SELECT ts, target, url, ok, status_code, response_ms, zscore, anomaly
           FROM checks WHERE ts>=? ORDER BY ts DESC""",
        (since,),
    ).fetchall()

    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(['timestamp_utc', 'target', 'url', 'ok', 'status_code',
                'response_ms', 'zscore', 'anomaly'])
    for r in rows:
        dt = datetime.fromtimestamp(r[0], tz=timezone.utc).isoformat()
        w.writerow([dt, r[1], r[2], r[3], r[4], r[5], r[6], r[7]])
    return buf.getvalue()


# ── HTTP handler ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Suppress default access log — too noisy for a monitoring dashboard
        pass

    def send(self, code: int, ctype: str, body: str):
        enc = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(enc)))
        self.send_header('X-Frame-Options', 'SAMEORIGIN')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(enc)

    def do_GET(self):
        path = self.path.split('?')[0].rstrip('/')

        if path not in ('/observatory', '/observatory/api', '/observatory/export.csv'):
            self.send(404, 'text/plain', 'Not found')
            return

        if not DB_PATH.exists():
            self.send(503, 'text/plain', 'No data yet — run checker.py first')
            return

        conn = get_conn()
        try:
            if path == '/observatory':
                self.send(200, 'text/html; charset=utf-8', render_dashboard(conn))

            elif path == '/observatory/api':
                self.send(200, 'application/json', json.dumps(render_api(conn), indent=2))

            elif path == '/observatory/export.csv':
                csv_body = render_csv(conn)
                enc = csv_body.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/csv')
                self.send_header('Content-Disposition',
                                 f'attachment; filename="observatory-{int(time.time())}.csv"')
                self.send_header('Content-Length', str(len(enc)))
                self.end_headers()
                self.wfile.write(enc)
        finally:
            conn.close()


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    server = HTTPServer(('127.0.0.1', PORT), Handler)
    print(f'[observatory] Listening on http://127.0.0.1:{PORT}')
    print(f'[observatory] Dashboard: http://127.0.0.1:{PORT}/observatory')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('[observatory] Shutting down')
        server.server_close()


if __name__ == '__main__':
    main()
