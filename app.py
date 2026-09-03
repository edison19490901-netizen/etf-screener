"""
ETF Screener Backend — HTTP Server
Start: python app.py
Serves ETF dashboard + /api/etf_data endpoint
Data sources: akshare (spot/fee/PE/累计净值) + baostock (latest-price fallback)
前复权 K-line derived from 累计净值 (baostock ignores adjustflag for ETFs).
"""
import json, os, sys, time, threading, re, hashlib
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
    '159338',  # 中证A500ETF国泰（宽基核心·中证A500，PE分位暂缺）
    '515880',  # 通信ETF国泰
    '588780',  # 科创芯片设计ETF国联安
    '159796',  # 电池ETF汇添富
    '510050',  # 上证50ETF华夏
    '159367',  # 创业板50ETF华夏
    '159150',  # 深证50ETF易方达
    '159212',  # 深100ETF南方
    '159227',  # 航空航天ETF华夏
    '159030',  # 粮食ETF华夏
    # 对冲仓 — 防御/避险
    '518680',  # 黄金ETF华夏（费率0.2% vs 华安0.6%）
    '511260',  # 十年国债ETF国泰
    '512890',  # 红利低波ETF华泰柏瑞
]
BENCHMARK_ETF = '510300'  # 对冲基准：沪深300

