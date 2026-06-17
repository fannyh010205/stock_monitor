#!/usr/bin/env python3
import os, sys, json, logging, smtplib, requests, yaml, pytz, time
import yfinance as yf
import anthropic
from datetime import datetime
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

BASE    = Path(__file__).parent.parent
CFG_F   = BASE / "config" / "config.yaml"
STATE_F = BASE / "data" / "state.json"
HOLD_F  = BASE / "data" / "mnvt_holdings.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)
ET  = pytz.timezone("America/New_York")
REPORT_MODE = os.environ.get("REPORT_MODE", "").lower() == "true"

# ------ config & state ------------------------------------------------------------------------------------------------------------------------------------
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

# ------ data fetch ------------------------------------------------------------------------------------------------------------------------------------------------
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

            # 52-week high/low
            if REPORT_MODE:
                h52   = t.history(period="1y", interval="1d")
                high52 = float(h52["High"].max()) if len(h52) > 0 else None
                low52  = float(h52["Low"].min())  if len(h52) > 0 else None
            else:
                high52 = None
                low52  = None

            return {
                "symbol":     symbol,
                "price":      round(float(price), 4) if price else None,
                "prev_close": round(float(prev_close), 4) if prev_close else None,
                "pct_change": round(pct, 2),
                "volume":     int(volume) if volume else None,
                "avg_volume": int(avg_vol) if avg_vol else None,
                "high52":     round(high52, 2) if high52 else None,
                "low52":      round(low52, 2)  if low52  else None,
            }
        except Exception as e:
            log.warning(f"{symbol} fetch attempt {attempt+1} failed: {e}")
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
        log.error(f"CoinGecko request failed: {e}")
        return {}

def fetch_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        d = r.json()["data"][0]
        return {"value": d["value"], "label": d["value_classification"]}
    except Exception:
        return None

def fetch_mnvt_nav():
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

# ------ news fetch ------------------------------------------------------------------------------------------------------------------------------------------------
def fetch_news(symbol, max_items=5):
    """Fetch latest news headlines for a symbol via Yahoo Finance."""
    try:
        ticker = yf.Ticker(symbol)
        news   = ticker.news or []
        results = []
        for item in news[:max_items]:
            title = item.get("title", "")
            link  = item.get("link", "")
            publisher = item.get("publisher", "")
            pub_time  = item.get("providerPublishTime", 0)
            time_str  = datetime.fromtimestamp(pub_time, tz=ET).strftime("%m/%d %H:%M") if pub_time else ""
            if title:
                results.append({
                    "title":     title,
                    "link":      link,
                    "publisher": publisher,
                    "time":      time_str,
                })
        return results
    except Exception as e:
        log.warning(f"News fetch failed for {symbol}: {e}")
        return []

# ------ AI analysis ---------------------------------------------------------------------------------------------------------------------------------------------
def ai_analyze(alerts, market_context):
    """
    Call Claude to analyze the alerts and provide:
    - Likely cause of the move
    - Market context
    - Actionable suggestion
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {}

    client = anthropic.Anthropic(api_key=api_key)
    analyses = {}

    for alert in alerts:
        symbol = alert["symbol"]
        news   = alert.get("news", [])
        news_text = "\n".join(
            f"- [{n['time']} {n['publisher']}] {n['title']}" for n in news
        ) if news else "No recent news found."

        prompt = f"""You are a professional stock market analyst. Analyze this market alert and provide a concise, actionable assessment.

ALERT:
- Symbol: {symbol} ({alert['name']})
- Alert Type: {alert['type']}
- Detail: {alert['detail']}
- Time: {datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}

MARKET CONTEXT:
{market_context}

RECENT NEWS FOR {symbol}:
{news_text}

Please provide a JSON response with exactly these fields:
{{
  "likely_cause": "1-2 sentences on the most likely reason for this move",
  "market_context": "1 sentence on how broader market conditions relate",
  "risk_level": "LOW / MEDIUM / HIGH",
  "suggestion": "1-2 sentences of actionable guidance (watch, buy, sell, hold, avoid chasing)",
  "confidence": "LOW / MEDIUM / HIGH - your confidence in this analysis"
}}

