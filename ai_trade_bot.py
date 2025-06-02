
"""
AI Crypto Trading Bot ＋ Interactive Dashboard
============================================
Free, no‑signup data sources with **full transparency** — the UI now shows
exactly what prompt is sent to Groq and the raw AI response, along with the
configured polling interval.

* **Live price & history**  → CoinGecko
* **Crypto headlines**      → CoinDesk RSS
* **7‑day forecast**        → CoinCodex (speculative!)
* **AI decision**           → Groq LLM (set `GROQ_API_KEY` below)
* **Charts & UI**           → Flask × Tailwind × Chart.js

```bash
git clone https://github.com/hejhdiss/ai-crypto-trading-.git
cd ai-crypto-trading-
pip install flask requests feedparser groq argparse
python ai_trade_bot.py        # open http://127.0.0.1:5000
```
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from datetime import datetime
from typing import Dict, List, Tuple

import feedparser
import requests
from flask import Flask, jsonify, render_template_string, request
from groq import Groq

# ────────────────────────────────────────────────
# Config & constants
# ────────────────────────────────────────────────
COINDESK_RSS = "https://feeds.feedburner.com/CoinDesk"
COINGECKO_LIST_URL = "https://api.coingecko.com/api/v3/coins/list"
COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_CHART_URL = (
    "https://api.coingecko.com/api/v3/coins/{id}/market_chart?vs_currency={vs}&days={days}&interval=hourly"
)
COINCODEx_PRED_URL = "https://coincodex.com/api/coindata/{sym}"

GROQ_API_KEY = os.getenv("GROQ_API_KEY") or "YOUR_GROQ_API_KEY_HERE"

TAILWIND_CDN = "https://cdn.tailwindcss.com"
CHARTJS_CDN   = "https://cdn.jsdelivr.net/npm/chart.js"

# ────────────────────────────────────────────────
# Helpers — CoinGecko symbol map / price / history / forecast
# ────────────────────────────────────────────────

def _symbol_map() -> Dict[str, str]:
    data = requests.get(COINGECKO_LIST_URL, timeout=20).json()
    return {c["symbol"].upper(): c["id"] for c in data}


SYMBOL_MAP = _symbol_map()


def get_price(sym: str, vs: str) -> float:
    cid = SYMBOL_MAP.get(sym.upper())
    if not cid:
        raise ValueError(f"Unknown symbol {sym}")
    url = f"{COINGECKO_PRICE_URL}?ids={cid}&vs_currencies={vs.lower()}"
    return float(requests.get(url, timeout=10).json()[cid][vs.lower()])


def get_history(sym: str, vs: str, days: int = 1) -> List[Tuple[int, float]]:
    cid = SYMBOL_MAP.get(sym.upper())
    url = COINGECKO_CHART_URL.format(id=cid, vs=vs.lower(), days=days)
    data = requests.get(url, timeout=10).json()
    return [(int(p[0]), float(p[1])) for p in data.get("prices", [])]


def get_prediction(sym: str, vs: str = "USD") -> float | None:
    try:
        j = requests.get(COINCODEx_PRED_URL.format(sym=sym.lower()), timeout=10).json()
        pred_usd = float(j["predictions"]["price_prediction_7d"])
        if vs.upper() == "USD":
            return pred_usd
        cur_vs  = get_price(sym, vs)
        cur_usd = get_price(sym, "USD")
        return pred_usd / cur_usd * cur_vs if cur_usd else None
    except Exception:
        return None

# ────────────────────────────────────────────────
# Groq — build prompt & get decision
# ────────────────────────────────────────────────

def groq_decision(news: List[str], price: float, prediction: float | None,
                  recent: List[float], coin: str, vs: str) -> Tuple[str, str, str]:
    """Return (decision, prompt_sent, raw_ai_response)."""
    pred_text   = f"{prediction:.6f}" if prediction is not None else "N/A"
    recent_line = ", ".join(f"{p:.6f}" for p in recent)
    prompt = (
        f"News Headlines:\n" + "\n".join(f"- {h}" for h in news) + "\n\n"
        f"Current Price {coin}/{vs}: {price}\n"
        f"7‑day Forecast: {pred_text}\n"
        f"Recent Hourly Prices: {recent_line}\n\n"
        "Respond with BUY, SELL, or HOLD (one word)."
    )
    if not GROQ_API_KEY or GROQ_API_KEY == "YOUR_GROQ_API_KEY_HERE":
        return "HOLD", prompt, "Groq key missing — default HOLD"

    client = Groq(api_key=GROQ_API_KEY)
    resp = client.chat.completions.create(
        model="allam-2-7b",
        messages=[{"role": "user", "content": prompt}],
    )
    ai_raw = resp.choices[0].message.content.strip()
    decision = ai_raw.split()[0].upper()
    return decision, prompt, ai_raw

# ────────────────────────────────────────────────
# Background Bot
# ────────────────────────────────────────────────



class TradingBot(threading.Thread):
    def __init__(self, coin="XRP", vs="USD", interval=60, log_file="trade_log.json"):
        super().__init__(daemon=True)
        self.coin, self.vs, self.interval = coin.upper(), vs.upper(), interval
        self.lock = threading.Lock()
        self._snap: Dict = {}
        self.log: List[Dict] = []           # <-- in-memory log list
        self.log_file = log_file            # <-- filename to save log
        self.start()

    def save_log(self):
        """Save the full log to JSON file."""
        try:
            with open(self.log_file, "w", encoding="utf-8") as f:
                json.dump(self.log, f, indent=2)
        except Exception as e:
            print("[bot] Error saving log:", e)

    def run(self):
        while True:
            try:
                price = get_price(self.coin, self.vs)
                hist  = get_history(self.coin, self.vs, days=1)[-6:]  # last ~6 hours
                news  = latest_news()
                pred  = get_prediction(self.coin, self.vs)
                recent_vals = [p[1] for p in hist]
                decision, prompt, ai_raw = groq_decision(news, price, pred, recent_vals,
                                                          self.coin, self.vs)
                with self.lock:
                    self._snap = {
                        "time": datetime.utcnow().isoformat() + "Z",
                        "coin": self.coin,
                        "vs":   self.vs,
                        "price": price,
                        "prediction": pred,
                        "decision": decision,
                        "prompt": prompt,
                        "ai_raw": ai_raw,
                        "interval": self.interval,
                        "news": news,
                    }
                    # Append snapshot to log
                    self.log.append(dict(self._snap))
                    # Save log to file after each update 
                    self.save_log()
            except Exception as exc:
                print("[bot]", exc)
            time.sleep(max(5, self.interval))

    # API helpers
    def snapshot(self):
        with self.lock:
            return dict(self._snap)

    def configure(self, coin, vs, interval):
        with self.lock:
            self.coin, self.vs, self.interval = coin.upper(), vs.upper(), max(5, interval)
            self._snap["status"] = "reconfigured"


# ────────────────────────────────────────────────
# Flask app & UI
# ────────────────────────────────────────────────

a = Flask(__name__)
bot = TradingBot()

HTML = """
<!DOCTYPE html><html><head><meta charset=utf-8><title>AI Crypto Dashboard</title>
<script src='{TAILWIND_CDN}'></script>
<script src='{CHARTJS_CDN}'></script>
</head><body class='bg-gray-100 p-4 flex flex-col items-center min-h-screen'>
<h1 class='text-3xl font-bold mb-4 text-center'>AI Crypto Trading Dashboard</h1>
<form id=cfg class='flex flex-wrap gap-2 bg-white shadow rounded-xl p-4 mb-6'>
  <input name=coin placeholder='Coin (XRP)' class='border p-2 rounded w-28' required>
  <input name=vs   placeholder='Quote (USD)' class='border p-2 rounded w-28' value='USD'>
  <input name=interval type=number min=5 placeholder='Interval s' value=60 class='border p-2 rounded w-28'>
  <button class='bg-blue-600 text-white px-4 py-2 rounded hover:bg-blue-700'>Update</button>
