"""
ETF Screener Backend — HTTP Server
Start: python app.py
Serves ETF dashboard + /api/etf_data endpoint
Data sources: akshare (spot/fee/PE/累计净值) + baostock (latest-price fallback)
前复权 K-line derived from 累计净值 (baostock ignores adjustflag for ETFs).
"""
import json, os, sys, time, threading, re
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import urllib.request

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
NAV_FILE = CACHE_DIR / 'etf_nav_cache.json'  # 累计净值 (前复权 source)
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
    # 攻击仓 — 行业ETF
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
    '159796',  # 电池ETF汇添富
    '510050',  # 上证50ETF华夏
    '159367',  # 创业板50ETF华夏
    '159150',  # 深证50ETF易方达
    '159212',  # 深100ETF南方
    '159227',  # 航空航天ETF华夏
    # 对冲仓 — 防御/避险
    '518680',  # 黄金ETF华夏（费率0.2% vs 华安0.6%）
    '511260',  # 十年国债ETF国泰
    '512890',  # 红利低波ETF华泰柏瑞
]
BENCHMARK_ETF = '510300'  # 对冲基准：沪深300

# Fallback names for ETFs not covered by akshare spot data
ETF_NAMES = {
    '159796': '电池ETF汇添富',
    '518680': '黄金ETF华夏',
    '511260': '十年国债ETF国泰',
    '512890': '红利低波ETF华泰柏瑞',
}

# ETF → underlying index mapping for PE percentile
# stock_index_pe_lg uses Chinese names: 上证50, 沪深300, 中证500, 创业板50, 中证红利, etc.
# Full list: 上证50, 沪深300, 上证380, 创业板50, 中证500, 上证180, 中证红利, 中证100, 中证1000, 上证银行, 中证100, 中证800
ETF_INDEX_MAP = {
    '510300': '沪深300',   # 沪深300ETF华泰柏瑞
    '515550': '中证500',   # 中证500ETF国联
    '510050': '上证50',   # 上证50ETF华夏
    '159367': '创业板50', # 创业板50ETF华夏
    # Note: 科创50, 恒生科技, 深证50, 深证100, 国证航天航空 not in legulegu's supported list
}

# ── Data Fetching ────────────────────────────────────────────────

def safe_float(val, default=0.0):
    """Convert value to float, handling NaN."""
    try:
        return float(val) if pd.notna(val) else default
    except (ValueError, TypeError):
        return default

def safe_int(val, default=0):
    """Convert value to int, handling NaN."""
    try:
        return int(float(val)) if pd.notna(val) else default
    except (ValueError, TypeError):
        return default


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


