#!/usr/bin/env python3
"""
股票监控脚本 — GitHub Actions 版
- 从环境变量读取邮件配置（GitHub Secrets）
- 从 config/config.yaml 读取监控标的和阈值
- 状态（冷却期）通过 GitHub Actions Cache 持久化
- 支持两种模式：异动检测（默认）/ 每日汇总（REPORT_MODE=true）
"""

import os, sys, json, logging, smtplib, requests, yaml, pytz, time
import yfinance as yf
from datetime import datetime
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header

# ── 路径 ──────────────────────────────────────────────────────
BASE   = Path(__file__).parent.parent
CFG_F  = BASE / "config" / "config.yaml"
STATE_F = BASE / "data" / "state.json"
HOLD_F  = BASE / "data" / "mnvt_holdings.json"

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)
ET  = pytz.timezone("America/New_York")

# ── 模式判断 ──────────────────────────────────────────────────
REPORT_MODE = os.environ.get("REPORT_MODE", "").lower() == "true"

# ═══════════════════════════════════════════════════════════════
# 配置 & 状态
# ═══════════════════════════════════════════════════════════════
def load_config():
    with open(CFG_F) as f:
        return yaml.safe_load(f)

def load_state():
    if STATE_F.exists():
        try:
            return json.loads(STATE_F.read_text())
        except Exception:
            pass
    return {"last_alerts": {}, "prev_prices": {}}

def save_state(state):
    STATE_F.parent.mkdir(parents=True, exist_ok=True)
    STATE_F.write_text(json.dumps(state, indent=2))

def cooled_down(state, key, minutes):
    last = state["last_alerts"].get(key)
    if not last:
        return True
    elapsed = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 60
    return elapsed >= minutes

def mark(state, key):
    state["last_alerts"][key] = datetime.now().isoformat()

# ═══════════════════════════════════════════════════════════════
# 数据获取
# ═══════════════════════════════════════════════════════════════
def fetch_stock(symbol):
    for attempt in range(3):
        try:
            t    = yf.Ticker(symbol)
            info = t.fast_info
            h20  = t.history(period="30d", interval="1d")
            price      = info.last_price
            prev_close = info.previous_close
            volume     = info.last_volume
            avg_vol    = h20["Volume"].mean() if len(h20) > 0 else None
            pct = ((price - prev_close) / prev_close * 100) if prev_close else 0
            return {
                "symbol":     symbol,
                "price":      round(float(price), 4) if price else None,
                "prev_close": round(float(prev_close), 4) if prev_close else None,
                "pct_change": round(pct, 2),
                "volume":     int(volume) if volume else None,
                "avg_volume": int(avg_vol) if avg_vol else None,
            }
        except Exception as e:
            log.warning(f"{symbol} 第{attempt+1}次获取失败: {e}")
            time.sleep(2)
    return None

def fetch_crypto(ids):
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={','.join(ids)}&vs_currencies=usd"
        "&include_24hr_change=true&include_24hr_vol=true"
    )
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"CoinGecko 请求失败: {e}")
        return {}

def fetch_mnvt_nav():
    """尝试从 yfinance 获取 MNVT NAV"""
    try:
        info = yf.Ticker("MNVT").info
        return info.get("navPrice") or info.get("nav")
    except Exception:
        return None

def load_holdings():
    if HOLD_F.exists():
        try:
            return json.loads(HOLD_F.read_text())
        except Exception:
            pass
    return []

# ═══════════════════════════════════════════════════════════════
# 异动检测
# ═══════════════════════════════════════════════════════════════
def check_stock(cfg_item, data, state, cooldown):
    alerts = []
    if not data or data["price"] is None:
        return alerts
    sym   = cfg_item["symbol"]
    price = data["price"]
    pct   = data["pct_change"]
    vol   = data["volume"]
    avg_v = data["avg_volume"]

    # 涨跌幅
    thr = cfg_item.get("alert_pct", 3.0)
    if abs(pct) >= thr:
        key = f"{sym}_pct"
        if cooled_down(state, key, cooldown):
            icon = "📈" if pct > 0 else "📉"
            alerts.append({"type": "涨跌幅异动", "symbol": sym,
                "name": cfg_item["name"],
                "detail": f"{icon} {pct:+.2f}%，当前 ${price:.2f}",
                "key": key, "urgency": "high"})

    # 价格突破
    pa    = cfg_item.get("price_alerts") or {}
    prev  = state["prev_prices"].get(sym)
    above = pa.get("above")
    below = pa.get("below")
    if above and prev and prev < above <= price:
        key = f"{sym}_above_{above}"
        if cooled_down(state, key, cooldown):
            alerts.append({"type": "突破上限", "symbol": sym,
                "name": cfg_item["name"],
                "detail": f"突破 ${above} 关键位，当前 ${price:.2f}",
                "key": key, "urgency": "high"})
    if below and prev and prev > below >= price:
        key = f"{sym}_below_{below}"
        if cooled_down(state, key, cooldown):
            alerts.append({"type": "跌破下限", "symbol": sym,
                "name": cfg_item["name"],
                "detail": f"跌破 ${below} 关键位，当前 ${price:.2f}",
                "key": key, "urgency": "high"})

    # 成交量放大
    vm = cfg_item.get("volume_multiplier", 2.0)
    if vol and avg_v and vol >= avg_v * vm:
        key = f"{sym}_vol"
        if cooled_down(state, key, cooldown):
            alerts.append({"type": "成交量异常", "symbol": sym,
                "name": cfg_item["name"],
                "detail": f"成交量 {vol:,}，是均量的 {vol/avg_v:.1f}x，价格 ${price:.2f}",
                "key": key, "urgency": "medium"})

    state["prev_prices"][sym] = price
    return alerts

