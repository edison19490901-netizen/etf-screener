# ETF Screener Dashboard — 完整部署说明书

## 项目概述

ETF Screener 是一个实时 ETF 监控面板，跟踪 **25 只精选 ETF**，提供折溢价率、PE 分位、K 线图（含 Bollinger Bands）、费用对比、1Y 最高/最低价网格（斐波那契 0.25/0.382/0.5/0.618/0.75，6区）等指标。采用 **双模式数据刷新**架构，日常查看 ~3 秒，全量刷新 ~130 秒。

### 跟踪的 ETF（25只）

| 代码 | 名称 | 类型 |
|------|------|------|
| 516080 | 创新药ETF易方达 | 行业ETF |
| 159061 | 绿色电力ETF南方 | 行业ETF |
| 159611 | 电力ETF广发 | 行业ETF |
| 159692 | 证券ETF东财 | 行业ETF |
| 516120 | 化工ETF富国 | 行业ETF |
| 516770 | 游戏ETF华泰柏瑞 | 行业ETF |
| 515550 | 中证500ETF国联 | 宽基ETF |
| 159562 | 黄金股ETF华夏 | 商品ETF |
| 588080 | 科创50ETF易方达 | 宽基ETF |
| 159741 | 恒生科技ETF嘉实 | 跨境ETF |
| 159928 | 消费ETF汇添富 | 行业ETF |
| 510300 | 沪深300ETF华泰柏瑞 | 宽基ETF |
| 515880 | 通信ETF国泰 | 行业ETF |
| 588780 | 科创芯片设计ETF国联安 | 行业ETF |
| 159796 | 电池ETF汇添富 | 行业ETF |
| 510050 | 上证50ETF华夏 | 宽基ETF |
| 159367 | 创业板50ETF华夏 | 宽基ETF |
| 159150 | 深证50ETF易方达 | 宽基ETF |
| 159212 | 深100ETF南方 | 宽基ETF |
| 159227 | 航空航天ETF华夏 | 行业ETF |
| 159030 | 粮食ETF华夏 | 行业ETF |
| 159338 | 中证A500ETF国泰 | 宽基ETF |
| 518680 | 黄金ETF华夏 | 商品ETF |
| 511260 | 十年国债ETF国泰 | 债券ETF |
| 512890 | 红利低波ETF华泰柏瑞 | 策略ETF |

### 技术栈

| 层 | 技术 | 用途 |
|----|------|------|
| 后端 | Python 3 + `http.server` | HTTP 服务，无框架依赖 |
| 数据 | akshare + baostock | 实时行情 / 历史K线 / 费率 / PE分位 |
| 计算 | pandas + numpy | 数据处理 + Bollinger Bands |
| 前端 | 纯 HTML/CSS/JS | 自包含单页应用，Canvas 绘图 |
| 部署 | Render | 免费 Web Service |
| 推送 | PushPlus | 微信消息推送 |

---

## 项目文件结构

```
etf-screener/
├── app.py                  # 后端服务器（核心）
├── etf_dashboard.html      # 前端页面（自包含）
├── password.html           # 密码登录页（可选）
├── update.py               # 每日定时更新脚本
├── requirements.txt        # Python 依赖
├── render.yaml             # Render 部署配置
├── ETF_icon.png            # 网站图标
├── .gitignore              # Git 忽略规则
└── cache/                  # 缓存目录（自动创建）
    ├── etf_cache.json      # ETF 数据缓存
    └── etf_metadata.json   # 慢变化元数据缓存
```

---

## 一、本地开发部署

### 1.1 环境要求

- Python 3.9+
- Git
- 网络可访问 akshare、baostock、East Money（中国大陆网络即可）

### 1.2 克隆仓库

```bash
git clone git@github.com:edison19490901-netizen/etf-screener.git
cd etf-screener
```

### 1.3 安装依赖

```bash
pip install -r requirements.txt
```

### 1.4 启动服务器

```bash
python app.py
```

