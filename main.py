"""Level Obedience: NIFTY levels scorer + paper trader (Angel One SmartAPI).
Run: uvicorn main:app --port 8000   then open http://localhost:8000
Paper trading only. No orders are ever sent to the broker."""
import os, json, asyncio, datetime as dt
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo
import pyotp
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from SmartApi import SmartConnect

load_dotenv()
IST = ZoneInfo("Asia/Kolkata")
TOKEN, TSYM = "99926000", "Nifty 50"          # NIFTY 50 index on NSE
TRADES = Path("trades.json")
trades = json.loads(TRADES.read_text()) if TRADES.exists() else []
S = {"spot": None, "atr": None, "levels": [], "updated": None, "error": None}
api = None

# ---------- Angel One ----------
def login():
    global api
    api = SmartConnect(api_key=os.environ["ANGEL_API_KEY"])
    r = api.generateSession(os.environ["ANGEL_CLIENT_CODE"], os.environ["ANGEL_PIN"],
                            pyotp.TOTP(os.environ["ANGEL_TOTP_SECRET"]).now())
    if not r.get("status"):
        raise RuntimeError(r.get("message", "login failed"))

def candles(interval, days):
    now = dt.datetime.now(IST)
    r = api.getCandleData({"exchange": "NSE", "symboltoken": TOKEN, "interval": interval,
        "fromdate": (now - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M"),
        "todate": now.strftime("%Y-%m-%d %H:%M")})
    if not r.get("status") or not r.get("data"):
        raise RuntimeError(r.get("message", "no candle data"))
    return [{"t": c[0], "o": c[1], "h": c[2], "l": c[3], "c": c[4]} for c in r["data"]]

# ---------- indicators ----------
def atr(c, n=14):
    tr = [max(x["h"] - x["l"], abs(x["h"] - p["c"]), abs(x["l"] - p["c"])) for p, x in zip(c, c[1:])]
    return sum(tr[-n:]) / n

def ema(v, n):
    k, e = 2 / (n + 1), v[0]
    for x in v[1:]:
        e = x * k + e * (1 - k)
    return e

# ---------- level generators ----------
def build_levels(m5, d1):
    today = m5[-1]["t"][:10]
    days = [d for d in d1 if d["t"][:10] < today]
    pd = days[-1]
    L = []
    def add(name, group, lo, hi=None, dynamic=False):
        hi = lo if hi is None else hi
        L.append({"name": name, "group": group, "lo": round(min(lo, hi), 2), "hi": round(max(lo, hi), 2),
                  "price": round((lo + hi) / 2, 2), "dynamic": dynamic})
    add("Prev day high", "Previous day", pd["h"]); add("Prev day low", "Previous day", pd["l"])
    add("Prev day close", "Previous day", pd["c"])
    wk = lambda d: dt.date.fromisoformat(d["t"][:10]).isocalendar()[:2]
    cur = dt.date.fromisoformat(today).isocalendar()[:2]
    pw = [d for d in days if wk(d) != cur]
    if pw:
        last = wk(pw[-1]); pw = [d for d in pw if wk(d) == last]
        add("Prev week high", "Previous week", max(d["h"] for d in pw))
        add("Prev week low", "Previous week", min(d["l"] for d in pw))
    h, l, c = pd["h"], pd["l"], pd["c"]
    P = (h + l + c) / 3
    for n, v in {"Pivot": P, "R1": 2 * P - l, "S1": 2 * P - h, "R2": P + h - l, "S2": P - (h - l)}.items():
        add(n, "Pivot points", v)
    bc = (h + l) / 2; tc = 2 * P - bc
    add("CPR", "CPR", bc, tc)
    tm = [x for x in m5 if x["t"][:10] == today][:3]
    if len(tm) == 3:
        add("Opening range high", "Opening range", max(x["h"] for x in tm))
        add("Opening range low", "Opening range", min(x["l"] for x in tm))
    closes = [x["c"] for x in m5]
    add("EMA 20 (5m)", "Moving averages", ema(closes, 20), dynamic=True)
    add("EMA 50 (5m)", "Moving averages", ema(closes, 50), dynamic=True)
    sw = days[-5:]; hi, lo = max(d["h"] for d in sw), min(d["l"] for d in sw)
    for r in (0.382, 0.5, 0.618):
        add(f"Fib {r*100:.1f}% (5d swing)", "Fibonacci", hi - (hi - lo) * r)
    recent = m5[-150:]
    for k, key, nm in ((("l", lambda a, b: a < b), "l", "Swing low"), (("h", lambda a, b: a > b), "h", "Swing high")):
        pts = [recent[i][key] for i in range(2, len(recent) - 2)
               if all(k[1](recent[i][key], recent[i + j][key]) for j in (-2, -1, 1, 2))]
        for v in pts[-3:]:
            add(nm, "Swings", v)
    return L

# ---------- "obedience" scoring: did price respect the level? ----------
def score(lv, m5, a):
    tol = 0.15 * a
    lo, hi, p = lv["lo"] - tol, lv["hi"] + tol, lv["price"]
    held = broke = 0
    i = 1
    while i < len(m5) - 1:
        x = m5[i]
        if x["l"] <= hi and x["h"] >= lo:
            prev = m5[i - 1]["c"]
            side = 1 if prev > hi else -1 if prev < lo else 0
            if side:
                res, best = None, 0
                for y in m5[i + 1:i + 7]:
                    d = (y["c"] - p) * side
                    if d < -0.25 * a: res = "broke"; break   # closed through the level
                    best = max(best, d)
                    if best >= 0.5 * a: res = "held"; break  # bounced away
                held += res == "held"; broke += res == "broke"
                i += 6
                continue
        i += 1
    return held, broke

# ---------- paper trader ----------
def save():
    TRADES.write_text(json.dumps(trades, indent=1))

def close(t, px, why):
    t.update(status="closed", reason=why, exit=round(px, 2), exit_time=dt.datetime.now(IST).isoformat(timespec="seconds"),
             pnl=round((px - t["entry"]) * (1 if t["side"] == "LONG" else -1), 2))

def paper(spot, a, levels):
    now = dt.datetime.now(IST); hm = now.hour * 60 + now.minute
    for t in [t for t in trades if t["status"] == "open"]:
        lng = t["side"] == "LONG"
        if (spot <= t["sl"]) if lng else (spot >= t["sl"]): close(t, spot, "stop")
        elif (spot >= t["tp"]) if lng else (spot <= t["tp"]): close(t, spot, "target")
        elif hm >= 15 * 60 + 15: close(t, spot, "square-off")
    if now.weekday() > 4 or not (9 * 60 + 20 <= hm < 15 * 60 + 15) or any(t["status"] == "open" for t in trades):
        save(); return
    used = {t["level"] for t in trades if t["time"][:10] == now.date().isoformat()}
    ok = [l for l in levels if not l["dynamic"] and l["held"] >= 2 and l["held"] / (l["held"] + l["broke"]) >= 0.6
          and abs(spot - l["price"]) <= 0.25 * a and f'{l["name"]} {l["price"]}' not in used]
    if ok:
        l = min(ok, key=lambda l: abs(spot - l["price"]))
        lng = spot >= l["price"]                       # above level = support bounce
        trades.append({"time": now.isoformat(timespec="seconds"), "side": "LONG" if lng else "SHORT",
            "level": f'{l["name"]} {l["price"]}', "entry": spot,
            "sl": round(l["price"] - 0.5 * a if lng else l["price"] + 0.5 * a, 2),
            "tp": round(spot + 1.5 * a if lng else spot - 1.5 * a, 2), "status": "open"})
    save()

# ---------- refresh loop ----------
async def refresh():
    global api
    while True:
        try:
            if api is None: await asyncio.to_thread(login)
            m5 = await asyncio.to_thread(candles, "FIVE_MINUTE", 10)
            d1 = await asyncio.to_thread(candles, "ONE_DAY", 30)
            spot = (await asyncio.to_thread(api.ltpData, "NSE", TSYM, TOKEN))["data"]["ltp"]
            a = atr(m5)
            lv = build_levels(m5, d1)
            for l in lv:
                l["held"], l["broke"] = (0, 0) if l["dynamic"] else score(l, m5, a)
            paper(spot, a, lv)
            S.update(spot=spot, atr=round(a, 2), levels=lv, error=None,
                     updated=dt.datetime.now(IST).isoformat(timespec="seconds"))
        except Exception as e:
            S["error"] = str(e); api = None            # force re-login next cycle
        await asyncio.sleep(30)

@asynccontextmanager
async def lifespan(_):
    task = asyncio.create_task(refresh())
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)
@app.get("/")
def index(): return FileResponse("index.html")
@app.get("/api/state")
def state(): return S
@app.get("/api/trades")
def get_trades(): return trades[::-1]