def check_crypto(cfg_item, cg, state, cooldown):
    alerts = []
    cg_id  = cfg_item["symbol"]
    ticker = cfg_item["ticker"]
    if cg_id not in cg:
        return alerts
    d     = cg[cg_id]
    price = d.get("usd")
    pct   = d.get("usd_24h_change", 0)
    if not price:
        return alerts

    thr = cfg_item.get("alert_pct", 5.0)
    if abs(pct) >= thr:
        key = f"{ticker}_pct"
        if cooled_down(state, key, cooldown):
            icon = "🚀" if pct > 0 else "🔻"
            alerts.append({"type": "涨跌幅异动", "symbol": ticker,
                "name": cfg_item["name"],
                "detail": f"{icon} {pct:+.2f}%，当前 ${price:,.2f}",
                "key": key, "urgency": "high"})

    pa    = cfg_item.get("price_alerts") or {}
    prev  = state["prev_prices"].get(ticker)
    above = pa.get("above")
    below = pa.get("below")
    if above and prev and prev < above <= price:
        key = f"{ticker}_above_{above}"
        if cooled_down(state, key, cooldown):
            alerts.append({"type": "突破上限", "symbol": ticker,
                "name": cfg_item["name"],
                "detail": f"突破 ${above:,.0f}，当前 ${price:,.2f}",
                "key": key, "urgency": "high"})
    if below and prev and prev > below >= price:
        key = f"{ticker}_below_{below}"
        if cooled_down(state, key, cooldown):
            alerts.append({"type": "跌破下限", "symbol": ticker,
                "name": cfg_item["name"],
                "detail": f"跌破 ${below:,.0f}，当前 ${price:,.2f}",
                "key": key, "urgency": "high"})

    state["prev_prices"][ticker] = price
    return alerts

def check_mnvt_nav_spread(mnvt_data, nav, cfg, state, cooldown):
    alerts = []
    if not nav or not mnvt_data or not mnvt_data.get("price"):
        return alerts
    price  = mnvt_data["price"]
    spread = (price - nav) / nav * 100
    disc_t = cfg.get("discount_alert_pct", -1.5)
    prem_t = cfg.get("premium_alert_pct",  2.0)
    if spread <= disc_t:
        key = "mnvt_discount"
        if cooled_down(state, key, cooldown):
            alerts.append({"type": "🏷️ MNVT 折价套利", "symbol": "MNVT",
                "name": "Moonvest ETF",
                "detail": f"市价 ${price:.2f} vs NAV ${nav:.2f}，折价 {spread:.2f}%，关注买入机会",
                "key": key, "urgency": "high"})
    elif spread >= prem_t:
        key = "mnvt_premium"
        if cooled_down(state, key, cooldown):
            alerts.append({"type": "⚠️ MNVT 溢价风险", "symbol": "MNVT",
                "name": "Moonvest ETF",
                "detail": f"市价 ${price:.2f} vs NAV ${nav:.2f}，溢价 {spread:.2f}%，注意追高风险",
                "key": key, "urgency": "medium"})
    return alerts