首次启动会自动从 akshare 拉取数据（约 130 秒），之后数据缓存在 `cache/` 目录。服务器监听 `http://localhost:8081`（本地默认端口，避开 stock-screener 的 8080）。

### 1.5 命令行参数

```bash
python app.py --port 9090      # 自定义端口（默认 8081）
python app.py --prefetch       # 启动时预加载数据（默认开启）
```

---

## 二、GitHub 仓库设置

### 2.1 创建仓库

1. 打开 https://github.com/new
2. Repository name: `etf-screener`
3. **不要勾选** "Add a README file"、"Add .gitignore"、"Choose a license"
4. 点击 "Create repository"

### 2.2 推送代码

```bash
cd etf-screener
git init
git checkout -b main
git add -A
git commit -m "Initial commit"
git remote add origin git@github.com:edison19490901-netizen/etf-screener.git
git push -u origin main
```

> **注意**：中国大陆用户建议使用 SSH 协议（`git@github.com`），HTTPS 直连可能被封锁。确认 SSH Key 已配置：
> ```bash
> ssh -T git@github.com    # 应返回 "Hi xxx! You've successfully authenticated"
> ```

### 2.3 后续更新

```bash
git add -A
git commit -m "描述改动内容"
git push origin main
```

Render 会自动检测 main 分支的更新并重新部署。

---

## 三、Render 部署

### 3.1 准备工作

- GitHub 仓库已创建并推送
- Render 账号（https://dashboard.render.com 注册，支持 GitHub 登录）

> **注意**：Render 控制台在中国大陆可能无法直接访问，需要 VPN。部署完成后，服务地址 `etf-screener.onrender.com` 在国内大概率可直接访问。

### 3.2 部署步骤

1. 登录 https://dashboard.render.com
2. 点击右上角 **New +** → **Web Service**
3. 点击 "Connect account" 授权 GitHub
4. 在仓库列表中找到 `edison19490901-netizen/etf-screener`，点击 **Connect**
5. 配置页面会自动读取 `render.yaml`，确认以下设置：

| 配置项 | 值 |
|--------|-----|
| Name | etf-screener（可自定义） |
| Region | Oregon (US West) |
| Runtime | Python 3 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `python app.py` |
| Instance Type | Free |
| Environment Variable → PORT | `8080` |

6. 点击 **Create Web Service**

### 3.3 部署时间线

| 阶段 | 耗时 | 说明 |
|------|------|------|
| Build（构建） | 2-5 分钟 | 安装 Python 依赖（akshare、baostock、pandas 等） |
| Deploy（部署） | 10-30 秒 | 启动 Python 服务 |
| 首次数据加载 | ~130 秒 | 服务器启动时自动拉取 14 只 ETF 的完整数据 |
| 可访问 | 总计 5-8 分钟 | 打开 `https://etf-screener.onrender.com` |

### 3.4 自定义域名（可选）

1. Render Dashboard → etf-screener → Settings → Custom Domains
2. 添加你的域名（如 `etf.yourdomain.com`）
3. 在 DNS 服务商添加 CNAME 记录指向 Render 提供的地址

---

## 四、环境变量

在 Render Dashboard → etf-screener → Environment 中配置：

| 变量名 | 必填 | 默认值 | 说明 |
|--------|------|--------|------|
| `PORT` | 是 | `8081` | 服务端口（本地默认；Render 部署时由平台自动赋值） |
| `PUSHPLUS_TOKEN` | 否 | 空 | PushPlus 微信推送 token，配置后 Full Refresh 完成自动推送 |
| `DASHBOARD_PASSWORD` | 否 | 空 | 看板访问密码。设置后访问 `http://localhost:8081/` 或 Render 网址先出密码页；留空则完全公开 |

### PushPlus Token 获取

1. 打开 http://www.pushplus.plus
2. 微信扫码登录
3. 复制你的 **token**
4. 添加到 Render 环境变量