</form>
<div id=panel class='w-full max-w-3xl bg-white rounded-xl shadow p-6'>
  <div class='grid grid-cols-2 md:grid-cols-3 gap-4 text-lg'>
    <p><span class='font-semibold'>Pair:</span> <span id=pair>—</span></p>
    <p><span class='font-semibold'>Price:</span> <span id=price>—</span></p>
    <p><span class='font-semibold'>AI Decision:</span> <span id=decision>—</span></p>
    <p><span class='font-semibold'>7‑day Pred:</span> <span id=pred>—</span></p>
    <p><span class='font-semibold'>Updated:</span> <span id=time>—</span></p>
    <p><span class='font-semibold'>Interval:</span> <span id=interval>—</span> s</p>
  </div>
  <canvas id=chart class='w-full h-64 my-6'></canvas>
  <h2 class='text-xl font-semibold mb-2'>Latest Headlines</h2>
  <ul id=news class='list-disc pl-5 space-y-1 text-sm text-gray-700 mb-6'></ul>
  <details class='mb-2'>
    <summary class='cursor-pointer text-sm font-semibold'>Debug ▸ Prompt & AI Response</summary>
    <textarea id=prompt class='w-full border rounded p-2 text-xs mt-2' rows=8 readonly></textarea>
    <p class='text-xs mt-1'><span class='font-semibold'>AI raw:</span> <span id=ai_raw class='break-words'></span></p>
  </details>
  <p class='text-xs text-gray-500'>Forecasts from CoinCodex are speculative and may be inaccurate. Use at your own risk.</p>
