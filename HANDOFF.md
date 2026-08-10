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
| Data sources | akshare + baostock | akshare: spot/fee/PE (~130s) · baostock: K-line (~3s) |
| Frontend | Single HTML file | Self-contained, dark theme, Canvas charts |
| Deploy | Render Web Service | Auto-deploys on git push to main |
| Push | PushPlus | WeChat notification after Full Refresh |

## Dual Refresh Modes

| | Quick (~3s) | Full (~130s) |
|---|---|---|
| K-line / BB / 1Y low | ✅ baostock | ✅ baostock |
| Spot price / discount | ❌ cached | ✅ akshare |
| Fund size / fees / PE | ❌ cached | ✅ akshare |
| PushPlus notify | ❌ | ✅ (if PUSHPLUS_TOKEN set) |

Default mode is **Quick**. Full mode runs on explicit button click or daily cron.

## 17 Tracked ETFs

### Attack basket (original 14)
516080, 159061, 159611, 159692, 516120, 516770, 515550, 159562, 588080, 159741, 159928, 510300, 515880, 588780

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

## Key Code Locations

| File | What |
|------|------|
| `app.py:46-65` | ETF_LIST + BENCHMARK_ETF |
| `app.py:77-100` | safe_float/safe_int helpers |
| `app.py:106-230` | fetch_all_data() |
| `app.py:370-530` | fetch_prices_quick() |
| `app.py:335-355` | calc_correlations() |
| `app.py:540-590` | get_data() cache logic |
| `app.py:630-730` | API handlers (etf_data, refresh, export) |
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
