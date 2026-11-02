import io
import json
import os
import time
import pandas as pd
import requests
import yfinance as yf

MIN_MOVE_PCT = 1.0
LOOKBACK_MIN = 30      # alert for breakout candles that closed within this many minutes
TZ = "Asia/Kolkata"
STATE_FILE = "sent.json"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
URLS = [
    "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
]


def now_ist():
    return pd.Timestamp.now(tz=TZ)


def get_symbols():
    for u in URLS:
        try:
            r = requests.get(u, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            return [s.strip() + ".NS" for s in df["Symbol"]]
        except Exception as e:
            print("List error:", u, e)
    return []


def fetch_5m(symbols):
    data = yf.download(symbols, period="5d", interval="5m", group_by="ticker",
                       threads=True, progress=False, auto_adjust=False)
    now = now_ist()
    out = {}
    for sym in symbols:
        try:
            df = data[sym][["Open", "High", "Low", "Close"]].dropna()
        except KeyError:
            continue
        if df.empty:
            continue
        df.index = df.index.tz_convert(TZ) if df.index.tz else df.index.tz_localize(TZ)
        df = df[df.index + pd.Timedelta(minutes=5) <= now]   # drop the still-forming candle
        if not df.empty:
            out[sym] = df
    return out


def setup_ok(o, h, l, c, p, side):
    """Are candles p-2, p-1, p a valid 3-candle setup that stays clear of the day high/low?"""
    a, b = p - 2, p - 1
    if side == "BUY":
        return (c[a] < o[a] and c[b] < o[b] and c[p] < o[p]
                and c[b] < c[a] and c[p] < c[b]
                and h[b] < h[a] and h[p] < h[b]
                and h[a] < h[:a].max())
    return (c[a] > o[a] and c[b] > o[b] and c[p] > o[p]
            and c[b] > c[a] and c[p] > c[b]
            and l[b] > l[a] and l[p] > l[b]
            and l[a] > l[:a].min())


def find_setup(o, h, l, c, i, last, side):
    """Latest valid setup formed after the previous breakout; any number of candles may pass before candle i."""
    for p in range(i - 1, max(last, 2), -1):
        if setup_ok(o, h, l, c, p, side):
            return p
    return None


def day_signals(df):
    """Every breakout signal on the latest trading day in df (same rules as the backtest)."""
    dates = df.index.date
    today = df.index[-1].date()
    prev = df[dates < today]
    if prev.empty:
        return []
    prev_close = prev["Close"].iloc[-1]
    day = df[dates == today]
    t = day.index.strftime("%H:%M")
    o, h, l, c = (day[x].values for x in ("Open", "High", "Low", "Close"))
    out = []
    last_up = last_dn = 0      # last earlier candle that closed above the day high / below the day low
    for i in range(1, len(day)):
        dh, dl = h[:i].max(), l[:i].min()
        if c[i] > dh:
            move = (h[i] / prev_close - 1) * 100
            if move >= MIN_MOVE_PCT:
                p = find_setup(o, h, l, c, i, last_up, "BUY")
                if p is not None:
                    out.append({"side": "BUY", "ts": day.index[i], "time": t[i], "close": c[i],
                                "level": dh, "move": move, "setup": f"{t[p - 2]}-{t[p]}"})
            last_up = i
        elif c[i] < dl:
            move = (1 - l[i] / prev_close) * 100
            if move >= MIN_MOVE_PCT:
                p = find_setup(o, h, l, c, i, last_dn, "SELL")
                if p is not None:
                    out.append({"side": "SELL", "ts": day.index[i], "time": t[i], "close": c[i],
                                "level": dl, "move": move, "setup": f"{t[p - 2]}-{t[p]}"})
            last_dn = i
    return out


def build_msg(sym, s, age_min):
    name = sym.replace(".NS", "")
    if s["side"] == "BUY":
        l1 = f"BUY: {name}"
        l2 = f"Candle {s['time']} closed at {s['close']:.2f}, above Day High {s['level']:.2f}"
        l3 = f"Up {s['move']:.1f}% today (high of this candle)"
    else:
        l1 = f"SELL: {name}"
        l2 = f"Candle {s['time']} closed at {s['close']:.2f}, below Day Low {s['level']:.2f}"
        l3 = f"Down {s['move']:.1f}% today (low of this candle)"
    l4 = f"Setup candles: {s['setup']}"
    l5 = f"Candle closed {age_min:.0f} min ago"
    return "\n".join([l1, l2, l3, l4, l5])


def notify(msg):
    print(msg, flush=True)
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: secrets are missing or empty")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
        print("Telegram response:", r.status_code, r.text[:200])
        return r.ok
    except Exception as e:
        print("Telegram error:", e)
        return False


def load_sent(today):
    try:
        with open(STATE_FILE) as f:
            return {k for k in json.load(f) if k.startswith(today)}
    except Exception:
        return set()


def save_sent(sent):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(sent), f)


def main():
    if os.getenv("TEST_MODE") == "true":
        notify("Test: your bot is working")
    now = now_ist()
    if now.weekday() >= 5 or not ("09:15" <= now.strftime("%H:%M") <= "15:50"):
        print("Market closed")
        return
    symbols = get_symbols()
    print("Symbols:", len(symbols))
    if not symbols:
        return
    sent = load_sent(now.strftime("%Y-%m-%d"))
    count = 0
    for sym, df in fetch_5m(symbols).items():
        for s in day_signals(df):
            age = (now - (s["ts"] + pd.Timedelta(minutes=5))).total_seconds() / 60
            if age > LOOKBACK_MIN:
                continue
            key = f"{s['ts']:%Y-%m-%d}|{sym}|{s['side']}|{s['time']}"
            if key in sent:
                continue
            if notify(build_msg(sym, s, age)):
                sent.add(key)
                count += 1
                time.sleep(3)      # stay under Telegram's message-rate limit
    save_sent(sent)
    print("New signals sent:", count)


if __name__ == "__main__":
    main()
