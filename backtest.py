import os
import pandas as pd
import requests
import yfinance as yf
from bot import get_symbols, TZ, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, notify

PERIOD = os.getenv("PERIOD") or "7d"
MIN_MOVE = float(os.getenv("MIN_MOVE") or 1.0)


def download(symbols):
    data = yf.download(symbols, period=PERIOD, interval="5m", group_by="ticker",
                       threads=True, progress=False, auto_adjust=False)
    out = {}
    for sym in symbols:
        try:
            df = data[sym][["Open", "High", "Low", "Close"]].dropna()
        except KeyError:
            continue
        if df.empty:
            continue
        df.index = df.index.tz_convert(TZ) if df.index.tz else df.index.tz_localize(TZ)
        out[sym] = df
    return out


def scan_symbol(sym, df):
    rows = []
    dates = df.index.date
    days = sorted(set(dates))
    for k in range(1, len(days)):
        prev_close = df[dates == days[k - 1]]["Close"].iloc[-1]
        day = df[dates == days[k]]
        o, h, l, c = (day[x].values for x in ("Open", "High", "Low", "Close"))
        done = set()
        for i in range(3, len(day)):
            a, b, d = i - 3, i - 2, i - 1
            dh, dl = h[:i].max(), l[:i].min()
            side = None
            if ((dh / prev_close - 1) * 100 >= MIN_MOVE
                    and c[a] < o[a] and c[b] < o[b] and c[d] < o[d]
                    and c[b] < c[a] and c[d] < c[b]
                    and h[b] < h[a] and h[d] < h[b]
                    and max(h[a], h[b], h[d]) < dh and c[i] > dh):
                side = "BUY"
            elif ((1 - dl / prev_close) * 100 >= MIN_MOVE
                    and c[a] > o[a] and c[b] > o[b] and c[d] > o[d]
                    and c[b] > c[a] and c[d] > c[b]
                    and l[b] > l[a] and l[d] > l[b]
                    and min(l[a], l[b], l[d]) > dl and c[i] < dl):
                side = "SELL"
            if not side or side in done:
                continue
            done.add(side)
            entry = c[i]
            eod = best = worst = float("nan")
            if i + 1 < len(day):
                if side == "BUY":
                    eod = (c[-1] / entry - 1) * 100
                    best = (h[i + 1:].max() / entry - 1) * 100
                    worst = (l[i + 1:].min() / entry - 1) * 100
                else:
                    eod = (1 - c[-1] / entry) * 100
                    best = (1 - l[i + 1:].min() / entry) * 100
                    worst = (1 - h[i + 1:].max() / entry) * 100
            rows.append({
                "date": str(days[k]),
                "time": day.index[i].strftime("%H:%M"),
                "symbol": sym.replace(".NS", ""),
                "side": side,
                "entry": round(float(entry), 2),
                "day_high_low": round(float(dh if side == "BUY" else dl), 2),
                "dayend_pct": round(float(eod), 2),
                "best_pct": round(float(best), 2),
                "worst_pct": round(float(worst), 2),
            })
    return rows


def main():
    symbols = get_symbols()
    data = download(symbols)
    rows = []
    for sym, df in data.items():
        rows += scan_symbol(sym, df)
    head = f"Backtest {PERIOD}, min move {MIN_MOVE}%, {len(data)} stocks"
    if not rows:
        notify(head + ": no signals found.")
        return
    res = pd.DataFrame(rows).sort_values(["date", "time"])
    res.to_csv("backtest.csv", index=False)
    lines = [head, f"Total signals: {len(res)} "
             f"(BUY {(res.side == 'BUY').sum()}, SELL {(res.side == 'SELL').sum()})"]
    eod = res["dayend_pct"].dropna()
    if len(eod):
        lines.append(f"Day-end result after entry: avg {eod.mean():.2f}%, "
                     f"positive in {(eod > 0).mean() * 100:.0f}% of signals")
    lines.append("")
    for r in res.head(25).itertuples():
        lines.append(f"{r.date} {r.time} {r.side} {r.symbol} @ {r.entry}")
    notify("\n".join(lines))
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        with open("backtest.csv", "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                data={"chat_id": TELEGRAM_CHAT_ID}, files={"document": f}, timeout=30)
        print("Document response:", r.status_code)


if __name__ == "__main__":
    main()
