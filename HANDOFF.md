---
name: etf-screener-handoff
description: "ETF Screener complete project handoff — architecture, deployment, hedging strategy, portfolio config"
metadata: 
  node_type: memory
  type: project
  originSessionId: d049ee4a-be38-421b-96ea-6047203cb3fc
  modified: 2026-08-10T02:59:32.538Z
---

# ETF Screener — Project Handoff

## Quick Links

- **Repo**: `git@github.com:edison19490901-netizen/etf-screener.git` (SSH)
- **Render URL**: `https://etf-screener.onrender.com`
- **Local**: `D:\Claudeee\dashboard-of-high_divided_screen\etf-screener\`
- **Server start**: `cd etf-screener && python app.py`
- **Push**: `git push origin main` (remote is SSH — do NOT use HTTPS)

## Architecture

| Layer | Tech | Notes |
|-------|------|-------|
| Backend | Python 3 `http.server` | No Flask — matches parent project pattern |
| Data sources | akshare + baostock | akshare: spot/fee/PE/累计净值 (~130s) · baostock: latest-price fallback |
| Frontend | Single HTML file | Self-contained, dark theme, Canvas charts |
| Deploy | Render Web Service | Auto-deploys on git push to main |
| Push | PushPlus | WeChat notification after Full Refresh |

## Dual Refresh Modes

| | Quick (~3s) | Full (~130s) |
|---|---|---|
| K-line / BB / 1Y low (前复权) | ✅ cached 累计净值 | ✅ akshare 累计净值 |
| Spot price | ✅ baostock (last close) | ✅ akshare |
| Discount / size / fees / PE | ❌ cached | ✅ akshare |
| PushPlus notify | ❌ | ✅ (if PUSHPLUS_TOKEN set) |

Default mode is **Quick**. Full mode runs on explicit button click or daily cron.

**前复权 K-line**: baostock ignores `adjustflag` for ETFs (returns 不复权), so split jumps corrupt 1Y low/high. The K-line is instead derived from 累计净值 (cumulative NAV, akshare `fund_open_fund_info_em`) rescaled to the current price — smooth across splits/dividends. Chart shows full 1Y (260 trading days) so 1Y min/max and grid lines intersect the curve.

## 18 Tracked ETFs

### Attack basket (15)
516080, 159061, 159611, 159692, 516120, 516770, 515550, 159562, 588080, 159741, 159928, 510300, 515880, 588780, 159796(电池ETF汇添富)

### Hedge basket (added 3)
- **518680** 黄金ETF华夏 — fee 0.20%, corr +0.25 vs CSI300
- **511260** 十年国债ETF国泰 — fee 0.15%, corr -0.07 vs CSI300 🟢
- **512890** 红利低波ETF华泰柏瑞 — fee 0.50%, corr -0.23 vs CSI300 🟢

Benchmark for correlation: `BENCHMARK_ETF = '510300'` (沪深300).

## User Portfolio (2026-08-10)

| Holding | Type | Value (CNY) | Weight |
|---------|------|-------------|--------|
| 600690 海尔智家 | Stock | 17,728 | 47.4% ⚠️ |
| 159928 消费ETF | ETF | 5,985 | 16.0% |
| 510300 沪深300ETF | ETF | 4,266 | 11.4% |
| 159741 恒生科技ETF | ETF | 2,444 | 6.5% |
| 588780 科创芯片ETF | ETF | 2,388 | 6.4% |
| 003816 中国广核 | Stock | 2,370 | 6.3% |
| 515880 通信ETF | ETF | 2,233 | 6.0% |
| **Total** | | **37,414** | |

## Hedging Strategy (Route A — Long Only)

Target: ~58,000 CNY total (attack 64% / hedge 36%)

Add to portfolio:
- 518680 黄金ETF — ~8,000 CNY (~1,300 shares @ ~6.15)
- 511260 十年国债ETF — ~7,000 CNY (~60 shares @ ~116)
- 512890 红利低波ETF — ~6,000 CNY (~3,500 shares @ ~1.72)

Risk actions:
1. Reduce 600690 from 47% → target <30%
2. Quarterly rebalance: hedge allocation ±10% trigger
3. Any single holding >30% → trim

## Grid Trading Strategy (网格交易)

Per-ETF grid between **1Y low** and **1Y high**, 5 Fibonacci lines (0.25 / 0.382 / 0.5 / 0.618 / 0.75) → 6 zones (bottom→top: 1..6):

| Zone | Range | Action | 份数 |
|------|-------|--------|------|
| 1区 | 低 ~ 0.25 | 低估·重仓买入 | 2 份 |
| 2区 | 0.25 ~ 0.382 | 偏低·加仓 | 2 份 |
| 3区 | 0.382 ~ 0.5 | 偏低·加仓 | 2 份 |
| 4区 | 0.5 ~ 0.618 | 偏高·持有/观望 | 2 份 |
| 5区 | 0.618 ~ 0.75 | 偏高·减仓 | 2 份 |
| 6区 | 0.75 ~ 高 | 不操作 | 0 份 |

- 1-5区各 2 份、6区不操作，共 10 份；每份 = 该 ETF 网格总预算 ÷ 10。
- Backend fields: `grid_25`, `grid_382`, `grid_50`, `grid_618`, `grid_75`, `grid_zone` (computed in `calc_grid()`).
- Frontend table: 1Y最高/1Y最低/5线价格/区域，当前区域支撑线高亮；card K线画 1Y高线 + 5条网格线。
- 工具栏「网格预算/只(元)」输入框（存 localStorage `etf_budget`）：1-5区各2份、6区不操作；表格「建议投入」列显示 1-6 区金额（6区=不操作），当前区域高亮。
- Card 布局：头部（名称+代码 左 / 最新价 右，下方实线分隔）；正文左列 1Y最低/0.25/0.382/0.5，右列 0.618/0.75/1Y最高，网格区域跨行右对齐。

## Key Code Locations

| File | What |
|------|------|
| `app.py:49-71` | ETF_LIST + BENCHMARK_ETF |
| `app.py:74-88` | ETF_NAMES dict |
| `app.py:92-104` | safe_float/safe_int helpers |
| `app.py:107-154` | calc_bollinger() (20-day) + calc_grid() (5线→6区) |
| `app.py:156-384` | fetch_all_data() (Full refresh) |
| `app.py:386-411` | calc_correlations() (60-day vs 510300) |
| `app.py:459-556` | fetch_nav_series() + nav_to_adj_closes() + compute_from_closes() — 前复权 K线/1Y/网格/BB |
| `app.py:558-644` | fetch_prices_quick() (Quick refresh) |
| `app.py:687-737` | get_data() cache logic |
| `app.py:739-954` | send_pushplus() + build_push_html() + HTTP API handlers |
| `etf_dashboard.html` | Full frontend |

## Environment Variables

| Var | Required | Default | Where |
|-----|----------|---------|-------|
| PORT | Yes | 8080 | Render auto-sets |
| PUSHPLUS_TOKEN | No | — | PushPlus WeChat token |

## Troubleshooting

- **GitHub push fails (HTTPS)**: GFW blocks 443. Use SSH (`git@github.com:...`)
- **NaN ValueError on Render**: Fixed with safe_float/safe_int in `app.py:77-96`
- **PE shows "--"**: Only 510300+515550 have PE data (12-index limit in akshare)
- **New ETF has no name**: Add to `ETF_NAMES` dict in `app.py:68-72`

**Why:** Full project context for continuing work or handoff to another developer.
**How to apply:** Read this file before making changes to ETF screener. Update it when portfolio or strategy changes.
