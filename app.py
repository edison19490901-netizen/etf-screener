"""
ETF Screener Backend — HTTP Server
Start: python app.py
Serves ETF dashboard + /api/etf_data endpoint
Data sources: akshare (spot/fee/PE) + baostock (K-line history)
"""
import json, os, sys, time, threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pandas as pd
import numpy as np
import baostock as bs

# ── Disable proxy (akshare often fails with proxy set) ──────────
for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
    os.environ.pop(k, None)
os.environ['NO_PROXY'] = '*'

# ── Beijing timezone ─────────────────────────────────────────────
BJ_TZ = timezone(timedelta(hours=8))

def bj_now():
    return datetime.now(BJ_TZ)

# ── Paths ────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / 'cache'
CACHE_FILE = CACHE_DIR / 'etf_cache.json'
META_FILE = CACHE_DIR / 'etf_metadata.json'  # fund size, fees, PE — slow-changing
os.chdir(BASE_DIR)

# Load .env if present
try:
    from dotenv import load_dotenv
    for p in [BASE_DIR / '.env', BASE_DIR.parent / '.env']:
        if p.exists():
            load_dotenv(p)
except ImportError:
    pass

# ── ETF List ─────────────────────────────────────────────────────
ETF_LIST = [
    '516080',  # 创新药ETF易方达
    '159061',  # 绿色电力ETF南方
    '159611',  # 电力ETF广发
    '159692',  # 证券ETF东财
    '516120',  # 化工ETF富国
    '516770',  # 游戏ETF华泰柏瑞
    '515550',  # 中证500ETF国联
    '159562',  # 黄金股ETF华夏
    '588080',  # 科创50ETF易方达
    '159741',  # 恒生科技ETF嘉实
    '159928',  # 消费ETF汇添富
    '510300',  # 沪深300ETF华泰柏瑞
    '515880',  # 通信ETF国泰
    '588780',  # 科创芯片设计ETF国联安
]

# ETF → underlying index mapping for PE percentile
# stock_index_pe_lg uses Chinese names: 上证50, 沪深300, 中证500, 创业板50, 中证红利, etc.
# Full list: 上证50, 沪深300, 上证380, 创业板50, 中证500, 上证180, 中证红利, 中证100, 中证1000, 上证银行, 中证100, 中证800
ETF_INDEX_MAP = {
    '510300': '沪深300',   # 沪深300ETF华泰柏瑞
    '515550': '中证500',   # 中证500ETF国联
    # Note: 科创50, 恒生科技, etc. not in legulegu's supported list
}

# ── Data Fetching ────────────────────────────────────────────────

def calc_bollinger(closes, period=20, std_mult=2):
    """Calculate Bollinger Bands. Returns (middle, upper, lower) arrays."""
    if len(closes) < period:
        return None, None, None
    s = pd.Series(closes)
    middle = s.rolling(period).mean()
    std = s.rolling(period).std()
    upper = middle + std_mult * std
    lower = middle - std_mult * std
    return middle.values, upper.values, lower.values


