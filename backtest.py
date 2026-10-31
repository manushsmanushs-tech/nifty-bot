import os
import pandas as pd
import requests
from bot import get_symbols, TZ, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, notify

PERIOD = os.getenv("PERIOD") or "7d"
MIN_MOVE = float(os.getenv("MIN_MOVE") or 1.0)
CHECK = [s.strip().upper().replace(".NS", "") for s in (os.getenv("CHECK") or "").split(",") if s.strip()]
CHECK_DATE = (os.getenv("CHECK_DATE") or "").strip()


def download(symbols):
    import yfinance as yf
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


def scan_symbol(sym, df):
    rows = []
    dates = df.index.date
    days = sorted(set(dates))
    for k in range(1, len(days)):
        prev_close = df[dates == days[k - 1]]["Close"].iloc[-1]
        day = df[dates == days[k]]
        t = day.index.strftime("%H:%M")
        o, h, l, c = (day[x].values for x in ("Open", "High", "Low", "Close"))
        done = set()
        last_up = last_dn = 0      # last earlier candle that closed above the day high / below the day low
        for i in range(1, len(day)):
            dh, dl = h[:i].max(), l[:i].min()
            side, p = None, None
            if c[i] > dh:
                if "BUY" not in done and (h[i] / prev_close - 1) * 100 >= MIN_MOVE:
                    p = find_setup(o, h, l, c, i, last_up, "BUY")
                    side = "BUY" if p is not None else None
                last_up = i
            elif c[i] < dl:
                if "SELL" not in done and (1 - l[i] / prev_close) * 100 >= MIN_MOVE:
                    p = find_setup(o, h, l, c, i, last_dn, "SELL")
                    side = "SELL" if p is not None else None
                last_dn = i
            if not side:
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
                "time": t[i],
                "symbol": sym.replace(".NS", ""),
                "side": side,
                "entry": round(float(entry), 2),
                "day_high_low": round(float(dh if side == "BUY" else dl), 2),
                "setup_candles": f"{t[p - 2]}-{t[p]}",
                "candles_waited": i - 1 - p,
                "dayend_pct": round(float(eod), 2),
                "best_pct": round(float(best), 2),
                "worst_pct": round(float(worst), 2),
            })
    return rows


def explain(sym, df):
    """Plain-language report: every 3-candle setup of one day and what happened after it."""
    dates = df.index.date
    days = sorted(set(dates))
    day_d = pd.Timestamp(CHECK_DATE).date() if CHECK_DATE else days[-1]
    name = sym.replace(".NS", "")
    if day_d not in days or days.index(day_d) == 0:
        return [f"{name}: no usable data for {day_d} (data has {days[0]} to {days[-1]})"]
    prev_close = df[dates == days[days.index(day_d) - 1]]["Close"].iloc[-1]
    day = df[dates == day_d]
    t = day.index.strftime("%H:%M")
    o, h, l, c = (day[x].values for x in ("Open", "High", "Low", "Close"))
    lines = [f"{name} on {day_d}: prev close {prev_close:.2f}, day high {h.max():.2f} "
             f"({(h.max() / prev_close - 1) * 100:+.1f}%), day low {l.min():.2f} "
             f"({(l.min() / prev_close - 1) * 100:+.1f}%)"]
    hits = [r for r in scan_symbol(sym, df) if r["date"] == str(day_d)]
    if hits:
        for r in hits:
            lines.append(f"==> BOT SIGNAL: {r['side']} at {r['time']} (setup {r['setup_candles']})")
    else:
        lines.append(f"==> BOT SIGNAL: none (min move {MIN_MOVE}%)")
    for side in ("BUY", "SELL"):
        found = []
        for p in range(3, len(day)):
            a = p - 2
            if side == "BUY":
                ok = all(c[x] < o[x] for x in (a, a + 1, p))
                ref = h[:a].max()
                below = h[a:p + 1].max() < ref
                closes = c[a] > c[a + 1] > c[p]
                ext = h[a] > h[a + 1] > h[p]
                brk = next((j for j in range(p + 1, len(day)) if c[j] > h[:j].max()), None)
                move = (h[brk] / prev_close - 1) * 100 if brk is not None else 0
            else:
                ok = all(c[x] > o[x] for x in (a, a + 1, p))
                ref = l[:a].min()
                below = l[a:p + 1].min() > ref
                closes = c[a] < c[a + 1] < c[p]
                ext = l[a] < l[a + 1] < l[p]
                brk = next((j for j in range(p + 1, len(day)) if c[j] < l[:j].min()), None)
                move = (1 - l[brk] / prev_close) * 100 if brk is not None else 0
            if not (ok and below):
                continue
            why = []
            if not closes:
                why.append("closes not each lower" if side == "BUY" else "closes not each higher")
            if not ext:
                why.append("highs not each lower" if side == "BUY" else "lows not each higher")
            if brk is None:
                why.append("price never broke it after this")
            elif move < MIN_MOVE:
                why.append(f"stock only {move:.1f}% at the breakout candle")
            lvl = "DH" if side == "BUY" else "DL"
            brk_txt = f"breakout {t[brk]}" if brk is not None else "no breakout"
            found.append(f" {t[a]}-{t[p]} {lvl} {ref:.2f}, {brk_txt}: "
                         + ("OK = SIGNAL" if not why else "; ".join(why)))
        lines.append(f"{side} setups (3 {'red' if side == 'BUY' else 'green'} candles "
                     f"staying below {'DH' if side == 'BUY' else 'DL'}): {len(found)}")
        lines += found[:10]
    return lines


def send_text(text):
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) > 3500:
            notify(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        notify(chunk)


def main():
    if CHECK:
        syms = [s + ".NS" for s in CHECK]
        data = download(syms)
        lines = []
        for sym in syms:
            if sym in data:
                lines += explain(sym, data[sym]) + [""]
            else:
                lines.append(f"{sym.replace('.NS', '')}: no data from Yahoo\n")
        send_text("\n".join(lines))
        return

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
    per_day = res.groupby("date").size()
    lines = [head, f"Total signals: {len(res)} "
             f"(BUY {(res.side == 'BUY').sum()}, SELL {(res.side == 'SELL').sum()})",
             "Per day: " + ", ".join(f"{d[5:]}: {n}" for d, n in per_day.items())]
    eod = res["dayend_pct"].dropna()
    if len(eod):
        lines.append(f"Day-end result after entry: avg {eod.mean():.2f}%, "
                     f"positive in {(eod > 0).mean() * 100:.0f}% of signals")
    lines.append("Full list is in backtest.csv")
    send_text("\n".join(lines))
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        with open("backtest.csv", "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                data={"chat_id": TELEGRAM_CHAT_ID}, files={"document": f}, timeout=30)
        print("Document response:", r.status_code)


if __name__ == "__main__":
    main()
