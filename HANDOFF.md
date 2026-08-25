---
name: etf-screener-handoff
description: "ETF Screener complete project handoff — architecture, deployment, hedging strategy, portfolio config"
metadata: 
  node_type: memory
  type: project
  originSessionId: d049ee4a-be38-421b-96ea-6047203cb3fc
  modified: 2026-08-17T00:00:00.000Z
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

## 24 Tracked ETFs

### Attack basket (21)
516080, 159061, 159611, 159692, 516120, 516770, 515550, 159562, 588080, 159741, 159928, 510300, 515880, 588780, 159796(电池ETF汇添富), 510050(上证50), 159367(创业板50), 159150(深证50), 159212(深100), 159227(航空航天), 159030(粮食ETF华夏)

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

## 行情解读 Analysis

dashboard 顶部工具栏下方的「📊 行情解读」面板，随数据刷新自动更新；可手动「刷新解读」「推送微信」。

- **数据来源**：读现有缓存（`get_data()`），不动双模式刷新逻辑。
- **整体概览**：跟踪数（攻击/对冲/持仓）、网格区分布 + 低估区(1-2区)占比、距1年低点均值、今日均涨、近5/20日均动量、站上MA20/MA60比例、对冲仓平均相关性 → 整体方向（整体偏强/震荡分化/整体偏弱）+ 自然语言总结。**不含**折溢价/PE 概览。
- **重点分组（5组）**：
  - `buy` 接近买入点（网格1-2区，附区内下沿/中部/上沿，top5）
  - `hedge` 对冲仓状态（黄金/国债/红利低波，现价区间 + corr_300）
  - `holding` 持仓ETF状态（取 `HOLDINGS` 常量）
  - `mover` 今日异动（涨跌幅 top/bottom 各3）
  - `warning` 风险警示（6区 / 场内溢价>0.3% / PE分位>80%）
- **HOLDINGS**（`app.py:800`）：默认 `['159928','510300','159741','588780','515880']`，对应下方持仓清单中的 ETF；改持仓时同步更新此常量。
- **推送**：`POST /api/pushplus_analysis` 复用 `send_pushplus` 推 markdown 报告（标题「ETF 行情解读 \<日期\>」）。
- **前端**：`etf_dashboard.html` 的 `.analysis-panel` + `AN` JS 对象（load/render/renderFallback/push），接口不可用时降级为基础统计。
- **交互**：面板头部有「收起/展开」按钮（`AN.toggle()`），折叠状态存 localStorage `an_collapsed`，刷新/重开保持；`AN.init()` 启动时恢复。标题 `.an-title` 用 `flex:1 1 auto;white-space:nowrap`，防止手机上「行情解读」四字被压成竖排（flex:1 的 basis:0 会把弹性项压到 0 宽）。

## 密码保护 Auth

- **后端密码页**（镜像 stock-screener）：设了 `DASHBOARD_PASSWORD` 后，访问 `/` 或 `/etf_dashboard.html` 先出 `password.html` 登录页，正确密码 → `Set-Cookie: etf_auth=<sha256>`（HttpOnly，24h）→ 跳看板。
- **路由**：`/login`（POST，表单 `password`）→ `_handle_login`；错误密码 302 回 `/?err=1`。`/api/*` 接口**不鉴权**（与 stock-screener 一致，保证本地 file:// 直连 API 不受影响）。
- **本地 file:// 打开**：绕过服务器、不弹密码（仅本机自己用，无风险）。要在浏览器测密码页用 `http://localhost:8081/`。
- **未设密码**（环境变量为空）时完全公开，行为与之前一致。

## Key Code Locations

| File | What |
|------|------|
| `app.py:49-76` | ETF_LIST + BENCHMARK_ETF |
| `app.py:79-84` | ETF_NAMES dict |
| `app.py:99-112` | safe_float/safe_int helpers |
| `app.py:114-160` | calc_bollinger() (20-day) + calc_grid() (5线→6区) |
| `app.py:163-391` | fetch_all_data() (Full refresh) |
| `app.py:393-464` | calc_correlations() (60-day vs 510300) |
| `app.py:466-561` | fetch_nav_series() + nav_to_adj_closes() + compute_from_closes() — 前复权 K线/1Y/网格/BB |
| `app.py:565-650` | fetch_prices_quick() (Quick refresh) |
| `app.py:694-741` | get_data() cache logic |
| `app.py:746-794` | send_pushplus() + build_push_html() |
| `app.py:800-1155` | 行情解读 Analysis：HOLDINGS/HEDGE_ETFS + compute_trend_stats() + build_analysis() + build_report_markdown() |
| `app.py:1157-1183` | 密码鉴权 Auth：DASHBOARD_PASSWORD/AUTH_COOKIE_NAME + _make_token/_parse_cookies/_check_auth |
| `app.py:1186-1456` | HTTP API handlers（/api/etf_data /api/refresh /api/export /api/analysis /api/health /api/pushplus_analysis + /login 密码登录） |
| `etf_dashboard.html` | Full frontend（含 .analysis-panel + AN 行情解读面板） |
| `password.html` | 密码登录页（POST /login） |

## Environment Variables

| Var | Required | Default | Where |
|-----|----------|---------|-------|
| PORT | Yes | 8081 (local) | 本地默认 8081（避开 stock-screener 的 8080）；Render 部署时自动赋值 |
| PUSHPLUS_TOKEN | No | — | PushPlus WeChat token |
| DASHBOARD_PASSWORD | No | — | 看板访问密码（设了则先出 password.html；本地 file:// 打开绕过） |

## Troubleshooting

- **ETF 面板显示股票/解读内容不对**: 本地端口撞车。stock-screener 默认占 8080，etf-screener 本地默认已改 **8081**；dashboard `_detectApi()` 会自动迁移旧的 `localhost:8080` 缓存。仍异常时手动清浏览器 localStorage 的 `etf_api`。
- **GitHub push fails (HTTPS)**: GFW blocks 443. Use SSH (`git@github.com:...`)
- **NaN ValueError on Render**: Fixed with safe_float/safe_int in `app.py:77-96`
- **PE shows "--"**: Only 510300+515550 have PE data (12-index limit in akshare)
- **New ETF has no name**: Add to `ETF_NAMES` dict in `app.py:68-72`

**Why:** Full project context for continuing work or handoff to another developer.
**How to apply:** Read this file before making changes to ETF screener. Update it when portfolio or strategy changes.