def fetch_all_data(progress_cb=None):
    """
    Fetch all ETF data from akshare.
    Returns list of dicts, one per ETF.
    progress_cb(msg) called for logging.
    """
    import akshare as ak

    def log(msg):
        if progress_cb:
            progress_cb(msg)
        print(f'[{bj_now():%H:%M:%S}] {msg}')

    results = []

    # ── Step 1: Spot quotes (all ETFs at once, paginated) ────────
    log('Fetching ETF spot quotes...')
    try:
        df_spot = ak.fund_etf_spot_em()
        df_spot = df_spot[df_spot['代码'].isin(ETF_LIST)].copy()
        log(f'  Got {len(df_spot)} ETFs from spot data')
    except Exception as e:
        log(f'  ERROR spot data: {e}')
        return None

    # Build base results from spot data
    for _, row in df_spot.iterrows():
        code = str(row['代码'])
        fund_size = row.get('流通市值', 0) or 0
        results.append({
            'code': code,
            'name': str(row.get('名称', '')),
            'latest_price': float(row.get('最新价', 0)),
            'iopv': float(row.get('IOPV实时估值', 0)) if pd.notna(row.get('IOPV实时估值')) else None,
            'discount_rate': float(row.get('基金折价率', 0)) if pd.notna(row.get('基金折价率')) else 0,
            'change_pct': float(row.get('涨跌幅', 0)) if pd.notna(row.get('涨跌幅')) else 0,
            'volume': int(row.get('成交量', 0) or 0),
            'turnover': float(row.get('成交额', 0) or 0),
            'high': float(row.get('最高价', 0)),
            'low': float(row.get('最低价', 0)),
            'prev_close': float(row.get('昨收', 0)),
            'amplitude': float(row.get('振幅', 0)) if pd.notna(row.get('振幅')) else 0,
            'turnover_rate': float(row.get('换手率', 0)) if pd.notna(row.get('换手率')) else 0,
            'fund_size': int(fund_size),
            'fund_size_yi': round(fund_size / 1e8, 2),  # 亿元
            'update_time': str(row.get('更新时间', '')),
            'data_date': str(row.get('数据日期', ''))[:10],
            # To be filled by history/fee/PE steps
            'price_history': None,
            'min_price_1y': None,
            'pct_from_low': None,
            'bb_lower': None,
            'pct_from_bb_low': None,
            'mgmt_fee': None,
            'custody_fee': None,
            'total_fee': None,
            'pe_percentile': None,
            'pe_current': None,
        })

    # ── Step 2: K-line history via baostock ──────────────────────
    end_date = bj_now().strftime('%Y-%m-%d')
    start_date_1y = (bj_now() - timedelta(days=380)).strftime('%Y-%m-%d')  # Extra margin

    # Baostock login (required once)
    try:
        bs.login()
        log('  baostock logged in')
    except Exception as e:
        log(f'  baostock login ERROR: {e}')

    for i, etf in enumerate(results):
        code = etf['code']
        # Convert to baostock format: 51xxxx→sh, 15xxxx→sz
        bs_code = ('sh.' if code.startswith('5') else 'sz.') + code
        try:
            log(f'  K-line [{i+1}/{len(results)}] {bs_code}')
            rs = bs.query_history_k_data_plus(
                bs_code, 'date,close,high,low,volume',
                start_date=start_date_1y, end_date=end_date,
                frequency='d', adjustflag='2'
            )
            if rs.error_code != '0' or not rs.data:
                log(f'    No data: {rs.error_msg}')
                continue

            # Parse data: [date, close, high, low, volume]
            dates = []
            closes = []
            lows = []
            for row in rs.data:
                try:
                    dates.append(row[0])
                    closes.append(float(row[1]))
                    lows.append(float(row[3]))
                except (ValueError, IndexError):
                    continue

            if len(closes) < 20:
                log(f'    Insufficient data: {len(closes)} rows')
                continue

            # 60-day price history for chart
            chart_closes = closes[-60:] if len(closes) >= 20 else closes
            etf['price_history'] = [round(c, 4) for c in chart_closes]

            # 1-year low
            if len(lows) > 0:
                etf['min_price_1y'] = round(min(lows), 4)
                if etf['min_price_1y'] > 0:
                    etf['pct_from_low'] = round(
                        (etf['latest_price'] - etf['min_price_1y']) / etf['min_price_1y'] * 100, 2
                    )

            # Bollinger Bands (20-day, use last 60 closes for stability)
            bb_mid, bb_upper, bb_lower = calc_bollinger(closes[-60:], period=20)
            if bb_lower is not None and len(bb_lower) > 0:
                # Lower band curve (last 40 values; first 20 are NaN from rolling window)
                bb_lo = [round(float(v), 4) for v in bb_lower[-40:] if not np.isnan(v)]
                if bb_lo:
                    etf['bb_lower_history'] = bb_lo
                last_lower = float(bb_lower[-1])
                if not np.isnan(last_lower) and last_lower > 0:
                    etf['bb_lower'] = round(last_lower, 4)
                    etf['pct_from_bb_low'] = round(
                        (etf['latest_price'] - last_lower) / last_lower * 100, 2
                    )
                # Upper band curve
                bb_hi = [round(float(v), 4) for v in bb_upper[-40:] if not np.isnan(v)]
                if bb_hi:
                    etf['bb_upper_history'] = bb_hi
                    etf['bb_upper'] = round(float(bb_upper[-1]), 4) if not np.isnan(bb_upper[-1]) else None

            time.sleep(0.1)  # Baostock is fast, small delay

        except Exception as e:
            log(f'  K-line ERROR {code}: {e}')

    try:
        bs.logout()
    except Exception:
        pass

    # ── Step 3: Fees for each ETF ─────────────────────────────────
    for i, etf in enumerate(results):
        code = etf['code']
        try:
            log(f'  Fee [{i+1}/{len(results)}] {code}')
            df_fee = ak.fund_fee_em(symbol=code, indicator='运作费用')
            if df_fee is not None and not df_fee.empty:
                # Fee table has one row with 4 columns:
                # col 0=管理费(type), col 1=rate, col 2=托管费(type), col 3=rate
                for _, frow in df_fee.iterrows():
                    # Check column pairs: (0,1) and (2,3)
                    for col_idx in [0, 2]:
                        if col_idx + 1 >= len(frow):
                            continue
                        fee_type = str(frow.iloc[col_idx])
                        fee_rate_str = str(frow.iloc[col_idx + 1])
                        if '管理' in fee_type:
                            etf['mgmt_fee'] = fee_rate_str
                        elif '托管' in fee_type:
                            etf['custody_fee'] = fee_rate_str

                # Calculate total fee
                mgmt = parse_fee_pct(etf.get('mgmt_fee'))
                custody = parse_fee_pct(etf.get('custody_fee'))
                if mgmt is not None:
                    etf['mgmt_fee'] = f'{mgmt:.2f}%'
                if custody is not None:
                    etf['custody_fee'] = f'{custody:.2f}%'
                if mgmt is not None and custody is not None:
                    etf['total_fee'] = f'{mgmt + custody:.2f}%'
                elif mgmt is not None:
                    etf['total_fee'] = f'{mgmt:.2f}%'
            time.sleep(0.3)
        except Exception as e:
            log(f'  Fee ERROR {code}: {e}')

    # ── Step 4: PE Percentile for supported indices ───────────────
    log('Fetching PE percentiles...')
    pe_data = {}  # index_name → {pe_current, pe_percentile}
    for etf_code, idx_name in ETF_INDEX_MAP.items():
        try:
            log(f'  PE {idx_name}')
            df_pe = ak.stock_index_pe_lg(symbol=idx_name)
            if df_pe is not None and not df_pe.empty:
                # Columns: 日期, 指数, 加权动态市盈率, 动态市盈率, 动态市盈率中位数,
                #          加权滚动市盈率, 滚动市盈率, 滚动市盈率中位数
                cols = df_pe.columns.tolist()
                # Find TTM PE column: contains '滚动市盈率' but not '加权' or '中位'
                pe_col = None
                for c in cols:
                    if '滚动市盈率' in str(c) and '加权' not in str(c) and '中位' not in str(c):
                        pe_col = c
                        break
                # Fallback: use '动态市盈率'
                if pe_col is None:
                    for c in cols:
                        if '动态市盈率' in str(c) and '加权' not in str(c) and '中位' not in str(c):
                            pe_col = c
                            break

                if pe_col:
                    latest = df_pe.iloc[-1]
                    pe_val = float(latest[pe_col]) if pd.notna(latest.get(pe_col)) else 0

                    # Calculate percentile: % of historical values below current PE
                    pe_series = df_pe[pe_col].dropna()
                    if len(pe_series) > 0 and pe_val > 0:
                        pe_percentile = round(
                            (pe_series < pe_val).sum() / len(pe_series) * 100, 1
                        )
                    else:
                        pe_percentile = None

                    pe_data[idx_name] = {
                        'pe_current': round(pe_val, 2) if pe_val > 0 else None,
                        'pe_percentile': pe_percentile,
                    }
                    log(f'    TTM PE={pe_val:.2f}, 分位={pe_percentile}%')
            time.sleep(0.5)
        except Exception as e:
            log(f'  PE ERROR {idx_name}: {e}')

    # Attach PE data to corresponding ETFs
    for etf in results:
        idx_name = ETF_INDEX_MAP.get(etf['code'])
        if idx_name and idx_name in pe_data:
            etf.update(pe_data[idx_name])

    log(f'Done: {len(results)} ETFs fetched')
    # Save metadata cache for fast mode
    save_metadata(results)
    return results