# ═══════════════════════════════════════════════════════════════
# 邮件构建
# ═══════════════════════════════════════════════════════════════
STYLE = """
body{font-family:Arial,sans-serif;background:#f4f6f8;margin:0;padding:20px}
.card{max-width:680px;margin:0 auto;background:#fff;border-radius:10px;
      overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.08)}
.header{padding:20px 24px;color:#fff}
.header h2{margin:0;font-size:18px}
.header p{margin:4px 0 0;font-size:12px;opacity:.75}
table{width:100%;border-collapse:collapse;font-size:14px}
th{padding:9px 14px;text-align:left;color:#777;font-size:12px;
   background:#f9f9f9;border-bottom:1px solid #eee}
td{padding:9px 14px;border-bottom:1px solid #f2f2f2;color:#333}
.up{color:#2e7d32;font-weight:600}
.dn{color:#c62828;font-weight:600}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;
       font-size:11px;font-weight:600}
.high{background:#fff3f3;color:#c62828}
.medium{background:#fff8e1;color:#e65100}
.footer{padding:14px 24px;font-size:11px;color:#aaa;border-top:1px solid #eee}
.nav-bar{margin:16px 24px;padding:12px 18px;background:#e8f5e9;
         border-radius:6px;border-left:4px solid #43a047;font-size:14px}
"""

def pct_span(pct):
    cls = "up" if pct > 0 else ("dn" if pct < 0 else "")
    arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "─")
    return f'<span class="{cls}">{arrow} {abs(pct):.2f}%</span>'

def alert_email(alerts):
    rows = "".join(f"""
      <tr>
        <td><span class="badge {a['urgency']}">{a['type']}</span></td>
        <td><b>{a['symbol']}</b></td>
        <td>{a['detail']}</td>
      </tr>""" for a in alerts)
    now = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    syms = "、".join(dict.fromkeys(a["symbol"] for a in alerts))
    return (
        f"⚡ 市场异动：{syms}",
        f"""<html><head><style>{STYLE}</style></head><body>
        <div class="card">
          <div class="header" style="background:#b71c1c">
            <h2>⚡ 市场异动提醒</h2><p>{now}</p></div>
          <table><thead><tr>
            <th>类型</th><th>标的</th><th>详情</th>
          </tr></thead><tbody>{rows}</tbody></table>
          <div class="footer">此邮件由自动监控系统发送，不构成投资建议。</div>
        </div></body></html>"""
    )

def daily_email(stock_rows, crypto_rows, nav_info, holdings):
    now  = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    today = datetime.now(ET).strftime("%Y-%m-%d")

    def vol_badge(vol, avg):
        if not vol or not avg: return ""
        r = vol / avg
        if r >= 3: return f' <span class="badge high">🔥 {r:.1f}x 均量</span>'
        if r >= 2: return f' <span class="badge medium">⚡ {r:.1f}x 均量</span>'
        return ""

    s_rows = "".join(f"""<tr>
      <td><b>{r['symbol']}</b></td>
      <td style="color:#555">{r['name']}</td>
      <td>${r['price']:.2f}</td>
      <td>{pct_span(r['pct_change'])}</td>
      <td>{vol_badge(r.get('volume'), r.get('avg_volume'))}</td>
    </tr>""" for r in stock_rows)

    c_rows = "".join(f"""<tr>
      <td><b>{r['ticker']}</b></td>
      <td style="color:#555">{r['name']}</td>
      <td>${r['price']:,.2f}</td>
      <td>{pct_span(r['pct_change'])}</td>
      <td></td>
    </tr>""" for r in crypto_rows)

    nav_section = ""
    if nav_info:
        sp = nav_info["spread"]
        cls = "up" if sp > 0 else "dn"
        hint = " 💡 <b>存在折价，关注买入机会</b>" if sp < -1.0 else (
               " ⚠️ <b>溢价偏高，注意追高风险</b>" if sp > 2.0 else "")
        nav_section = f"""<div class="nav-bar">
          <b>MNVT 净值监控</b>　
          市价 <b>${nav_info['price']:.2f}</b> &nbsp;|&nbsp;
          NAV <b>${nav_info['nav']:.2f}</b> &nbsp;|&nbsp;
          折溢价 <b class="{cls}">{sp:+.2f}%</b>{hint}
        </div>"""

    hold_section = ""
    if holdings:
        h_rows = "".join(f"""<tr>
          <td><b>{h['symbol']}</b></td>
          <td style="color:#777">{h.get('weight','-')}%</td>
        </tr>""" for h in holdings[:15])
        hold_section = f"""<div style="padding:0 24px 16px">
          <p style="font-size:12px;color:#888;margin:16px 0 8px">MNVT 持仓参考</p>
          <table style="max-width:300px">
            <thead><tr><th>标的</th><th>权重</th></tr></thead>
            <tbody>{h_rows}</tbody>
          </table></div>"""

    return (
        f"📊 每日市场汇总 {today}",
        f"""<html><head><style>{STYLE}</style></head><body>
        <div class="card">
          <div class="header" style="background:#0d47a1">
            <h2>📊 每日市场监控汇总</h2><p>{now}</p></div>
          {nav_section}
          <div style="padding:0 24px 4px">
            <p style="font-size:12px;color:#888;margin:16px 0 8px">美股 / ETF / 大盘</p>
            <table><thead><tr>
              <th>代码</th><th>名称</th><th>价格</th><th>涨跌</th><th>成交量</th>
            </tr></thead><tbody>{s_rows}</tbody></table>
          </div>
          <div style="padding:0 24px 4px">
            <p style="font-size:12px;color:#888;margin:16px 0 8px">加密货币</p>
            <table><thead><tr>
              <th>代码</th><th>名称</th><th>价格</th><th>24h 涨跌</th><th></th>
            </tr></thead><tbody>{c_rows}</tbody></table>
          </div>
          {hold_section}
          <div class="footer">数据来源：Yahoo Finance / CoinGecko。不构成投资建议。</div>
        </div></body></html>"""
    )