### 看板密码（可选）

1. 在 `.env`（本地）或 Render 环境变量中添加 `DASHBOARD_PASSWORD=你的密码`
2. 重启服务后，访问 `/` 或 `/etf_dashboard.html` 会先出现密码页，输对密码才进看板
3. 密码存服务端（sha256 token + HttpOnly cookie，24h 有效），不会出现在前端代码里
4. 说明：`/api/*` 数据接口保持开放（保证本地 `file://` 直连可用）；本地用 `file://` 直接打开 HTML 会绕过密码页（仅本机自己用，无风险）

---

## 五、数据刷新模式

### 5.1 双模式对比

| | ⚡ Quick Refresh | 🔄 Full Refresh |
|---|---|---|
| **耗时** | ~3 秒 | ~130 秒 |
| **K线 (60天)** | ✅ baostock | ✅ baostock |
| **Bollinger Bands** | ✅ | ✅ |
| **1年最低价** | ✅ | ✅ |
| **最新价** | ✅ 最新收盘价 | ✅ 盘中实时价 |
| **折溢价率** | ❌ 缓存 | ✅ akshare 实时 |
| **规模(亿)** | ❌ 缓存 | ✅ akshare 实时 |
| **费率** | ❌ 缓存 | ✅ akshare 实时 |
| **PE分位(3Y)** | ❌ 缓存 | ✅ akshare 实时 |
| **涨跌幅/换手率** | ❌ 缓存 | ✅ akshare 实时 |
| **推送微信** | ❌ | ✅（需配置 PUSHPLUS_TOKEN） |

### 5.2 数据源说明

| 数据 | Quick 来源 | Full 来源 | 更新频率 |
|------|-----------|-----------|---------|
| 价格/K线/Bollinger | baostock（新浪源） | baostock | 每日收盘后更新 |
| 实时价/折溢价/规模 | 最近一次 Full 缓存 | akshare `fund_etf_spot_em()` | 盘中实时 |
| 费率 | 最近一次 Full 缓存 | akshare `fund_fee_em()` | 基本不变 |
| PE分位 | 最近一次 Full 缓存 | akshare `stock_index_pe_lg()` | 每日更新 |

### 5.3 使用建议

- **每日查看**：打开页面自动 Quick Refresh（~3秒看到K线和价格）
- **周末/盘前**：点击 Full Refresh 获取最新规模、费率、PE分位
- **盘中盯盘**：Quick Refresh 即可，折溢价率来自上次 Full 缓存

---

## 六、API 端点

| 端点 | 方法 | 参数 | 说明 |
|------|------|------|------|
| `/` | GET | — | 看板入口：设了 `DASHBOARD_PASSWORD` 则先出密码页 |
| `/login` | POST | `password` | 密码登录，成功后设 cookie 并跳看板 |
| `/api/etf_data` | GET | `?full=1` 全量；`?force=1` 跳过缓存 | 获取 ETF 数据 JSON |
| `/api/refresh` | GET | `?async=1&quick=1` | 触发数据刷新 |
| `/api/export` | GET | — | 下载 Excel 文件 |
| `/api/health` | GET | — | 健康检查 |

### 响应示例

```json
GET /api/etf_data
{
  "count": 14,
  "updated": "2026-08-10 14:30",
  "mode": "quick",
  "etfs": [
    {
      "code": "510300",
      "name": "沪深300ETF华泰柏瑞",
      "latest_price": 4.555,
      "discount_rate": -0.15,
      "change_pct": 1.23,
      "fund_size_yi": 1280.5,
      "total_fee": "0.60%",
      "pe_percentile": 45.2,
      "pe_current": 13.8,
      "min_price_1y": 4.405,
      "pct_from_low": 3.4,
      "bb_lower": 4.5549,
      "bb_upper": 4.8201,
      "bb_lower_history": [4.50, 4.51, ...],
      "bb_upper_history": [4.78, 4.79, ...],
      "price_history": [4.45, 4.47, ...],
      "_cached_at": "2026-08-10 14:30"
    }
  ]
}
```