</div>

<script>
let chart;
const cfgForm = document.getElementById('cfg');
function fmt(n){return (typeof n==='number')?n.toFixed(6):n;}
async function pull(){
  const r = await fetch('/api/latest'); if(!r.ok) return;
  const d = await r.json();
  document.getElementById('pair').textContent = d.coin+'/'+d.vs;
  document.getElementById('price').textContent = fmt(d.price);
  document.getElementById('decision').textContent = d.decision;
  document.getElementById('pred').textContent = d.prediction?fmt(d.prediction):'—';
  document.getElementById('time').textContent  = d.time?.replace('T',' ').replace('Z','');
  document.getElementById('interval').textContent = d.interval;
  document.getElementById('prompt').value = d.prompt||'';
  document.getElementById('ai_raw').textContent = d.ai_raw||'—';
  const n = document.getElementById('news'); n.innerHTML='';
  (d.news||[]).forEach(t=>{ const li=document.createElement('li'); li.textContent=t; n.appendChild(li); });
}
async function draw(){
  const res = await fetch('/api/history'); if(!res.ok) return;
  const hist = await res.json();
  const labels = hist.map(p=>new Date(p[0]).toLocaleTimeString());
  const prices = hist.map(p=>p[1]);
  if(chart) chart.destroy();
  chart = new Chart(document.getElementById('chart'),{
    type:'line',
    data:{labels, datasets:[{label:'Price', data:prices, fill:false}]},
    options:{plugins:{legend:{display:false}}, scales:{x:{display:false}}}
  });
}
setInterval(()=>{pull(); draw();}, 60000);
window.addEventListener('DOMContentLoaded', ()=>{pull(); draw();});
cfgForm.addEventListener('submit', async e=>{
  e.preventDefault();
  const body=Object.fromEntries(new FormData(cfgForm).entries());
  body.interval=parseInt(body.interval);
  await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  setTimeout(()=>{pull(); draw();}, 1000);
});
</script>
</body></html>
"""

# ────────────────────────────────────────────────
# News helper
# ────────────────────────────────────────────────

def latest_news(limit: int = 5) -> List[str]:
    return [e.title for e in feedparser.parse(COINDESK_RSS).entries[:limit]]

# ────────────────────────────────────────────────
# Flask routes
# ────────────────────────────────────────────────

@a.route("/")
def index():
    return render_template_string(HTML)


@a.route("/api/latest")
def api_latest():
    return jsonify(bot.snapshot())


@a.route("/api/log")
def api_log():
    with bot.lock:
        return jsonify(bot.log)

@a.route("/api/history")
def api_history():
    with bot.lock:
        # Return last 24h hourly price data 
        hist = get_history(bot.coin, bot.vs, days=1)
    return jsonify(hist)
if __name__ == "__main__":
    # Run the Flask web server 
    a.run(debug=True, host="127.0.0.1", port=5000)