# ═══════════════════════════════════════════════════════════════
# 发送邮件
# ═══════════════════════════════════════════════════════════════
def send_email(subject, html):
    sender   = os.environ["EMAIL_SENDER"]
    password = os.environ["EMAIL_PASSWORD"]
    receiver = os.environ["EMAIL_RECEIVER"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8").encode()
    msg["From"]    = sender
    msg["To"]      = receiver
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls()
            s.login(sender, password)
            s.sendmail(sender, receiver, msg.as_string())
        log.info(f"✅ 邮件已发送：{subject}")
    except Exception as e:
        log.error(f"❌ 邮件发送失败: {e}")
        sys.exit(1)

# ═══════════════════════════════════════════════════════════════
# 主逻辑
# ═══════════════════════════════════════════════════════════════
def main():
    cfg      = load_config()
    state    = load_state()
    cooldown = cfg["cooldown"]["same_alert_cooldown_minutes"]

    # ── 每日汇总模式 ──────────────────────────────────────────
    if REPORT_MODE:
        log.info("=== 每日汇总模式 ===")
        stock_rows = []
        for item in cfg["watchlist"]["stocks"]:
            d = fetch_stock(item["symbol"])
            if d and d["price"]:
                stock_rows.append({**d, "name": item["name"]})
                log.info(f"  {item['symbol']}: ${d['price']} ({d['pct_change']:+.2f}%)")

        cg_ids      = [c["symbol"] for c in cfg["watchlist"]["crypto"]]
        cg          = fetch_crypto(cg_ids)
        crypto_rows = []
        for item in cfg["watchlist"]["crypto"]:
            d = cg.get(item["symbol"], {})
            if d.get("usd"):
                crypto_rows.append({
                    "ticker": item["ticker"], "name": item["name"],
                    "price": d["usd"], "pct_change": d.get("usd_24h_change", 0),
                })

        nav      = fetch_mnvt_nav()
        mnvt_d   = fetch_stock("MNVT")
        nav_info = None
        if nav and mnvt_d and mnvt_d.get("price"):
            spread   = (mnvt_d["price"] - nav) / nav * 100
            nav_info = {"price": mnvt_d["price"], "nav": nav, "spread": round(spread, 2)}

        holdings = load_holdings()
        subj, html = daily_email(stock_rows, crypto_rows, nav_info, holdings)
        send_email(subj, html)
        return

    # ── 实时异动检测模式 ──────────────────────────────────────
    log.info("=== 异动检测模式 ===")
    all_alerts = []

    for item in cfg["watchlist"]["stocks"]:
        d = fetch_stock(item["symbol"])
        if d:
            log.info(f"  {item['symbol']}: ${d['price']} ({d['pct_change']:+.2f}%)")
            all_alerts.extend(check_stock(item, d, state, cooldown))

    # MNVT 折溢价检测
    mnvt_cfg = cfg.get("mnvt_nav", {})
    if mnvt_cfg.get("enabled"):
        nav    = fetch_mnvt_nav()
        mnvt_d = fetch_stock("MNVT")
        all_alerts.extend(check_mnvt_nav_spread(mnvt_d, nav, mnvt_cfg, state, cooldown))

    cg_ids = [c["symbol"] for c in cfg["watchlist"]["crypto"]]
    cg     = fetch_crypto(cg_ids)
    for item in cfg["watchlist"]["crypto"]:
        d = cg.get(item["symbol"], {})
        if d.get("usd"):
            log.info(f"  {item['ticker']}: ${d['usd']:,.2f} ({d.get('usd_24h_change',0):+.2f}%)")
        all_alerts.extend(check_crypto(item, cg, state, cooldown))

    if all_alerts:
        for a in all_alerts:
            mark(state, a["key"])
        subj, html = alert_email(all_alerts)
        send_email(subj, html)
        log.info(f"共触发 {len(all_alerts)} 条异动提醒")
    else:
        log.info("无异动，本次检测结束")

    save_state(state)


if __name__ == "__main__":
    main()