---

## 七、每日自动更新（Cron）

### 7.1 Render Cron Job（推荐）

在 Render Dashboard 创建 Cron Job：

1. **New +** → **Cron Job**
2. 选择同一仓库 `etf-screener`
3. 配置：

| 配置项 | 值 |
|--------|-----|
| Name | etf-update-daily |
| Schedule | `0 16 * * *`（北京时间午夜，美股收盘后） |
| Command | `python update.py` |
| Runtime | Python 3 |

### 7.2 GitHub Actions（备选）

```yaml
# .github/workflows/daily-update.yml
name: Daily ETF Update
on:
  schedule:
    - cron: '0 16 * * *'  # UTC 16:00 = 北京时间 00:00
  workflow_dispatch:

jobs:
  update:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r etf-screener/requirements.txt
      - run: python etf-screener/update.py
```

---

## 八、故障排查

### 8.1 Render 部署失败

| 症状 | 原因 | 解决 |
|------|------|------|
| Build 失败 | 依赖安装错误 | 检查 `requirements.txt`，确认版本号 |
| 启动后立即崩溃 | NaN 数据或网络问题 | 查看 Render Logs，确认 akshare/baostock 可访问 |
| 503 Service Unavailable | Free 实例休眠 | 等待 30 秒自动唤醒 |

### 8.2 数据不完整

| 症状 | 原因 | 解决 |
|------|------|------|
| PE分位显示 "--" | 指数不在 akshare 支持列表 | 仅 510300(沪深300) 和 515550(中证500) 有 PE |
| 折溢价为 0 | Quick 模式用缓存 | 执行一次 Full Refresh |
| 某个 ETF 最新价为 0 | 数据源缺数据 | 检查 Render Logs 该 ETF 的 K-line 请求 |

### 8.3 本地调试

```bash
# 查看缓存文件
cat cache/etf_cache.json | python -m json.tool | head -50

# 测试 API
curl http://localhost:8081/api/etf_data | python -m json.tool | head -30
curl http://localhost:8081/api/health
curl -o test.xlsx http://localhost:8081/api/export

# 单独测试数据获取
python -c "
from app import fetch_prices_quick
data = fetch_prices_quick()
print(f'{len(data)} ETFs')
for e in data:
    print(f'{e[\"code\"]} {e[\"name\"]}: ¥{e[\"latest_price\"]}  BB={e.get(\"bb_lower\")}')
"
```

### 8.4 Render 日志查看

1. Render Dashboard → etf-screener → Logs
2. 筛选 "Error" 或 "exception"
3. 常见错误：
   - `ValueError: cannot convert float NaN` → 已修复（safe_float）
   - `Connection refused` → akshare API 暂时不可用，重试即可
   - `baostock login error` → 网络问题，通常自愈

---

## 九、维护清单

| 频率 | 操作 |
|------|------|
| 每日 | 检查 Render 是否 "Live" |
| 每周 | 执行一次 Full Refresh 更新缓存 |
| 每月 | 检查 akshare/baostock 版本更新 |
| 按需 | 添加/删除 ETF：修改 `app.py` 中的 `ETF_LIST` |

### 添加新 ETF

编辑 `app.py`：

```python
ETF_LIST = [
    # ... 现有 ETF ...
    '159xxx',  # 新ETF名称
]
```

如果需要 PE 分位，在 `ETF_INDEX_MAP` 中添加映射（需确认指数在 akshare 支持列表中）：

```python
ETF_INDEX_MAP = {
    '510300': '沪深300',
    '515550': '中证500',
    '159xxx': '上证50',  # 新增
}
```

提交并推送，Render 自动重新部署。

---

## 十、许可与致谢

- 数据来源：akshare、baostock、东方财富
- PE 分位数据：legulegu.com（通过 akshare）
- 推送服务：PushPlus
- 部署平台：Render