def save_metadata(etfs):
    """Save slow-changing metadata (fund size, fees, PE, names) for fast refresh."""
    meta = {}
    for e in etfs:
        meta[e['code']] = {
            'name': e.get('name', ''),
            'fund_size': e.get('fund_size', 0),
            'fund_size_yi': e.get('fund_size_yi', 0),
            'mgmt_fee': e.get('mgmt_fee'),
            'custody_fee': e.get('custody_fee'),
            'total_fee': e.get('total_fee'),
            'pe_percentile': e.get('pe_percentile'),
            'pe_current': e.get('pe_current'),
            'discount_rate': e.get('discount_rate'),
            'change_pct': e.get('change_pct'),
            'volume': e.get('volume'),
            'turnover': e.get('turnover'),
            'turnover_rate': e.get('turnover_rate'),
            'amplitude': e.get('amplitude'),
            'iopv': e.get('iopv'),
            'update_time': e.get('update_time'),
            'data_date': e.get('data_date'),
        }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(META_FILE, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False)


def load_metadata():
    """Load cached metadata."""
    if META_FILE.exists():
        try:
            with open(META_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def fetch_prices_quick(progress_cb=None):
    """
    Fast refresh: baostock K-line only (3s), metadata from cache.
    No akshare calls — skips the 110s spot data fetch.
    """
    def log(msg):
        if progress_cb:
            progress_cb(msg)
        print(f'[{bj_now():%H:%M:%S}] {msg}')

    meta = load_metadata()
    results = []
    end_date = bj_now().strftime('%Y-%m-%d')
    start_date_1y = (bj_now() - timedelta(days=380)).strftime('%Y-%m-%d')

    try:
        bs.login()
    except Exception as e:
        log(f'baostock login error: {e}')
        return None

    for code in ETF_LIST:
        etf_meta = meta.get(code, {})
        bs_code = ('sh.' if code.startswith('5') else 'sz.') + code
        etf = {
            'code': code,
            'name': etf_meta.get('name', code),
            'latest_price': 0,
            'price_history': None,
            'min_price_1y': None,
            'pct_from_low': None,
            'bb_lower': None,
            'pct_from_bb_low': None,
            # From metadata cache
            'fund_size': etf_meta.get('fund_size', 0),
            'fund_size_yi': etf_meta.get('fund_size_yi', 0),
            'mgmt_fee': etf_meta.get('mgmt_fee'),
            'custody_fee': etf_meta.get('custody_fee'),
            'total_fee': etf_meta.get('total_fee'),
            'pe_percentile': etf_meta.get('pe_percentile'),
            'pe_current': etf_meta.get('pe_current'),
            'discount_rate': etf_meta.get('discount_rate', 0),
            'change_pct': etf_meta.get('change_pct', 0),
            'volume': etf_meta.get('volume', 0),
            'turnover': etf_meta.get('turnover', 0),
            'turnover_rate': etf_meta.get('turnover_rate', 0),
            'amplitude': etf_meta.get('amplitude', 0),
            'iopv': etf_meta.get('iopv'),
            'update_time': etf_meta.get('update_time', ''),
            'data_date': etf_meta.get('data_date', ''),
        }
        try:
            rs = bs.query_history_k_data_plus(
                bs_code, 'date,close,high,low,volume',
                start_date=start_date_1y, end_date=end_date,
                frequency='d', adjustflag='2'
            )
            if rs.error_code != '0' or not rs.data:
                results.append(etf)
                continue

            closes = [float(r[1]) for r in rs.data]
            lows = [float(r[3]) for r in rs.data]

            if len(closes) < 10:
                etf['latest_price'] = closes[-1] if closes else 0
                results.append(etf)
                continue

            etf['latest_price'] = round(closes[-1], 4)
            etf['price_history'] = [round(c, 4) for c in closes[-60:]]
            etf['min_price_1y'] = round(min(lows), 4)
            if etf['min_price_1y'] > 0:
                etf['pct_from_low'] = round(
                    (etf['latest_price'] - etf['min_price_1y']) / etf['min_price_1y'] * 100, 2
                )

            bb_mid, bb_upper, bb_lower = calc_bollinger(closes[-60:], period=20)
            if bb_lower is not None and len(bb_lower) > 0:
                bb_lo = [round(float(v), 4) for v in bb_lower[-40:] if not np.isnan(v)]
                if bb_lo:
                    etf['bb_lower_history'] = bb_lo
                ll = float(bb_lower[-1])
                if not np.isnan(ll) and ll > 0:
                    etf['bb_lower'] = round(ll, 4)
                    etf['pct_from_bb_low'] = round(
                        (etf['latest_price'] - ll) / ll * 100, 2
                    )
                bb_hi = [round(float(v), 4) for v in bb_upper[-40:] if not np.isnan(v)]
                if bb_hi:
                    etf['bb_upper_history'] = bb_hi
                    etf['bb_upper'] = round(float(bb_upper[-1]), 4) if not np.isnan(bb_upper[-1]) else None

            time.sleep(0.05)
        except Exception as e:
            log(f'  Quick K-line ERROR {code}: {e}')

        results.append(etf)

    try:
        bs.logout()
    except Exception:
        pass

    log(f'Quick refresh done: {len(results)} ETFs')
    return results


def parse_fee_pct(rate_str):
    """Parse fee rate string like '0.15%（每年）' → 0.15"""
    if not rate_str:
        return None
    import re
    m = re.search(r'([\d.]+)\s*%', str(rate_str))
    return float(m.group(1)) if m else None


# ── Cache Management ─────────────────────────────────────────────

_cache = None
_cache_lock = threading.Lock()
_fetching = False


def load_cache():
    """Load cached ETF data from disk."""
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            print(f'Loaded cache: {len(data)} ETFs, cached at {data[0].get("_cached_at","?") if data else "empty"}')
            return data
        except Exception as e:
            print(f'Cache load error: {e}')
    return None


def save_cache(data):
    """Save ETF data to disk cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for d in data:
        d['_cached_at'] = bj_now().strftime('%Y-%m-%d %H:%M')
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, default=str)
    except Exception as e:
        print(f'Cache save error: {e}')


def get_data(force_refresh=False, full=False):
    """
    Get ETF data.
    - Default: cache → quick (baostock, ~3s) → stale cache
    - full=True: cache → full (akshare+baostock, ~130s) → stale cache
    """
    global _cache, _fetching

    with _cache_lock:
        if force_refresh:
            _cache = None

        if _cache is not None:
            return _cache

        # Try disk cache first (only if fresh enough: < 1 hour for quick, < 1 day for full)
        cached = load_cache()
        if cached and not force_refresh:
            cached_time = cached[0].get('_cached_at', '') if cached else ''
            _cache = cached
            return _cache

        # Need live fetch
        if _fetching:
            return cached or []

        _fetching = True

    try:
        if full:
            data = fetch_all_data()
        else:
            data = fetch_prices_quick()
            # If no metadata exists, fall back to full
            if data and not load_metadata():
                log_msg = lambda m: print(f'[{bj_now():%H:%M:%S}] {m}')
                log_msg('No metadata cache, falling back to full fetch...')
                data = fetch_all_data()

        if data:
            save_cache(data)
            with _cache_lock:
                _cache = data
            return data
        return load_cache() or []
    finally:
        with _cache_lock:
            _fetching = False


# ── HTTP Handler ─────────────────────────────────────────────────

class ETFHandler(SimpleHTTPRequestHandler):
    """Custom handler: API endpoints + static file serving."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def log_message(self, format, *args):
        print(f'[{bj_now():%H:%M:%S}] {args[0]}')

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # ── API: Get ETF data ─────────────────────────────────
        if path == '/api/etf_data':
            full = qs.get('full', ['0'])[0] == '1'
            force = qs.get('force', ['0'])[0] == '1'
            data = get_data(force_refresh=force, full=full)
            self._json_response({
                'count': len(data),
                'updated': data[0].get('_cached_at', '') if data else '',
                'mode': 'full' if full else 'quick',
                'etfs': data,
            })
            return

        # ── API: Refresh data ─────────────────────────────────
        if path == '/api/refresh':
            # Default: full refresh (akshare+baostock, ~130s). Use ?quick=1 for fast mode (~3s).
            quick = qs.get('quick', ['0'])[0] == '1'
            async_mode = qs.get('async', ['0'])[0] == '1'
            if async_mode:
                t = threading.Thread(
                    target=lambda: get_data(force_refresh=True, full=not quick),
                    daemon=True
                )
                t.start()
                mode = 'quick (~3s)' if quick else 'full (~130s)'
                self._json_response({'status': 'ok', 'msg': f'Refresh started: {mode}'})
            else:
                data = get_data(force_refresh=True, full=not quick)
                self._json_response({
                    'status': 'ok',
                    'count': len(data) if data else 0,
                    'msg': f'Refreshed {len(data) if data else 0} ETFs',
                })
            return

        # ── API: Health check ─────────────────────────────────
        if path == '/api/health':
            self._json_response({'status': 'ok', 'time': bj_now().isoformat()})
            return

        # ── Static files ──────────────────────────────────────
        # Redirect root to dashboard
        if path == '/' or path == '':
            self.send_response(302)
            self.send_header('Location', '/etf_dashboard.html')
            self.end_headers()
            return

        return super().do_GET()

    def _json_response(self, data):
        body = json.dumps(data, ensure_ascii=False, default=str).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(body)


# ── Main ─────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=int(os.getenv('PORT', '8080')))
    parser.add_argument('--prefetch', action='store_true', default=True,
                        help='Pre-fetch data on startup (default: True)')
    args = parser.parse_args()

    print(f'ETF Screener starting on port {args.port}...')

    if args.prefetch:
        print('Pre-fetching ETF data...')
        get_data(force_refresh=False)  # Uses cache if available

    server = HTTPServer(('0.0.0.0', args.port), ETFHandler)
    print(f'Server ready: http://localhost:{args.port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nShutting down...')
        server.shutdown()


if __name__ == '__main__':
    main()