def calc_grid(low, high, price):
    """
    Fibonacci grid between 1Y low and 1Y high.
    5 lines (0.25 / 0.382 / 0.5 / 0.618 / 0.75) → 6 zones (bottom→top: 1..6).
    Zones 1-5 each hold 2份 (2 units); zone 6 is a no-op (不操作).
    Returns line prices + current zone (1-6).
    """
    if not low or not high or high <= low:
        return {'grid_25': None, 'grid_382': None, 'grid_50': None,
                'grid_618': None, 'grid_75': None, 'grid_zone': None}
    rng = high - low
    l25 = low + 0.25 * rng
    l382 = low + 0.382 * rng
    l50 = low + 0.5 * rng
    l618 = low + 0.618 * rng
    l75 = low + 0.75 * rng
    zone = 1
    if price >= l25:
        zone = 2
    if price >= l382:
        zone = 3
    if price >= l50:
        zone = 4
    if price >= l618:
        zone = 5
    if price >= l75:
        zone = 6
    return {
        'grid_25': round(l25, 4),
        'grid_382': round(l382, 4),
        'grid_50': round(l50, 4),
        'grid_618': round(l618, 4),
        'grid_75': round(l75, 4),
        'grid_zone': zone,
    }


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
        fund_size = safe_float(row.get('流通市值'), 0)
        results.append({
            'code': code,
            'name': str(row.get('名称', '')),
            'latest_price': safe_float(row.get('最新价'), 0),
            'iopv': safe_float(row.get('IOPV实时估值'), None) if pd.notna(row.get('IOPV实时估值')) else None,
            'discount_rate': safe_float(row.get('基金折价率'), 0),
            'change_pct': safe_float(row.get('涨跌幅'), 0),
            'volume': safe_int(row.get('成交量'), 0),
            'turnover': safe_float(row.get('成交额'), 0),
            'high': safe_float(row.get('最高价'), 0),
            'low': safe_float(row.get('最低价'), 0),
            'prev_close': safe_float(row.get('昨收'), 0),
            'amplitude': safe_float(row.get('振幅'), 0),
            'turnover_rate': safe_float(row.get('换手率'), 0),
            'fund_size': int(fund_size),
            'fund_size_yi': round(fund_size / 1e8, 2),  # 亿元
            'update_time': str(row.get('更新时间', '')),
            'data_date': str(row.get('数据日期', ''))[:10],
            # To be filled by history/fee/PE steps
            'price_history': None,
            'min_price_1y': None,
            'max_price_1y': None,
            'grid_25': None,
            'grid_382': None,
            'grid_50': None,
            'grid_618': None,
            'grid_75': None,
            'grid_zone': None,
            'pct_from_low': None,
            'bb_lower': None,
            'pct_from_bb_low': None,
            'mgmt_fee': None,
            'custody_fee': None,
            'total_fee': None,
            'pe_percentile': None,
            'pe_current': None,
        })

    # Add any ETFs missing from spot data (e.g. bond/gold ETFs that akshare may skip)
    existing_codes = {r['code'] for r in results}
    for code in ETF_LIST:
        if code not in existing_codes:
            results.append({
                'code': code,
                'name': ETF_NAMES.get(code, code),
                'latest_price': 0, 'iopv': None, 'discount_rate': 0,
                'change_pct': 0, 'volume': 0, 'turnover': 0,
                'high': 0, 'low': 0, 'prev_close': 0, 'amplitude': 0,
                'turnover_rate': 0, 'fund_size': 0, 'fund_size_yi': 0,
                'update_time': '', 'data_date': '',
                'price_history': None, 'min_price_1y': None,
                'max_price_1y': None, 'grid_25': None, 'grid_382': None,
                'grid_50': None, 'grid_618': None, 'grid_75': None, 'grid_zone': None,
                'pct_from_low': None, 'bb_lower': None,
                'pct_from_bb_low': None,
                'mgmt_fee': None, 'custody_fee': None, 'total_fee': None,
                'pe_percentile': None, 'pe_current': None,
            })

    # ── Step 2: latest price fallback + 前复权 K-line (NAV) ───────
    end_date = bj_now().strftime('%Y-%m-%d')

    # Baostock — only to fill latest_price for ETFs missing from spot data
    # (baostock ignores adjustflag for ETFs, so we do NOT use it for 1Y range).
    try:
        bs.login()
        log('  baostock logged in')
    except Exception as e:
        log(f'  baostock login ERROR: {e}')

    for etf in results:
        code = etf['code']
        if etf.get('latest_price'):
            continue
        bs_code = ('sh.' if code.startswith('5') else 'sz.') + code
        try:
            rs = bs.query_history_k_data_plus(
                bs_code, 'date,close',
                start_date=(bj_now() - timedelta(days=10)).strftime('%Y-%m-%d'),
                end_date=end_date, frequency='d', adjustflag='3'
            )
            if rs.error_code == '0' and rs.data:
                etf['latest_price'] = round(float(rs.data[-1][1]), 4)
        except Exception as e:
            log(f'  K-line fallback ERROR {code}: {e}')

    try:
        bs.logout()
    except Exception:
        pass

    # 累计净值 → 前复权 for chart / 1Y low/high / grid / Bollinger.
    log('Fetching NAV history (前复权)...')
    nav_data = fetch_nav_series(ETF_LIST, log)
    save_nav_cache(nav_data)

    for etf in results:
        code = etf['code']
        nav = nav_data.get(code)
        adj = nav_to_adj_closes(nav, etf.get('latest_price'))
        if adj:
            compute_from_closes(etf, adj, etf.get('latest_price'))
        else:
            log(f'  NAV missing for {code}, derived fields left empty')

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
    # Calculate correlations with benchmark
    calc_correlations(results)
    # Save metadata cache for fast mode
    save_metadata(results)
    return results


def calc_correlations(etfs):
    """Calculate 60-day correlation of each ETF with the benchmark (510300)."""
    benchmark = None
    price_map = {}  # code → closes array
    for e in etfs:
        ph = e.get('price_history')
        if ph and len(ph) >= 20:
            # price_history now holds ~260 days (full 1Y); correlation stays 60-day
            price_map[e['code']] = pd.Series(ph[-60:])
    if BENCHMARK_ETF not in price_map:
        return
    bench_returns = price_map[BENCHMARK_ETF].pct_change().dropna()
    for e in etfs:
        e['corr_300'] = None
        if e['code'] == BENCHMARK_ETF:
            e['corr_300'] = 1.0
            continue
        prices = price_map.get(e['code'])
        if prices is not None and len(prices) >= 20:
            rets = prices.pct_change().dropna()
            # Align lengths to the shorter series
            min_len = min(len(rets), len(bench_returns))
            if min_len >= 10:
                r = rets.iloc[-min_len:].corr(bench_returns.iloc[-min_len:])
                if pd.notna(r):
                    e['corr_300'] = round(float(r), 3)


