"""
ETF Screener — Daily Update Script
- Fetches fresh ETF data via akshare
- Updates etf_dashboard.html EMBED block
- Can push via PushPlus to WeChat (optional)
"""
import sys, os, json, re, shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Beijing timezone
BJ_TZ = timezone(timedelta(hours=8))

def bj_now():
    return datetime.now(BJ_TZ)

# Change to script directory
os.chdir(Path(__file__).resolve().parent)
sys.path.insert(0, '.')

from app import fetch_all_data, save_cache


def get_dashboard_url():
    """Return ETF dashboard URL."""
    if os.getenv('RENDER'):
        return 'https://etf-screener-xxxx.onrender.com'  # Update with actual Render URL
    return 'http://localhost:8080'


def send_pushplus(token: str, title: str, content: str, template: str = 'html') -> bool:
    """Send push notification via PushPlus."""
    import urllib.request
    url = 'http://www.pushplus.plus/send'
    data = json.dumps({
        'token': token,
        'title': title,
        'content': content,
        'template': template,
    }).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            if result.get('code') == 200:
                print(f'  PushPlus OK: {title}')
                return True
            else:
                print(f'  PushPlus fail: {result}')
                return False
    except Exception as e:
        print(f'  PushPlus error: {e}')
        return False


def build_push_html(etfs) -> str:
    """Build compact HTML table for WeChat push."""
    now = bj_now().strftime('%Y-%m-%d %H:%M')
    rows = ''
    for e in etfs:
        name = e.get('name', '-')
        code = e.get('code', '-')
        price = e.get('latest_price', 0)
        discount = e.get('discount_rate', 0)
        fee = e.get('total_fee', e.get('mgmt_fee', '--'))
        pe_pct = e.get('pe_percentile')
        pe_str = f'{pe_pct:.1f}%' if pe_pct is not None else '--'
        d_color = '#059669' if discount < -0.3 else '#dc2626' if discount > 0.3 else '#64748b'
        rows += (
            f'<tr>'
            f'<td style="text-align:left;font-weight:500">{name}<br><span style="font-size:10px;color:#8892b0">{code}</span></td>'
            f'<td style="font-weight:600">{price:.3f}</td>'
            f'<td style="color:{d_color};font-weight:600">{discount:+.2f}%</td>'
            f'<td>{fee}</td>'
            f'<td>{pe_str}</td>'
            f'</tr>'
        )

    return (
        f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1.0">'
        f'<title>ETF Screener Daily Report</title></head>'
        f'<body style="margin:0;padding:10px;font-family:-apple-system,PingFang SC,Microsoft YaHei,sans-serif;background:#fff;color:#1a1a2e">'
        f'<h2 style="font-size:17px;text-align:center;margin:0 0 4px">ETF Screener Daily Report</h2>'
        f'<div style="text-align:center;font-size:11px;color:#64748b;margin-bottom:10px">{now} | <b style="color:#059669">{len(etfs)}</b> ETFs</div>'
        f'<table style="width:100%;border-collapse:collapse;font-size:12px">'
        f'<thead><tr style="background:#f1f5f9;color:#64748b;font-size:10px">'
        f'<th style="padding:6px 3px;text-align:left">Name</th><th style="padding:6px 3px">Price</th>'
        f'<th style="padding:6px 3px">Discount</th><th style="padding:6px 3px">Fee</th><th style="padding:6px 3px">PE%</th>'
        f'</tr></thead><tbody>{rows}</tbody></table>'
        f'<div style="text-align:center;padding:10px;color:#94a3b8;font-size:10px;border-top:1px solid #e2e8f0;margin-top:10px">'
        f'Data: akshare | <a href="{get_dashboard_url()}" style="color:#6366f1">Open Dashboard</a></div></body></html>'
    )


def main():
    print(f'[{bj_now():%Y-%m-%d %H:%M}] ETF Screener update starting...')

    # Step 1: Fetch fresh data
    print('Fetching ETF data from akshare...')
    etfs = fetch_all_data()
    if not etfs:
        print('ERROR: No ETF data fetched')
        sys.exit(1)

    print(f'  Fetched {len(etfs)} ETFs')

    # Step 2: Save cache
    save_cache(etfs)
    print('  Cache saved')

    # Step 3: Update dashboard.html EMBED block
    dashboard_path = Path('etf_dashboard.html')
    if dashboard_path.exists():
        with open(dashboard_path, 'r', encoding='utf-8') as f:
            html = f.read()

        # Strip internal fields before embedding
        embed_data = []
        for e in etfs:
            clean = {k: v for k, v in e.items() if not k.startswith('_')}
            embed_data.append(clean)

        if 'var EMBED=' in html:
            html = re.sub(
                r'var EMBED=\[.*?\];',
                f'var EMBED={json.dumps(embed_data, ensure_ascii=False)};',
                html
            )

        with open(dashboard_path, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f'  dashboard.html EMBED updated ({len(embed_data)} ETFs)')
    else:
        print('  WARNING: etf_dashboard.html not found')

    # Step 4: PushPlus push (if token configured)
    pushplus_token = os.getenv('PUSHPLUS_TOKEN', '')
    if pushplus_token:
        html_content = build_push_html(etfs)
        send_pushplus(pushplus_token, f'ETF Screener ({len(etfs)} ETFs)', html_content, 'html')

    print(f'[{bj_now():%Y-%m-%d %H:%M}] Done: {len(etfs)} ETFs updated')


if __name__ == '__main__':
    main()
