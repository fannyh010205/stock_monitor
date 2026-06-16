# 📊 股票监控系统 — GitHub Actions 部署指南

## 文件结构

```
stock-monitor/                         ← GitHub 仓库根目录
├── .github/
│   └── workflows/
│       ├── realtime_check.yml         # 每5分钟检测异动
│       └── daily_report.yml           # 每天9点发汇总邮件
├── scripts/
│   └── check.py                       # 主程序
├── config/
│   └── config.yaml                    # ⭐ 监控标的 & 阈值配置
├── data/                              # 自动生成（缓存持仓/状态）
└── requirements.txt
```

---

## 部署步骤（10 分钟完成）

### 第一步：创建 GitHub 仓库

1. 登录 https://github.com
2. 右上角「+」→「New repository」
3. 名字随便取，如 `stock-monitor`
4. 选 **Private**（重要！避免配置泄露）
5. 点「Create repository」

### 第二步：上传文件

方法 A：直接拖拽（最简单）
- 在仓库页面点「uploading an existing file」
- 把所有文件夹整体拖进去，保持目录结构
- Commit changes

方法 B：命令行
```bash
cd stock-monitor   # 解压后的目录
git init
git remote add origin https://github.com/你的用户名/stock-monitor.git
git add .
git commit -m "初始化监控系统"
git push -u origin main
```

### 第三步：配置 Gmail App Password

1. 打开 https://myaccount.google.com/security
2. 确保已开启「两步验证」
3. 搜索「App passwords」→ 新建，名称填 StockMonitor
4. 复制生成的 16 位密码（格式：xxxx xxxx xxxx xxxx）

### 第四步：添加 GitHub Secrets（存储邮件密码）

在你的仓库页面：
**Settings → Secrets and variables → Actions → New repository secret**

依次添加以下 3 个：

| Secret 名称      | 填入内容                    |
|-----------------|---------------------------|
| `EMAIL_SENDER`  | 你的 Gmail 地址             |
| `EMAIL_PASSWORD`| 第三步生成的 App Password   |
| `EMAIL_RECEIVER`| 收件邮箱（可以是同一个地址）  |

### 第五步：启用 Actions

1. 仓库页面点「Actions」标签
2. 如果看到提示「Workflows aren't being run on this repository」，点「I understand my workflows, go ahead and enable them」
3. 左侧可以看到两个 workflow：
   - 📈 实时行情监控（每5分钟）
   - 📊 每日汇总报告（工作日9点）

### 第六步：手动测试

点「📊 每日汇总报告」→「Run workflow」→「Run workflow」
等待约 1 分钟，检查邮件是否收到。

---

## 日常使用

### 修改监控标的
直接在 GitHub 网页编辑 `config/config.yaml`，保存后自动生效。

### 添加新股票
在 `config.yaml` 的 `stocks` 列表中添加：
```yaml
- symbol: "NVDA"
  name: "NVIDIA"
  type: "stock"
  alert_pct: 4.0
  volume_multiplier: 2.5
  price_alerts:
    above: 150.0    # 可选：突破提醒
    below: null
```

### 设置 BTC 价格突破提醒
```yaml
- symbol: "bitcoin"
  ticker: "BTC"
  name: "Bitcoin"
  alert_pct: 5.0
  price_alerts:
    above: 100000   # BTC 突破 10 万时提醒
    below: 60000    # BTC 跌破 6 万时提醒
```

### 查看运行日志
仓库 → Actions → 点击任意一次运行记录 → 展开「运行行情检测」步骤

### 手动触发一次检测
Actions → 📈 实时行情监控 → Run workflow

---

## 常见问题

**Q: GitHub Actions 是真的免费吗？**
公开仓库无限免费；私有仓库每月 2000 分钟免费额度。
本系统每次运行约 1 分钟，每5分钟一次 = 每月约 8640 分钟。
**超出额度？** → 把仓库改成 Public（代码没有敏感信息，密码在 Secrets 里是安全的）

**Q: 每5分钟最新的吗？**
是的，但 GitHub 的 cron 调度有时会延迟 1–5 分钟，属于正常现象。

**Q: 如何暂停监控？**
Actions → 📈 实时行情监控 → 右上角「...」→「Disable workflow」

**Q: MNVT 持仓怎么维护？**
自动抓取暂不支持（需根据官网结构定制）。
手动方案：在仓库中创建 `data/mnvt_holdings.json`：
```json
[
  {"symbol": "AAPL", "weight": 8.5},
  {"symbol": "NVDA", "weight": 7.2},
  {"symbol": "MSFT", "weight": 6.8}
]
```
每日汇总邮件会显示这些持仓。