Respond with JSON only, no other text."""

        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}]
            )
            text = response.content[0].text.strip()
            # Strip markdown fences if present
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            analysis = json.loads(text.strip())
            analyses[symbol] = analysis
            log.info(f"AI analysis done for {symbol}")
        except Exception as e:
            log.warning(f"AI analysis failed for {symbol}: {e}")
            analyses[symbol] = {
                "likely_cause":   "Analysis unavailable.",
                "market_context": "",
                "risk_level":     "MEDIUM",
                "suggestion":     "Review news and price action manually.",
                "confidence":     "LOW",
            }
        time.sleep(1)

    return analyses

# ------ alert detection ---------------------------------------------------------------------------------------------------------------------------------
def check_stock(cfg_item, data, state, cooldown):
    alerts = []
    if not data or data["price"] is None:
        return alerts
    sym   = cfg_item["symbol"]
    price = data["price"]
    pct   = data["pct_change"]
    vol   = data["volume"]
    avg_v = data["avg_volume"]

    thr = cfg_item.get("alert_pct", 3.0)
    if abs(pct) >= thr:
        key = f"{sym}_pct"
        if cooled_down(state, key, cooldown):
            direction = "UP" if pct > 0 else "DOWN"
            alerts.append({"type": f"Price Move {direction}", "symbol": sym,
                "name": cfg_item["name"],
                "detail": f"{pct:+.2f}%, now ${price:.2f}",
                "key": key, "urgency": "high", "data": data})

    pa    = cfg_item.get("price_alerts") or {}
    prev  = state["prev_prices"].get(sym)
    above = pa.get("above")
    below = pa.get("below")
    if above and prev and prev < above <= price:
        key = f"{sym}_above_{above}"
        if cooled_down(state, key, cooldown):
            alerts.append({"type": "Broke Above Key Level", "symbol": sym,
                "name": cfg_item["name"],
                "detail": f"Broke above ${above}, now ${price:.2f}",
                "key": key, "urgency": "high", "data": data})
    if below and prev and prev > below >= price:
        key = f"{sym}_below_{below}"
        if cooled_down(state, key, cooldown):
            alerts.append({"type": "Broke Below Key Level", "symbol": sym,
                "name": cfg_item["name"],
                "detail": f"Broke below ${below}, now ${price:.2f}",
                "key": key, "urgency": "high", "data": data})

    vm = cfg_item.get("volume_multiplier", 2.0)
    if vol and avg_v and vol >= avg_v * vm:
        key = f"{sym}_vol"
        if cooled_down(state, key, cooldown):
            alerts.append({"type": "Volume Spike", "symbol": sym,
                "name": cfg_item["name"],
                "detail": f"Volume {vol:,} = {vol/avg_v:.1f}x avg, price ${price:.2f}",
                "key": key, "urgency": "medium", "data": data})

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
            direction = "UP" if pct > 0 else "DOWN"
            alerts.append({"type": f"Crypto Move {direction}", "symbol": ticker,
                "name": cfg_item["name"],
                "detail": f"{pct:+.2f}%, now ${price:,.2f}",
                "key": key, "urgency": "high",
                "data": {"price": price, "pct_change": pct}})

    pa    = cfg_item.get("price_alerts") or {}
    prev  = state["prev_prices"].get(ticker)
    above = pa.get("above")
    below = pa.get("below")
    if above and prev and prev < above <= price:
        key = f"{ticker}_above_{above}"
        if cooled_down(state, key, cooldown):
            alerts.append({"type": "Crypto Broke Above", "symbol": ticker,
                "name": cfg_item["name"],
                "detail": f"Broke above ${above:,.0f}, now ${price:,.2f}",
                "key": key, "urgency": "high",
                "data": {"price": price, "pct_change": pct}})
    if below and prev and prev > below >= price:
        key = f"{ticker}_below_{below}"
        if cooled_down(state, key, cooldown):
            alerts.append({"type": "Crypto Broke Below", "symbol": ticker,
                "name": cfg_item["name"],
                "detail": f"Broke below ${below:,.0f}, now ${price:,.2f}",
                "key": key, "urgency": "high",
                "data": {"price": price, "pct_change": pct}})

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
            alerts.append({"type": "MNVT Discount", "symbol": "MNVT",
                "name": "Moonvest ETF",
                "detail": f"Price ${price:.2f} vs NAV ${nav:.2f}, discount {spread:.2f}% - potential buy",
                "key": key, "urgency": "high",
                "data": {"price": price, "pct_change": spread}})
    elif spread >= prem_t:
        key = "mnvt_premium"
        if cooled_down(state, key, cooldown):
            alerts.append({"type": "MNVT Premium", "symbol": "MNVT",
                "name": "Moonvest ETF",
                "detail": f"Price ${price:.2f} vs NAV ${nav:.2f}, premium {spread:.2f}% - watch risk",
                "key": key, "urgency": "medium",
                "data": {"price": price, "pct_change": spread}})
    return alerts

# ------ email builder ---------------------------------------------------------------------------------------------------------------------------------------
STYLE = """
body{font-family:Arial,sans-serif;background:#f4f6f8;margin:0;padding:20px}
.card{max-width:700px;margin:0 auto;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.08)}
.header{padding:20px 24px;color:#fff}
.header h2{margin:0;font-size:18px}
.header p{margin:4px 0 0;font-size:12px;opacity:.75}
.section{padding:16px 24px}
.section-title{font-size:12px;color:#888;font-weight:600;margin:0 0 10px;text-transform:uppercase;letter-spacing:.5px}
table{width:100%;border-collapse:collapse;font-size:14px}
th{padding:9px 14px;text-align:left;color:#777;font-size:12px;background:#f9f9f9;border-bottom:1px solid #eee}
td{padding:9px 14px;border-bottom:1px solid #f2f2f2;color:#333;vertical-align:top}
.up{color:#2e7d32;font-weight:600}
.dn{color:#c62828;font-weight:600}
.badge{display:inline-block;padding:3px 10px;border-radius:4px;font-size:11px;font-weight:600}
.high{background:#fff3f3;color:#c62828}
.medium{background:#fff8e1;color:#e65100}
.low{background:#e8f5e9;color:#2e7d32}
.ai-box{background:#f8f9ff;border-left:3px solid #3f51b5;border-radius:0 6px 6px 0;padding:12px 16px;margin:8px 0}
.ai-box .cause{font-size:14px;color:#1a237e;font-weight:600;margin-bottom:6px}
.ai-box .detail{font-size:13px;color:#444;margin-bottom:4px}
.ai-box .suggest{font-size:13px;color:#1b5e20;font-weight:600;margin-top:8px;padding-top:8px;border-top:1px solid #e0e0e0}
.risk-HIGH{color:#c62828;font-weight:600}
.risk-MEDIUM{color:#e65100;font-weight:600}
.risk-LOW{color:#2e7d32;font-weight:600}
.news-item{font-size:12px;color:#555;padding:3px 0;border-bottom:1px solid #f0f0f0}
.news-item a{color:#1565c0;text-decoration:none}
.news-meta{font-size:11px;color:#999}
.fg-bar{display:inline-block;padding:4px 12px;border-radius:12px;font-size:12px;font-weight:600}
.footer{padding:14px 24px;font-size:11px;color:#aaa;border-top:1px solid #eee}
.nav-bar{margin:0 24px 16px;padding:12px 18px;background:#e8f5e9;border-radius:6px;border-left:4px solid #43a047;font-size:14px}
"""

def pct_span(pct):
    cls   = "up" if pct > 0 else ("dn" if pct < 0 else "")
    arrow = "&#9650;" if pct > 0 else ("&#9660;" if pct < 0 else "&#8212;")
    return f'<span class="{cls}">{arrow} {abs(pct):.2f}%</span>'

def risk_badge(level):
    color = {"HIGH": "#c62828", "MEDIUM": "#e65100", "LOW": "#2e7d32"}.get(level, "#555")
    return f'<span style="color:{color};font-weight:600">&#9679; {level} RISK</span>'

def build_alert_block(alert, analysis, news):
    sym  = alert["symbol"]
    data = alert.get("data", {})
    price = data.get("price", 0)
    pct   = data.get("pct_change", 0)

    # 52-week context
    h52  = data.get("high52")
    l52  = data.get("low52")
    range_text = ""
    if h52 and l52 and price:
        pos = (price - l52) / (h52 - l52) * 100 if h52 != l52 else 50
        range_text = f'<div style="font-size:12px;color:#777;margin-top:4px">52w range: ${l52:.2f} - ${h52:.2f} &nbsp; (currently at {pos:.0f}% of range)</div>'

    # AI analysis block
    ai_html = ""
    if analysis:
        conf = analysis.get("confidence", "")
        ai_html = f"""
        <div class="ai-box">
          <div class="cause">AI Analysis {risk_badge(analysis.get('risk_level','MEDIUM'))}</div>
          <div class="detail"><b>Likely cause:</b> {analysis.get('likely_cause','')}</div>
          <div class="detail"><b>Market context:</b> {analysis.get('market_context','')}</div>
          <div class="suggest">Suggestion: {analysis.get('suggestion','')}</div>
          <div style="font-size:11px;color:#999;margin-top:6px">Analysis confidence: {conf}</div>
        </div>"""

    # News block
    news_html = ""
    if news:
        items = "".join(f"""
          <div class="news-item">
            <a href="{n['link']}" target="_blank">{n['title']}</a>
            <span class="news-meta"> &mdash; {n['publisher']} {n['time']}</span>
          </div>""" for n in news)
        news_html = f"""
        <div style="margin-top:10px">
          <div style="font-size:11px;color:#888;font-weight:600;margin-bottom:4px">RELATED NEWS</div>
          {items}
        </div>"""

    urgency_color = "#b71c1c" if alert["urgency"] == "high" else "#e65100"

    return f"""
    <div style="border:1px solid #eee;border-radius:8px;margin-bottom:16px;overflow:hidden">
      <div style="background:{urgency_color};color:#fff;padding:10px 16px;display:flex;justify-content:space-between;align-items:center">
        <div>
          <b style="font-size:16px">{sym}</b>
          <span style="font-size:13px;opacity:.85;margin-left:8px">{alert['name']}</span>
        </div>
        <div style="text-align:right">
          <span style="font-size:15px;font-weight:600">${price:,.2f}</span>
          <span style="font-size:13px;margin-left:8px">{'+' if pct>0 else ''}{pct:.2f}%</span>
        </div>
      </div>
      <div style="padding:12px 16px">
        <span class="badge {'high' if alert['urgency']=='high' else 'medium'}">{alert['type']}</span>
        <span style="margin-left:10px;font-size:13px;color:#444">{alert['detail']}</span>
        {range_text}
        {ai_html}
        {news_html}
      </div>
    </div>"""

def alert_email(alerts, analyses, fg):
    now  = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    syms = ", ".join(dict.fromkeys(a["symbol"] for a in alerts))

    fg_html = ""
    if fg:
        val = int(fg["value"])
        if val >= 75:
            fg_color = "#c62828"; fg_label = "Extreme Greed"
        elif val >= 55:
            fg_color = "#e65100"; fg_label = "Greed"
        elif val >= 45:
            fg_color = "#555";    fg_label = "Neutral"
        elif val >= 25:
            fg_color = "#1565c0"; fg_label = "Fear"
        else:
            fg_color = "#4a148c"; fg_label = "Extreme Fear"
        fg_html = f"""
        <div style="padding:10px 24px;background:#f8f8f8;border-bottom:1px solid #eee;font-size:13px">
          Market Sentiment: <span class="fg-bar" style="background:{fg_color}20;color:{fg_color}">{val} - {fg_label}</span>
        </div>"""

    blocks = "".join(
        build_alert_block(a, analyses.get(a["symbol"]), a.get("news", []))
        for a in alerts
    )

    subj = f"[Alert] {syms} - Market Move Detected"
    html = f"""<html><head><style>{STYLE}</style></head><body>
      <div class="card">
        <div class="header" style="background:#b71c1c">
          <h2>Market Alert</h2><p>{now}</p></div>
        {fg_html}
        <div class="section">{blocks}</div>
        <div class="footer">Data: Yahoo Finance / CoinGecko / Alternative.me. AI analysis by Claude. Not investment advice.</div>
      </div></body></html>"""
    return subj, html

def pct_span_plain(pct):
    arrow = "&#9650;" if pct > 0 else ("&#9660;" if pct < 0 else "&#8212;")
    cls   = "up" if pct > 0 else ("dn" if pct < 0 else "")
    return f'<span class="{cls}">{arrow} {abs(pct):.2f}%</span>'

def daily_email(stock_rows, crypto_rows, nav_info, holdings, fg):
    now   = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    today = datetime.now(ET).strftime("%Y-%m-%d")

    def vol_badge(vol, avg):
        if not vol or not avg: return ""
        r = vol / avg
        if r >= 3: return f'<span class="badge high">HOT {r:.1f}x vol</span>'
        if r >= 2: return f'<span class="badge medium">{r:.1f}x vol</span>'
        return ""

    def range_pct(price, h52, l52):
        if not price or not h52 or not l52 or h52 == l52: return ""
        pos = (price - l52) / (h52 - l52) * 100
        return f'<div style="font-size:11px;color:#999">${l52:.0f} - ${h52:.0f} ({pos:.0f}%)</div>'

    s_rows = "".join(f"""<tr>
      <td><b>{r['symbol']}</b></td>
      <td style="color:#555">{r['name']}</td>
      <td>${r['price']:.2f}{range_pct(r['price'], r.get('high52'), r.get('low52'))}</td>
      <td>{pct_span_plain(r['pct_change'])}</td>
      <td>{vol_badge(r.get('volume'), r.get('avg_volume'))}</td>
    </tr>""" for r in stock_rows)

    c_rows = "".join(f"""<tr>
      <td><b>{r['ticker']}</b></td>
      <td style="color:#555">{r['name']}</td>
      <td>${r['price']:,.2f}</td>
      <td>{pct_span_plain(r['pct_change'])}</td>
      <td></td>
    </tr>""" for r in crypto_rows)

    nav_section = ""
    if nav_info:
        sp  = nav_info["spread"]
        cls = "up" if sp > 0 else "dn"
        hint = " <b>Discount - potential buy</b>" if sp < -1.0 else (
               " <b>Premium - watch risk</b>" if sp > 2.0 else "")
        nav_section = f"""<div class="nav-bar">
          <b>MNVT NAV Monitor</b> &nbsp;
          Price <b>${nav_info['price']:.2f}</b> | NAV <b>${nav_info['nav']:.2f}</b>
          | Spread <b class="{cls}">{sp:+.2f}%</b>{hint}</div>"""

    fg_section = ""
    if fg:
        val = int(fg["value"])
        if val >= 75:   fg_color = "#c62828"
        elif val >= 55: fg_color = "#e65100"
        elif val >= 45: fg_color = "#555"
        elif val >= 25: fg_color = "#1565c0"
        else:           fg_color = "#4a148c"
        fg_section = f"""
        <div style="padding:10px 24px 0;font-size:13px">
          Market Sentiment: <span class="fg-bar" style="background:{fg_color}20;color:{fg_color}">{val} - {fg['label']}</span>
        </div>"""

    hold_section = ""
    if holdings:
        h_rows = "".join(f"<tr><td><b>{h['symbol']}</b></td><td style='color:#777'>{h.get('weight','-')}%</td></tr>"
                         for h in holdings[:15])
        hold_section = f"""<div class="section">
          <div class="section-title">MNVT Holdings</div>
          <table style="max-width:300px"><thead><tr><th>Symbol</th><th>Weight</th></tr></thead>
          <tbody>{h_rows}</tbody></table></div>"""

    subj = f"Daily Market Summary {today}"
    html = f"""<html><head><style>{STYLE}</style></head><body>
      <div class="card">
        <div class="header" style="background:#0d47a1">
          <h2>Daily Market Summary</h2><p>{now}</p></div>
        {fg_section}
        {nav_section}
        <div class="section">
          <div class="section-title">Stocks / ETFs</div>
          <table><thead><tr>
            <th>Symbol</th><th>Name</th><th>Price / 52w Range</th><th>Change</th><th>Volume</th>
          </tr></thead><tbody>{s_rows}</tbody></table>
        </div>
        <div class="section">
          <div class="section-title">Crypto</div>
          <table><thead><tr>
            <th>Symbol</th><th>Name</th><th>Price</th><th>24h Change</th><th></th>
          </tr></thead><tbody>{c_rows}</tbody></table>
        </div>
        {hold_section}
        <div class="footer">Data: Yahoo Finance / CoinGecko / Alternative.me. Not investment advice.</div>
      </div></body></html>"""
    return subj, html

# ------ send email ------------------------------------------------------------------------------------------------------------------------------------------------
def send_email(subject, html):
    sender   = os.environ["EMAIL_SENDER"]
    password = os.environ["EMAIL_PASSWORD"]
    receiver = os.environ["EMAIL_RECEIVER"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = receiver
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls()
            s.login(sender, password)
            s.send_message(msg)
        log.info(f"Email sent: {subject}")
    except Exception as e:
        log.error(f"Email failed: {e}")
        sys.exit(1)

# ------ main ------------------------------------------------------------------------------------------------------------------------------------------------------------------
def main():
    cfg      = load_config()
    state    = load_state()
    cooldown = cfg["cooldown"]["same_alert_cooldown_minutes"]
    fg       = fetch_fear_greed()

    if REPORT_MODE:
        log.info("=== Daily Report Mode ===")
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
        subj, html = daily_email(stock_rows, crypto_rows, nav_info, holdings, fg)
        send_email(subj, html)
        return

    log.info("=== Alert Check Mode ===")
    all_alerts = []

    for item in cfg["watchlist"]["stocks"]:
        d = fetch_stock(item["symbol"])
        if d:
            log.info(f"  {item['symbol']}: ${d['price']} ({d['pct_change']:+.2f}%)")
            all_alerts.extend(check_stock(item, d, state, cooldown))

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

    if not all_alerts:
        log.info("No alerts triggered.")
        save_state(state)
        return

    # Fetch news for each alerted symbol
    log.info(f"{len(all_alerts)} alerts triggered, fetching news + AI analysis...")
    for alert in all_alerts:
        sym = alert["symbol"]
        alert["news"] = fetch_news(sym)
        log.info(f"  News fetched for {sym}: {len(alert['news'])} items")

    # Build market context string for AI
    spy_d = fetch_stock("SPY")
    qqq_d = fetch_stock("QQQ")
    btc_d = fetch_crypto(["bitcoin"]).get("bitcoin", {})
    market_context = (
        f"SPY: {spy_d['pct_change']:+.2f}% today" if spy_d else "SPY: N/A"
    ) + " | " + (
        f"QQQ: {qqq_d['pct_change']:+.2f}% today" if qqq_d else "QQQ: N/A"
    ) + " | " + (
        f"BTC: {btc_d.get('usd_24h_change', 0):+.2f}% 24h" if btc_d else "BTC: N/A"
    ) + " | " + (
        f"Fear & Greed: {fg['value']} ({fg['label']})" if fg else "Fear & Greed: N/A"
    )

    # AI analysis
    analyses = ai_analyze(all_alerts, market_context)

    # Mark and send
    for a in all_alerts:
        mark(state, a["key"])
    subj, html = alert_email(all_alerts, analyses, fg)
    send_email(subj, html)
    log.info(f"Alert email sent with {len(all_alerts)} alerts.")

    save_state(state)

if __name__ == "__main__":
    main()
