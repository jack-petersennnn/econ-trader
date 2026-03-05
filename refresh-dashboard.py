#!/usr/bin/env python3
"""Refresh econ-data.json for the dashboard."""
import json, time, os, sys
from urllib.request import urlopen, Request

BASE = 'https://api.elections.kalshi.com/trade-api/v2'
DASHBOARD = '/home/ubuntu/.openclaw/workspace/dashboard/econ-data.json'

TARGETS = {
    'fed': ['KXFEDDECISION-27JAN', 'KXFED-27JAN', 'KXFEDDECISION-27MAR', 'KXFED-27MAR'],
    'cpi': ['KXLCPIMAXYOY-27'],
    'gdp': ['KXGDP-27JAN30', 'KXGDPYEAR-26'],
    'unemployment': ['KXU3MAX-27']
}

def fetch_event(ticker):
    req = Request(f'{BASE}/events/{ticker}?with_nested_markets=true',
                  headers={'User-Agent': 'econ/1', 'Accept': 'application/json'})
    return json.loads(urlopen(req, timeout=20).read().decode())

dashboard_data = {}
for cat, tickers in TARGETS.items():
    dashboard_data[cat] = []
    for t in tickers:
        try:
            data = fetch_event(t)
            evt = data.get('event', data)
            markets = evt.get('markets', [])
            dashboard_data[cat].append({
                'ticker': t,
                'title': evt.get('title', ''),
                'markets': [{
                    'ticker': m.get('ticker', ''),
                    'title': m.get('title', ''),
                    'yes_price': (m.get('yes_bid', 0) or 0),
                    'volume': m.get('volume', 0),
                    'close_date': m.get('close_date', '')[:10]
                } for m in markets[:8]]
            })
            time.sleep(1)
        except Exception as e:
            print(f'Skip {t}: {e}', file=sys.stderr)

with open(DASHBOARD, 'w') as f:
    json.dump(dashboard_data, f, indent=2)

total = sum(len(v) for v in dashboard_data.values())
print(f'Refreshed econ dashboard: {total} events')
