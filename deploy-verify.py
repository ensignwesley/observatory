#!/usr/bin/env python3
"""
deploy-verify.py — Observatory coverage checker

Compares nginx proxy_pass locations against Observatory TARGETS.
Reports proxied services that have no Observatory target.

Usage:
    python3 deploy-verify.py                    # check all
    python3 deploy-verify.py --nginx /etc/nginx/sites-enabled/wesley
    python3 deploy-verify.py --json             # machine-readable output

Exit codes:
    0 = all proxied services covered
    1 = gaps found
"""

import re
import sys
import os
import json
import argparse

NGINX_SITE  = '/etc/nginx/sites-enabled/wesley'
CHECKER_PY  = os.path.join(os.path.dirname(__file__), 'checker.py')

# ── Parse nginx site config ───────────────────────────────────────────────────

def parse_nginx_locations(path):
    """
    Extract location blocks with proxy_pass directives.
    Returns list of {'location': '/path', 'upstream': 'http://127.0.0.1:PORT/...'}.
    Ignores static/alias locations (no proxy_pass = not a proxied service).
    """
    try:
        text = open(path).read()
    except FileNotFoundError:
        print(f'ERROR: nginx config not found: {path}', file=sys.stderr)
        sys.exit(2)

    # Find location blocks: capture location path + everything until closing }
    # This is a simple line-by-line approach — not a full nginx parser.
    locations = []
    current_loc = None
    brace_depth = 0

    for line in text.splitlines():
        stripped = line.strip()

        # Start of a location block
        loc_match = re.match(r'location\s+(?:[~^=*]+\s+)?([^\s{]+)\s*\{?', stripped)
        if loc_match and 'location' in stripped:
            current_loc = {'location': loc_match.group(1), 'upstream': None}
            brace_depth = 1
            continue

        if current_loc:
            brace_depth += stripped.count('{')
            brace_depth -= stripped.count('}')

            proxy_match = re.match(r'proxy_pass\s+(\S+?);', stripped)
            if proxy_match:
                current_loc['upstream'] = proxy_match.group(1).rstrip('/')

            if brace_depth <= 0:
                if current_loc['upstream']:   # only track proxied locations
                    locations.append(current_loc)
                current_loc = None
                brace_depth = 0

    return locations

# ── Parse Observatory TARGETS ─────────────────────────────────────────────────

def parse_observatory_targets(path):
    """
    Extract TARGETS from checker.py by text parsing.
    Looks for 'slug': and 'url': pairs within the TARGETS block.
    Robust enough for the structured format used in checker.py.
    """
    try:
        text = open(path).read()
    except FileNotFoundError:
        print(f'ERROR: checker.py not found: {path}', file=sys.stderr)
        sys.exit(2)

    # Extract the TARGETS = [ ... ] block
    m = re.search(r'TARGETS\s*=\s*\[(.+?)\n\]', text, re.DOTALL)
    if not m:
        print('ERROR: could not find TARGETS list in checker.py', file=sys.stderr)
        sys.exit(2)

    block = m.group(1)
    slugs = re.findall(r"'slug'\s*:\s*'([^']+)'", block)
    urls  = re.findall(r"'url'\s*:\s*'([^']+)'", block)

    if len(slugs) != len(urls):
        print(f'WARN: slug/url count mismatch ({len(slugs)}/{len(urls)})', file=sys.stderr)

    return [{'slug': s, 'url': u} for s, u in zip(slugs, urls)]

# ── Compare ───────────────────────────────────────────────────────────────────

def extract_port(url):
    """Extract port number from a URL, or None."""
    m = re.search(r':(\d+)', url)
    return m.group(1) if m else None

def check_coverage(nginx_locs, obs_targets):
    """
    For each proxied nginx location, check if Observatory covers it.
    Matching strategy: look for a target whose URL shares the same upstream port.
    Returns (covered, gaps) as lists of location dicts.
    """
    obs_ports = set()
    obs_paths = set()
    for t in obs_targets:
        port = extract_port(t['url'])
        if port:
            obs_ports.add(port)
        # Also track URL paths for static-file coverage
        m = re.match(r'https?://[^/]+(/.+)', t['url'])
        if m:
            obs_paths.add(m.group(1).rstrip('/'))

    covered = []
    gaps    = []

    for loc in nginx_locs:
        port = extract_port(loc['upstream'])
        if port and port in obs_ports:
            covered.append(loc)
        else:
            gaps.append(loc)

    return covered, gaps

# ── Report ────────────────────────────────────────────────────────────────────

def report_text(covered, gaps, obs_targets):
    lines = []
    lines.append('── Observatory Coverage Report ─────────────────────────────')
    lines.append(f'   Nginx proxied locations : {len(covered) + len(gaps)}')
    lines.append(f'   Observatory targets     : {len(obs_targets)}')
    lines.append('')

    if covered:
        lines.append('✅  COVERED')
        for loc in covered:
            lines.append(f'    {loc["location"]:20s} → {loc["upstream"]}')
        lines.append('')

    if gaps:
        lines.append('❌  NOT IN OBSERVATORY')
        for loc in gaps:
            lines.append(f'    {loc["location"]:20s} → {loc["upstream"]}')
            port = extract_port(loc['upstream'])
            hint = f'http://127.0.0.1:{port}{loc["location"]}' if port else loc['upstream']
            lines.append(f'    {"":20s}   → add to TARGETS: url={hint}')
        lines.append('')
        lines.append(f'ACTION: {len(gaps)} proxied location(s) have no Observatory target.')
        lines.append('        Add them to TARGETS in checker.py and restart the checker service.')
    else:
        lines.append('All proxied nginx locations are covered by Observatory. ✅')

    return '\n'.join(lines)

def report_json(covered, gaps, obs_targets):
    return json.dumps({
        'covered': covered,
        'gaps':    gaps,
        'targets': len(obs_targets),
        'ok':      len(gaps) == 0,
    }, indent=2)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description='Check Observatory coverage against nginx proxies')
    p.add_argument('--nginx',    default=NGINX_SITE, help='nginx site config file')
    p.add_argument('--checker',  default=CHECKER_PY, help='checker.py path')
    p.add_argument('--json',     action='store_true', help='JSON output')
    args = p.parse_args()

    nginx_locs  = parse_nginx_locations(args.nginx)
    obs_targets = parse_observatory_targets(args.checker)
    covered, gaps = check_coverage(nginx_locs, obs_targets)

    if args.json:
        print(report_json(covered, gaps, obs_targets))
    else:
        print(report_text(covered, gaps, obs_targets))

    sys.exit(0 if not gaps else 1)

if __name__ == '__main__':
    main()
