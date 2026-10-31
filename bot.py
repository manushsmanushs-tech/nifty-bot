import io
import os
import pandas as pd
import requests
import yfinance as yf

MIN_MOVE_PCT = 1.0
TZ = "Asia/Kolkata"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
URLS = [
    "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
]


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
    now = pd.Timestamp.now(tz=TZ)
    out = {}
    for sym in symbols:
        try:
            df = data[sym][["Open", "High", "Low", "Close"]].dropna()
        except KeyError:
            continue
        if df.empty:
            continue
        df.index = df.index.tz_convert(TZ) if df.index.tz else df.index.tz_localize(TZ)
        # drop the still-forming candle
        df = df[df.index + pd.Timedelta(minutes=5) <= now]
        if not df.empty:
            out[sym] = df
    return out


def check_signal(df):
    today = df.index[-1].date()
    day = df[df.index.date == today]
    prev = df[df.index.date < today]
    if prev.empty or len(day) < 4:
        return None

    prev_close = prev["Close"].iloc[-1]
    brk = day.iloc[-1]                      # breakout candle
    c1, c2, c3 = day.iloc[-4], day.iloc[-3], day.iloc[-2]
    before = day.iloc[:-1]
    day_high = before["High"].max()
    day_low = before["Low"].min()

    # ---------- LONG: stock rose >= 1% (measured at the Day High) ----------
    up_pct = (day_high / prev_close - 1) * 100
    if up_pct >= MIN_MOVE_PCT:
        if (all(c["Close"] < c["Open"] for c in (c1, c2, c3))
                and c2["Close"] < c1["Close"] and c3["Close"] < c2["Close"]
                and c2["High"] < c1["High"] and c3["High"] < c2["High"]
                and max(c1["High"], c2["High"], c3["High"]) < day_high
                and brk["Close"] > day_high):
            return "BUY"

    # ---------- SHORT: stock fell >= 1% (measured at the Day Low) ----------
    dn_pct = (1 - day_low / prev_close) * 100
    if dn_pct >= MIN_MOVE_PCT:
        if (all(c["Close"] > c["Open"] for c in (c1, c2, c3))
                and c2["Close"] > c1["Close"] and c3["Close"] > c2["Close"]
                and c2["Low"] > c1["Low"] and c3["Low"] > c2["Low"]
                and min(c1["Low"], c2["Low"], c3["Low"]) > day_low
                and brk["Close"] < day_low):
            return "SELL"
    return None


def notify(msg):
    print(msg, flush=True)
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: secrets are missing or empty")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
        print("Telegram response:", r.status_code, r.text)
    except Exception as e:
        print("Telegram error:", e)


def main():
    manual = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if manual:
        notify("Test: your bot is working")
    now = pd.Timestamp.now(tz=TZ)
    if now.weekday() >= 5 or not ("09:15" <= now.strftime("%H:%M") <= "15:40"):
        print("Market closed")
        return
    symbols = get_symbols()
    print("Symbols:", len(symbols))
    for sym, df in fetch_5m(symbols).items():
        # only alert if the breakout candle closed within the last 10 minutes
        if now - (df.index[-1] + pd.Timedelta(minutes=5)) > pd.Timedelta(minutes=10):
            continue
        sig = check_signal(df)
        if sig:
            last = df.iloc[-1]
            notify(f"{sig}: {sym.replace('.NS', '')} | close {last['Close']:.2f} | "
                   f"candle {df.index[-1].strftime('%H:%M')}")


if __name__ == "__main__":
    main()