def save_metadata(etfs):
    """Save slow-changing metadata (fund size, fees, PE, names) for fast refresh."""
    meta = {}
    for e in etfs:
        meta[e['code']] = {
            'name': e.get('name', ''),
            'fund_size': e.get('fund_size', 0),
            'fund_size_yi': e.get('fund_size_yi', 0),
            'corr_300': e.get('corr_300'),
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


# ── 前复权 K-line via 累计净值 ───────────────────────────────────
# baostock ignores adjustflag for ETFs (returns 不复权), so split jumps
# corrupt the 1Y low/high. 累计净值 (cumulative NAV) is the exchange's own
# split+dividend-adjusted series (smooth), reliable via eastmoney fund API.

def fetch_nav_series(codes, progress_cb=None):
    """Fetch 累计净值 (cumulative NAV) for ETFs, filtered to ~last 400 days.
    Returns {code: {'dates': [str], 'cum': [float]}}."""
    import akshare as ak

    def log(msg):
        if progress_cb:
            progress_cb(msg)
        else:
            print(f'[{bj_now():%H:%M:%S}] {msg}')

    cutoff = (bj_now() - timedelta(days=400)).strftime('%Y-%m-%d')
    out = {}
    for i, code in enumerate(codes):
        try:
            df = ak.fund_open_fund_info_em(symbol=code, indicator='累计净值走势')
            if df is None or df.empty:
                log(f'  NAV [{i+1}/{len(codes)}] {code}: empty')
                continue
            dcol, ncol = df.columns[0], df.columns[1]
            dates = [str(x)[:10] for x in df[dcol]]
            cum = [safe_float(x) for x in df[ncol]]
            keep = [(d, c) for d, c in zip(dates, cum) if d >= cutoff]
            if not keep:
                log(f'  NAV [{i+1}/{len(codes)}] {code}: no recent data')
                continue
            out[code] = {'dates': [k[0] for k in keep], 'cum': [k[1] for k in keep]}
            log(f'  NAV [{i+1}/{len(codes)}] {code}: {len(keep)} pts')
        except Exception as e:
            log(f'  NAV ERROR {code}: {e}')
        time.sleep(0.25)
    return out


def load_nav_cache():
    """Load cached 累计净值 series."""
    if NAV_FILE.exists():
        try:
            with open(NAV_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_nav_cache(nav_data):
    """Save 累计净值 series to disk."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(NAV_FILE, 'w', encoding='utf-8') as f:
            json.dump(nav_data, f, ensure_ascii=False)
    except Exception as e:
        print(f'Nav cache save error: {e}')


def nav_to_adj_closes(nav, latest_price):
    """Rescale 累计净值 (后复权) → 前复权 closes anchored at latest_price."""
    if not nav or not nav.get('cum') or not latest_price:
        return None
    cum = nav['cum']
    cum_now = cum[-1] if cum else 0
    if not cum_now:
        return None
    scale = latest_price / cum_now
    return [c * scale for c in cum]


def compute_from_closes(etf, closes, latest_price):
    """Compute chart / 1Y range / Fibonacci grid / Bollinger from a closes array.
    The chart shows the full 1Y window so 1Y min/max lines intersect the curve."""
    if not closes:
        return
    window = closes[-260:] if len(closes) >= 260 else closes
    etf['price_history'] = [round(c, 4) for c in window]
    if window:
        etf['min_price_1y'] = round(min(window), 4)
        etf['max_price_1y'] = round(max(window), 4)
        if etf['min_price_1y'] > 0 and latest_price:
            etf['pct_from_low'] = round(
                (latest_price - etf['min_price_1y']) / etf['min_price_1y'] * 100, 2
            )
        etf.update(calc_grid(etf['min_price_1y'], etf['max_price_1y'], latest_price))
    bb_mid, bb_upper, bb_lower = calc_bollinger(window, period=20)
    if bb_lower is not None and len(bb_lower) > 0:
        # Full valid BB series (drop the first 19 NaN from rolling window), right-aligned to chart
        bb_lo = [round(float(v), 4) for v in bb_lower if not np.isnan(v)]
        if bb_lo:
            etf['bb_lower_history'] = bb_lo
        ll = float(bb_lower[-1])
        if not np.isnan(ll) and ll > 0:
            etf['bb_lower'] = round(ll, 4)
            if latest_price:
                etf['pct_from_bb_low'] = round((latest_price - ll) / ll * 100, 2)
        bb_hi = [round(float(v), 4) for v in bb_upper if not np.isnan(v)]
        if bb_hi:
            etf['bb_upper_history'] = bb_hi
            etf['bb_upper'] = round(float(bb_upper[-1]), 4) if not np.isnan(bb_upper[-1]) else None


def fetch_prices_quick(progress_cb=None):
    """
    Fast refresh: baostock latest close (fast) + cached 累计净值 (前复权).
    No akshare calls — keeps ~3s speed; 1Y range/grid/chart come from NAV cache.
    """
    def log(msg):
        if progress_cb:
            progress_cb(msg)
        print(f'[{bj_now():%H:%M:%S}] {msg}')

    meta = load_metadata()
    nav_cache = load_nav_cache()
    results = []
    end_date = bj_now().strftime('%Y-%m-%d')
    start_date = (bj_now() - timedelta(days=10)).strftime('%Y-%m-%d')

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
            'name': etf_meta.get('name') or ETF_NAMES.get(code, code),
            'latest_price': 0,
            'price_history': None,
            'min_price_1y': None,
            'max_price_1y': None,
            'grid_25': None,
            'grid_382': None,
            'grid_50': None,
            'grid_618': None,
            'grid_75': None,
            'grid_zone': None,
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
            'corr_300': etf_meta.get('corr_300'),
        }
        try:
            rs = bs.query_history_k_data_plus(
                bs_code, 'date,close',
                start_date=start_date, end_date=end_date,
                frequency='d', adjustflag='3'
            )
            if rs.error_code == '0' and rs.data:
                etf['latest_price'] = round(float(rs.data[-1][1]), 4)
        except Exception as e:
            log(f'  Quick K-line ERROR {code}: {e}')

        nav = nav_cache.get(code)
        adj = nav_to_adj_closes(nav, etf.get('latest_price'))
        if adj:
            compute_from_closes(etf, adj, etf.get('latest_price'))

        results.append(etf)

    try:
        bs.logout()
    except Exception:
        pass

    calc_correlations(results)
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
            # If no metadata or NAV cache exists, fall back to full
            if data and (not load_metadata() or not load_nav_cache()):
                log_msg = lambda m: print(f'[{bj_now():%H:%M:%S}] {m}')
                log_msg('No metadata/NAV cache, falling back to full fetch...')
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


# ── PushPlus Notification ───────────────────────────────────────

def send_pushplus(token: str, title: str, content: str, template: str = 'html') -> bool:
    """Send push notification via PushPlus WeChat."""
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
            return result.get('code') == 200
    except Exception as e:
        print(f'[{bj_now():%H:%M:%S}] PushPlus error: {e}')
        return False


def build_push_html(etfs) -> str:
    """Build compact HTML table for WeChat push."""
    now_str = bj_now().strftime('%Y-%m-%d %H:%M')
    rows = ''
    for e in etfs:
        code = e.get('code', '-')
        name = e.get('name', code)
        price = e.get('latest_price', 0)
        low = e.get('min_price_1y')
        high = e.get('max_price_1y')
        zone = e.get('grid_zone')
        zone_str = f'{zone}区' if zone is not None else '--'
        low_str = f'{low:.3f}' if low is not None else '--'
        high_str = f'{high:.3f}' if high is not None else '--'
        rows += (
            f'<tr>'
            f'<td style="text-align:left">{name}<br><span style="font-size:10px;color:#8892b0">{code}</span></td>'
            f'<td style="font-weight:600">{price:.3f}</td>'
            f'<td>{low_str}</td><td>{high_str}</td><td>{zone_str}</td>'
            f'</tr>'
        )
    return (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8"></head>'
        f'<body style="font-family:sans-serif;padding:8px;background:#fff">'
        f'<h3>ETF Screener Report</h3>'
        f'<p style="color:#64748b;font-size:12px">{now_str} | {len(etfs)} ETFs</p>'
        f'<table style="width:100%;border-collapse:collapse;font-size:11px">'
        f'<tr style="background:#f1f5f9"><th>name</th><th>latest</th><th>1Y low</th><th>1Y High</th><th>zone</th></tr>'
        f'{rows}</table></body></html>'
    )


# ── HTTP Handler ─────────────────────────────────────────────────

class ETFHandler(SimpleHTTPRequestHandler):
    """Custom handler: API endpoints + static file serving."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def log_message(self, format, *args):
        print(f'[{bj_now():%H:%M:%S}] {args[0]}')

    def end_headers(self):
        # Bust browser cache for static files (so HTML edits show up on refresh)
        if self.path and not self.path.startswith('/api/'):
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        super().end_headers()

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

        # ── API: Export Excel ─────────────────────────────────
        if path == '/api/export':
            data = get_data(force_refresh=False, full=False)
            self._export_excel(data)
            return

        # ── API: Refresh data ─────────────────────────────────
        if path == '/api/refresh':
            # Default: full refresh (akshare+baostock, ~130s). Use ?quick=1 for fast mode (~3s).
            quick = qs.get('quick', ['0'])[0] == '1'
            async_mode = qs.get('async', ['0'])[0] == '1'

            def do_refresh():
                return get_data(force_refresh=True, full=not quick)

            if async_mode:
                t = threading.Thread(target=do_refresh, daemon=True)
                t.start()
                mode = 'quick (~3s)' if quick else 'full (~130s)'
                self._json_response({'status': 'ok', 'msg': f'Refresh started: {mode}'})
            else:
                data = do_refresh()
                self._json_response({
                    'status': 'ok',
                    'count': len(data) if data else 0,
                    'msg': f'Refreshed {len(data) if data else 0} ETFs',
                })
            return

        # ── API: PushPlus WeChat push (manual button only) ────
        if path == '/api/pushplus':
            push_token = os.getenv('PUSHPLUS_TOKEN', '')
            if not push_token:
                self._json_response({'status': 'error', 'msg': 'PUSHPLUS_TOKEN not configured'})
                return
            data = get_data(force_refresh=False, full=False)
            if not data:
                self._json_response({'status': 'error', 'msg': 'No data available'})
                return
            # Sort by grid zone ascending (None last)
            data = sorted(data, key=lambda e: (e.get('grid_zone') is None, e.get('grid_zone') or 0))
            try:
                html = build_push_html(data)
                ok = send_pushplus(push_token, f'ETF Screener ({len(data)} ETFs)', html)
                print(f'[{bj_now():%H:%M:%S}] PushPlus: {"OK" if ok else "FAIL"}')
                self._json_response({'status': 'ok' if ok else 'error',
                                     'msg': 'PushPlus sent' if ok else 'PushPlus failed'})
            except Exception as ex:
                print(f'[{bj_now():%H:%M:%S}] PushPlus error: {ex}')
                self._json_response({'status': 'error', 'msg': f'PushPlus error: {ex}'})
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

    def _export_excel(self, data):
        """Generate and serve Excel file from ETF data."""
        if not data:
            self._json_response({'error': 'No data available'})
            return
        rows = []
        for e in data:
            rows.append({
                '代码': e.get('code', ''),
                '名称': e.get('name', ''),
                '最新价': e.get('latest_price', 0),
                '折溢价(%)': e.get('discount_rate', 0),
                '涨跌幅(%)': e.get('change_pct', 0),
                '规模(亿)': e.get('fund_size_yi', 0),
                '费率': e.get('total_fee') or e.get('mgmt_fee', '--'),
                'PE分位(%)': e.get('pe_percentile'),
                'PE当前': e.get('pe_current'),
                '距1Y低点(%)': e.get('pct_from_low'),
                '1Y最低': e.get('min_price_1y'),
                '1Y最高': e.get('max_price_1y'),
                '网格0.25': e.get('grid_25'),
                '网格0.382': e.get('grid_382'),
                '网格0.5': e.get('grid_50'),
                '网格0.618': e.get('grid_618'),
                '网格0.75': e.get('grid_75'),
                '网格区域': e.get('grid_zone'),
                'BB上轨': e.get('bb_upper'),
                'BB下轨': e.get('bb_lower'),
                '换手率(%)': e.get('turnover_rate', 0),
            })
        df = pd.DataFrame(rows)
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='ETFs', index=False)
            # Auto-adjust column widths
            ws = writer.sheets['ETFs']
            for col_idx, col_name in enumerate(df.columns, 1):
                max_len = max(
                    df[col_name].astype(str).str.len().max(),
                    len(str(col_name))
                )
                ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = min(max_len + 4, 40)
        body = output.getvalue()
        filename = f'etf_screener_{bj_now().strftime("%Y%m%d_%H%M")}.xlsx'
        self.send_response(200)
        self.send_header('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.send_header('Content-Length', str(len(body)))
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