# Fallback names for ETFs not covered by akshare spot data
ETF_NAMES = {
    '159338': '中证A500ETF国泰',
    '159030': '粮食ETF华夏',
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


# ════════════════════ 行情解读 Analysis ════════════════════

# 用户持仓 ETF（对应 HANDOFF.md 持仓清单，可自行增删）
HOLDINGS = ['159928', '510300', '159741', '588780', '515880']
# 对冲仓 ETF（黄金 / 十年国债 / 红利低波）
HEDGE_ETFS = ['518680', '511260', '512890']

ZONE_LABELS = {
    1: '低估·重仓买入', 2: '偏低·加仓', 3: '偏低·加仓',
    4: '偏高·持有/观望', 5: '偏高·减仓', 6: '不操作',
}


def compute_trend_stats(closes):
    """Given ~260 daily closes (前复权), compute trend indicators (ret_5/ret_20/MA20/MA60/trend).
    Returns a dict, or None when data is missing/insufficient."""
    if not isinstance(closes, (list, tuple, pd.Series)):
        return None
    arr = np.asarray([float(c) for c in closes if c is not None], dtype=float)
    arr = arr[~np.isnan(arr)]
    n = len(arr)
    if n < 20:
        return None
    price = float(arr[-1])

    def pct(d):
        if n <= d or arr[-1 - d] <= 0:
            return None
        return round((price / float(arr[-1 - d]) - 1) * 100, 2)

    def ma(w):
        if n < w:
            return None
        return float(arr[-w:].mean())

    ma20, ma60 = ma(20), ma(60)
    trend = '震荡'
    if ma20 is not None and ma60 is not None:
        if price > ma20 > ma60:
            trend = '上升'
        elif price < ma20 < ma60:
            trend = '下降'
    return {
        'ret_5': pct(5),
        'ret_20': pct(20),
        'ma20': round(ma20, 2) if ma20 is not None else None,
        'ma60': round(ma60, 2) if ma60 is not None else None,
        'above_ma20': bool(ma20 is not None and price > ma20),
        'above_ma60': bool(ma60 is not None and price > ma60),
        'trend': trend,
    }


def _zone_int(e):
    """Safe int conversion of grid_zone (None / NaN → None)."""
    z = e.get('grid_zone')
    try:
        z = float(z)
        return int(z) if pd.notna(z) else None
    except (TypeError, ValueError):
        return None


def _zone_bounds(e):
    """Return (lo, hi) price range of current grid zone, or None."""
    z = _zone_int(e)
    if z is None:
        return None
    m = {
        1: ('min_price_1y', 'grid_25'),
        2: ('grid_25', 'grid_382'),
        3: ('grid_382', 'grid_50'),
        4: ('grid_50', 'grid_618'),
        5: ('grid_618', 'grid_75'),
        6: ('grid_75', 'max_price_1y'),
    }.get(z)
    if not m:
        return None
    lo, hi = e.get(m[0]), e.get(m[1])
    if lo is None or hi is None:
        return None
    lo, hi = float(lo), float(hi)
    if lo <= 0 or hi <= lo:
        return None
    return lo, hi


def _zone_position(e):
    """Position of current price within its grid zone: 下沿 / 中部 / 上沿, or ''."""
    b = _zone_bounds(e)
    if not b:
        return ''
    lo, hi = b
    pos = (safe_float(e.get('latest_price'), 0) - lo) / (hi - lo)
    if pos <= 0.33:
        return '下沿'
    if pos >= 0.66:
        return '上沿'
    return '中部'


def _trend_phrase(t):
    """Short natural-language trend phrase from compute_trend_stats result."""
    if not t or not t.get('trend'):
        return ''
    r20 = t.get('ret_20')
    rs = '--' if r20 is None else f"{r20:+.2f}%"
    if t['trend'] == '上升':
        return f"趋势偏多（近20日{rs}）"
    if t['trend'] == '下降':
        return f"趋势偏弱（近20日{rs}）"
    return f"趋势震荡（近20日{rs}）"


def _fmt_signed(v):
    return '--' if v is None else f"{v:+.2f}%"


def build_analysis(etfs):
    """Compute 行情解读: overall overview + highlight groups + markdown report.
    Returns a JSON-serializable dict, or None when data is empty."""
    if not etfs:
        return None

    rows = []
    for e in etfs:
        row = dict(e)
        closes = row.get('price_history')
        row['_trend'] = compute_trend_stats(closes) if closes else None
        rows.append(row)

    n = len(rows)
    attack = [r for r in rows if r['code'] not in HEDGE_ETFS]
    hedge = [r for r in rows if r['code'] in HEDGE_ETFS]
    holding_sel = [r for r in rows if r['code'] in HOLDINGS]

    # ── Overview stats ──
    zone_dist = {str(z): 0 for z in range(1, 7)}
    for r in rows:
        z = _zone_int(r)
        if z is not None and 1 <= z <= 6:
            zone_dist[str(z)] += 1
    buy_count = zone_dist['1'] + zone_dist['2']
    zone_buy_pct = round(buy_count / n * 100, 1) if n else 0.0

    lows = [safe_float(r.get('pct_from_low'), 0) for r in rows if r.get('pct_from_low') is not None]
    avg_pct_from_low = round(sum(lows) / len(lows), 1) if lows else None

    chgs = [safe_float(r.get('change_pct'), 0) for r in rows if r.get('change_pct') is not None]
    avg_chg = round(sum(chgs) / len(chgs), 2) if chgs else None

    ret5s = [r['_trend']['ret_5'] for r in rows if r['_trend'] and r['_trend'].get('ret_5') is not None]
    ret20s = [r['_trend']['ret_20'] for r in rows if r['_trend'] and r['_trend'].get('ret_20') is not None]
    above20 = sum(1 for r in rows if r['_trend'] and r['_trend'].get('above_ma20'))
    above60 = sum(1 for r in rows if r['_trend'] and r['_trend'].get('above_ma60'))
    with_trend = sum(1 for r in rows if r['_trend'])
    avg_ret_5 = round(sum(ret5s) / len(ret5s), 2) if ret5s else None
    avg_ret_20 = round(sum(ret20s) / len(ret20s), 2) if ret20s else None
    above_ma20_pct = round(above20 / with_trend * 100, 0) if with_trend else None
    above_ma60_pct = round(above60 / with_trend * 100, 0) if with_trend else None

    corrs = [safe_float(r.get('corr_300'), 0) for r in hedge if r.get('corr_300') is not None]
    avg_corr = round(sum(corrs) / len(corrs), 2) if corrs else None

    direction = '震荡分化'
    if avg_ret_20 is not None and above_ma20_pct is not None:
        if avg_ret_20 > 2 and above_ma20_pct > 60:
            direction = '整体偏强'
        elif avg_ret_20 < -2 or above_ma20_pct < 40:
            direction = '整体偏弱'

    if avg_corr is None:
        hedge_note = ''
    elif avg_corr < 0.1:
        hedge_note = f"，对冲仓与沪深300平均相关{avg_corr:+.2f}，对冲效果好"
    elif avg_corr < 0.4:
        hedge_note = f"，对冲仓平均相关{avg_corr:+.2f}，对冲效果一般"
    else:
        hedge_note = f"，对冲仓平均相关{avg_corr:+.2f}，注意对冲有效性"

    summary = f"共跟踪 {n} 只ETF（攻击 {len(attack)} + 对冲 {len(hedge)}）"
    if holding_sel:
        summary += f"，持仓 {len(holding_sel)} 只"
    summary += f"，低估区（1-2区）占 {zone_buy_pct}%"
    if avg_pct_from_low is not None:
        summary += f"，距1年低点平均 +{avg_pct_from_low}%"
    summary += f"，整体{direction}"
    if avg_ret_20 is not None:
        summary += f"（近20日平均{avg_ret_20:+.2f}%）"
    summary += hedge_note + "。"
    if zone_buy_pct >= 30:
        summary += "仍有三成以上ETF处于低估买入区，可逢低分批布局。"
    elif zone_buy_pct >= 15:
        summary += "部分ETF仍处低估区，可择优关注。"
    else:
        summary += "多数ETF已脱离低估区，追涨需谨慎。"

    # ── 重点 ETF 挑选 ──
    def make_h(r, group):
        t = r.get('_trend') or {}
        corr = r.get('corr_300')
        return {
            'code': r.get('code', ''),
            'name': r.get('name', ''),
            'price': round(safe_float(r.get('latest_price'), 0), 4),
            'zone': _zone_int(r),
            'is_holding': bool(r.get('code') in HOLDINGS),
            'is_hedge': bool(r.get('code') in HEDGE_ETFS),
            'group': group,
            'trend': t.get('trend'),
            'ret_5': t.get('ret_5'),
            'ret_20': t.get('ret_20'),
            'change_pct': safe_float(r.get('change_pct'), 0),
            'corr_300': round(corr, 3) if corr is not None else None,
            'discount_rate': safe_float(r.get('discount_rate'), 0),
            'pe_percentile': r.get('pe_percentile'),
            'pct_from_low': round(safe_float(r.get('pct_from_low'), 0), 1) if r.get('pct_from_low') is not None else None,
            'description': '',
        }

    def zone_pos_val(r):
        b = _zone_bounds(r)
        if not b:
            return 99.0
        lo, hi = b
        return (safe_float(r.get('latest_price'), 0) - lo) / (hi - lo)

    buy_sel = [r for r in rows if _zone_int(r) in (1, 2)]
    buy_sel.sort(key=lambda r: (int(r['grid_zone']), zone_pos_val(r)))
    buy_sel = buy_sel[:5]

    movers = [r for r in rows if r.get('change_pct') is not None]
    movers.sort(key=lambda r: safe_float(r.get('change_pct'), 0), reverse=True)
    mover_sel = (movers[:3] + (movers[-3:] if len(movers) >= 3 else [])) if movers else []

    warnings = []
    for r in rows:
        z = _zone_int(r)
        disc = safe_float(r.get('discount_rate'), 0)
        pe = r.get('pe_percentile')
        if z == 6:
            warnings.append((r, "处于6区（高位/不操作），已接近1年高点"))
        if disc > 0.3:
            warnings.append((r, f"场内溢价 {disc:+.2f}%，注意估值泡沫"))
        if pe is not None and safe_float(pe, 0) > 80:
            warnings.append((r, f"PE分位 {safe_float(pe, 0):.0f}%，估值偏高"))

    pairs = []
    for r in buy_sel:
        pairs.append((make_h(r, 'buy'), r))
    for r in hedge:
        pairs.append((make_h(r, 'hedge'), r))
    for r in holding_sel:
        pairs.append((make_h(r, 'holding'), r))
    for r in mover_sel:
        pairs.append((make_h(r, 'mover'), r))
    for r, reason in warnings:
        h = make_h(r, 'warning')
        h['description'] = reason
        pairs.append((h, r))

    highlights = []
    for h, r in pairs:
        t = r.get('_trend') or {}
        z = h['zone']
        zlbl = ZONE_LABELS.get(z, '')
        pl = '--' if h['pct_from_low'] is None else f"+{h['pct_from_low']}%"
        if h['group'] == 'buy':
            pos = _zone_position(r)
            h['description'] = (f"处于{z}区（{zlbl}）{('·' + pos) if pos else ''}，"
                                f"距1年低点{pl}。{_trend_phrase(t)}")
        elif h['group'] == 'hedge':
            corr_s = '--' if h['corr_300'] is None else f"{h['corr_300']:+.2f}"
            h['description'] = (f"对冲ETF：处于{z}区（{zlbl}），与沪深300相关性 {corr_s}。"
                                f"{_trend_phrase(t)}")
        elif h['group'] == 'holding':
            h['description'] = f"持仓ETF：处于{z}区（{zlbl}），距1年低点{pl}。{_trend_phrase(t)}"
        elif h['group'] == 'mover':
            h['description'] = f"今日{_fmt_signed(h['change_pct'])}，{_trend_phrase(t)}"
        # warning 组 description 已在上方赋值，跳过
        highlights.append(h)

    analysis = {
        'generated_at': bj_now().strftime('%Y-%m-%d %H:%M'),
        'overview': {
            'total': n,
            'attack': len(attack),
            'hedge': len(hedge),
            'holdings': len(holding_sel),
            'zone_dist': zone_dist,
            'zone_buy_pct': zone_buy_pct,
            'avg_pct_from_low': avg_pct_from_low,
            'avg_chg': avg_chg,
            'avg_ret_5': avg_ret_5,
            'avg_ret_20': avg_ret_20,
            'above_ma20_pct': above_ma20_pct,
            'above_ma60_pct': above_ma60_pct,
            'avg_corr': avg_corr,
            'direction': direction,
            'summary': summary,
        },
        'highlights': highlights,
    }
    analysis['report'] = build_report_markdown(analysis)
    return analysis


def build_report_markdown(a):
    """Assemble the PushPlus Markdown report from an analysis dict."""
    o = a['overview']
    L = []
    date_str = a.get('generated_at', '')[:10]
    L.append(f"# ETF 行情解读 {date_str}")
    L.append('')
    L.append('## 一、整体概览')
    L.append(f"- 跟踪ETF：**{o['total']}** 只（攻击 {o['attack']} + 对冲 {o['hedge']}"
             f"{'，持仓 ' + str(o['holdings']) + ' 只' if o.get('holdings') else ''}）")
    dist = ' / '.join(f"{k}区 {v}只" for k, v in sorted(o['zone_dist'].items()))
    L.append(f"- 网格区间：{dist}；**低估区（1-2区）占比 {o['zone_buy_pct']}%**")
    if o.get('avg_pct_from_low') is not None:
        L.append(f"- 距1年低点平均 **+{o['avg_pct_from_low']}%**")
    if o.get('avg_chg') is not None:
        L.append(f"- 今日平均涨跌 **{o['avg_chg']:+.2f}%**")
    if o.get('avg_ret_5') is not None:
        L.append(f"- 近5日平均 {o['avg_ret_5']:+.2f}%，近20日平均 {o['avg_ret_20']:+.2f}%；"
                 f"站上20日均线 {o['above_ma20_pct']:.0f}%，站上60日均线 {o['above_ma60_pct']:.0f}%")
    if o.get('avg_corr') is not None:
        L.append(f"- 对冲仓与沪深300平均相关性 **{o['avg_corr']:+.2f}**")
    L.append('')
    L.append(f"**整体判断**：{o['summary']}")
    L.append('')

    group_titles = {
        'buy': '接近买入点（低估/偏低，可关注加仓）',
        'hedge': '对冲仓状态',
        'holding': '持仓ETF状态',
        'mover': '今日异动',
        'warning': '风险警示',
    }
    numerals = ['二', '三', '四', '五', '六', '七']
    idx = 0
    for g in ('buy', 'hedge', 'holding', 'mover', 'warning'):
        items = [h for h in a['highlights'] if h['group'] == g]
        if not items:
            continue
        L.append(f"## {numerals[idx]}、{group_titles[g]}（{len(items)}只）")
        if g == 'mover':
            items = sorted(items, key=lambda h: h['change_pct'] if h['change_pct'] is not None else -999, reverse=True)
        for i, h in enumerate(items, 1):
            zs = f"{h['zone']}区" if h['zone'] else '--'
            L.append(f"{i}. **{h['name']}**（{h['code']}）｜现价 {h['price']}｜{zs}")
            L.append(f"   {h['description']}")
        L.append('')
        idx += 1

    L.append('---')
    L.append('数据来源：akshare + baostock ｜ 仅供参考，不构成投资建议。')
    return '\n'.join(L)


# ── Auth (password page) ──────────────────────────────────────────

PASSWORD_FILE = BASE_DIR / 'password.html'
DASHBOARD_PASSWORD = os.getenv('DASHBOARD_PASSWORD', '')
AUTH_COOKIE_NAME = 'etf_auth'

def _make_token(password):
    return hashlib.sha256(f'dash-salt-{password}'.encode()).hexdigest()

def _parse_cookies(handler):
    cookie_header = handler.headers.get('Cookie', '')
    cookies = {}
    for item in cookie_header.split(';'):
        item = item.strip()
        if '=' in item:
            k, v = item.split('=', 1)
            cookies[k.strip()] = v.strip()
    return cookies

def _check_auth(handler):
    if not DASHBOARD_PASSWORD:
        return True  # No password set = open access
    cookies = _parse_cookies(handler)
    token = cookies.get(AUTH_COOKIE_NAME, '')
    return token == _make_token(DASHBOARD_PASSWORD)


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

        # ── API: 行情解读 Analysis ───────────────────────────
        if path == '/api/analysis':
            self._api_analysis()
            return

        # ── API: Health check ─────────────────────────────────
        if path == '/api/health':
            self._json_response({'status': 'ok', 'time': bj_now().isoformat()})
            return

        # ── Static files (password-gated) ─────────────────────
        # Root: serve password page if configured, else redirect to dashboard
        if path == '/' or path == '':
            if DASHBOARD_PASSWORD and not _check_auth(self):
                self._serve_password()
            else:
                self._redirect('/etf_dashboard.html')
            return

        # Dashboard: require auth when password configured
        if path == '/etf_dashboard.html':
            if DASHBOARD_PASSWORD and not _check_auth(self):
                self._redirect('/')
            else:
                super().do_GET()   # serve the dashboard file
            return

        return super().do_GET()

    def _load_analysis_etfs(self):
        """Load ETF data for analysis: cached data (quick), no refresh."""
        return get_data(force_refresh=False, full=False) or []

    def _api_analysis(self):
        """GET /api/analysis — 行情解读（整体概览 + 重点分组 + markdown 报告）"""
        try:
            etfs = self._load_analysis_etfs()
            if not etfs:
                self._json_response({'ok': False, 'error': 'No data available for analysis'})
                return
            analysis = build_analysis(etfs)
            if not analysis:
                self._json_response({'ok': False, 'error': 'Analysis produced no result'})
                return
            self._json_response({'ok': True, 'analysis': analysis})
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._json_response({'ok': False, 'error': f'Analysis error: {e}'})

    def _api_pushplus_analysis(self):
        """POST /api/pushplus_analysis — 推送行情解读到微信（markdown）"""
        token = os.getenv('PUSHPLUS_TOKEN', '')
        if not token:
            self._json_response({'ok': False, 'error': 'PUSHPLUS_TOKEN not configured'})
            return
        try:
            etfs = self._load_analysis_etfs()
            if not etfs:
                self._json_response({'ok': False, 'error': 'No data available to push'})
                return
            analysis = build_analysis(etfs)
            report = analysis['report']
            title = f"ETF 行情解读 {analysis['generated_at'][:10]}"
            ok = send_pushplus(token, title, report, 'markdown')
            print(f'[{bj_now():%H:%M:%S}] PushPlus analysis: {"OK" if ok else "FAIL"} (size={len(report)} chars)')
            self._json_response({'ok': ok, 'message': '解读已推送微信' if ok else 'PushPlus 发送失败'})
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._json_response({'ok': False, 'error': f'PushPlus analysis error: {e}'})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == '/login':
            self._handle_login()
            return
        if path == '/api/pushplus_analysis':
            self._api_pushplus_analysis()
            return
        self._json_response({'status': 'error', 'msg': 'Not found'})

    def _serve_password(self):
        if PASSWORD_FILE.exists():
            content = PASSWORD_FILE.read_bytes()
        else:
            content = b'<h1>password.html not found</h1>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.end_headers()
        self.wfile.write(content)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header('Location', location)
        self.end_headers()

    def _handle_login(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')
        params = parse_qs(body)
        password = params.get('password', [''])[0]

        if not DASHBOARD_PASSWORD:
            # No password configured — allow access
            token = _make_token('')
            self.send_response(302)
            self.send_header('Set-Cookie', f'{AUTH_COOKIE_NAME}={token}; Path=/; HttpOnly; Max-Age=86400; SameSite=Lax')
            self.send_header('Location', '/etf_dashboard.html')
            self.end_headers()
        elif password == DASHBOARD_PASSWORD:
            token = _make_token(password)
            self.send_response(302)
            self.send_header('Set-Cookie', f'{AUTH_COOKIE_NAME}={token}; Path=/; HttpOnly; Max-Age=86400; SameSite=Lax')
            self.send_header('Location', '/etf_dashboard.html')
            self.end_headers()
        else:
            self.send_response(302)
            self.send_header('Location', '/?err=1')
            self.end_headers()

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
    parser.add_argument('--port', type=int, default=int(os.getenv('PORT', '8081')))
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
