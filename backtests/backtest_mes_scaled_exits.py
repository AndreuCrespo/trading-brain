"""Scaled-exit experiment on the acceptance-filtered BPB proxy.

Adrián's management (WhatsApp 11-jul): stop 1R (6 pts), take half at 2R (12),
final half at 3R (18); move stop to breakeven after TP1. Lets a sub-50%
win rate stay profitable if avg win >> avg loss.

Compares three exit models on the SAME acceptance-filtered trades (A=1):
  A) single exit at next known level (original backtest)
  B) fixed 2R single target
  C) scaled: half at 2R, stop->BE, runner half at 3R
Walks 1-minute bars; intra-bar resolves stop-first (conservative).
"""
import glob
import struct
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, r"D:\inversion\sierra-mcp\src")
from sierra_mcp import scid_reader

PATH = r"D:\SierraChart\Data\MESU26.scid"
TICK = 0.25
VA_PCT = 0.70
STOP_PTS = 6.0
PULLBACK_WINDOW_MIN = 120
LOOKBACK_DAYS = 28
ACCEPT_MIN = 1  # best config from obs #13

SC_OFFSET = (scid_reader.SC_EPOCH - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds()
S2200 = 22 * 3600
RTH_OPEN_S = 13 * 3600 + 30 * 60
RTH_CLOSE_S = 20 * 3600

cutoff_scdt = int((time.time() - LOOKBACK_DAYS * 86400 - SC_OFFSET) * 1e6)


def session_start(unix):
    return int((unix - S2200) // 86400) * 86400 + S2200


minute_bars, vol_by_price = {}, {}
with open(PATH, "rb") as f:
    f.seek(scid_reader.HEADER_SIZE)
    rs, fmt = scid_reader.RECORD_SIZE, scid_reader.RECORD_STRUCT
    while True:
        blob = f.read(rs * 2_000_000)
        if not blob:
            break
        for rec in fmt.iter_unpack(blob[: len(blob) - len(blob) % rs]):
            if rec[0] < cutoff_scdt:
                continue
            unix = rec[0] * 1e-6 + SC_OFFSET
            close, vol = rec[4], rec[6]
            m = int(unix // 60) * 60
            b = minute_bars.get(m)
            if b is None:
                minute_bars[m] = [close, close, close, close, vol]
            else:
                b[1] = max(b[1], close); b[2] = min(b[2], close); b[3] = close; b[4] += vol
            sess = session_start(unix)
            lad = vol_by_price.setdefault(sess, defaultdict(int))
            lad[round(close / TICK) * TICK] += vol

minutes_sorted = sorted(minute_bars)
sessions = sorted(vol_by_price)


def profile(sess):
    lad = vol_by_price[sess]
    prices = sorted(lad)
    total = sum(lad.values())
    poc = max(prices, key=lambda p: lad[p])
    target = total * VA_PCT
    sel, sv = {poc}, lad[poc]
    lo = hi = prices.index(poc)
    while sv < target and (lo > 0 or hi < len(prices) - 1):
        below = lad[prices[lo - 1]] if lo > 0 else -1
        above = lad[prices[hi + 1]] if hi < len(prices) - 1 else -1
        if above >= below and hi < len(prices) - 1:
            hi += 1; sel.add(prices[hi]); sv += above
        elif lo > 0:
            lo -= 1; sel.add(prices[lo]); sv += below
        else:
            break
    return {"poc": poc, "vah": max(sel), "val": min(sel)}


def bars_between(t0, t1):
    return [(m, *minute_bars[m]) for m in minutes_sorted if t0 <= m < t1]


def hilo(bars):
    return (max(b[2] for b in bars), min(b[3] for b in bars)) if bars else (None, None)


# build trades with acceptance filter A=1
trades = []
for i, sess in enumerate(sessions):
    if i == 0:
        continue
    prev = sessions[i - 1]
    if prev < sess - 4 * 86400:
        continue
    rth_open = session_start(sess) // 86400 * 86400 + 86400 + RTH_OPEN_S
    rth = bars_between(rth_open, rth_open - RTH_OPEN_S + RTH_CLOSE_S)
    if len(rth) < 60:
        continue
    p = profile(prev)
    open_px = rth[0][1]
    bs = None
    for j, b in enumerate(rth):
        if b[4] > p["vah"] + TICK and open_px <= p["vah"]:
            bs = ("long", j, p["vah"]); break
        if b[4] < p["val"] - TICK and open_px >= p["val"]:
            bs = ("short", j, p["val"]); break
    if not bs:
        continue
    side, j0, level = bs
    acc = 0; entry_j = None
    for j in range(j0 + 1, min(j0 + 1 + PULLBACK_WINDOW_MIN, len(rth))):
        b = rth[j]
        if (b[4] > level if side == "long" else b[4] < level):
            acc += 1
        if (b[3] <= level if side == "long" else b[2] >= level):
            if acc >= ACCEPT_MIN:
                entry_j = j
            break
    if entry_j is None:
        continue
    date = datetime.fromtimestamp(rth_open, tz=timezone.utc).strftime("%m-%d %a")
    trades.append({"date": date, "side": side, "level": level, "j": entry_j, "rth": rth,
                   "up": sorted(x for x in [hilo(bars_between(prev, prev + 86400))[0], hilo(bars_between(sess, rth_open))[0]] if x),
                   "dn": sorted((x for x in [hilo(bars_between(prev, prev + 86400))[1], hilo(bars_between(sess, rth_open))[1]] if x), reverse=True)})


def walk(t, model):
    side, entry, rth, j = t["side"], t["level"], t["rth"], t["j"]
    stop = entry - STOP_PTS if side == "long" else entry + STOP_PTS
    tp1, tp2 = entry + 12 * (1 if side == "long" else -1), entry + 18 * (1 if side == "long" else -1)
    if model == "A":  # next known level
        levels = [x for x in t["up"] if x > entry + 2] if side == "long" else [x for x in t["dn"] if x < entry - 2]
        tgt = levels[0] if levels else entry + 12 * (1 if side == "long" else -1)
    half_done = False
    r = 0.0
    for k in range(j + 1, len(rth)):
        hi, lo = rth[k][2], rth[k][3]
        if side == "long":
            if lo <= stop:
                r += (2 if half_done else 1) * ((stop - entry) / STOP_PTS)
                return r
            if model == "C" and not half_done and hi >= tp1:
                r += 0.5 * 2; half_done = True; stop = entry  # BE
            if model == "C" and half_done and hi >= tp2:
                return r + 0.5 * 3
            if model == "B" and hi >= tp1:
                return 2.0
            if model == "A" and hi >= tgt:
                return (tgt - entry) / STOP_PTS
        else:
            if hi >= stop:
                r += (2 if half_done else 1) * ((entry - stop) / STOP_PTS)
                return r
            if model == "C" and not half_done and lo <= tp1:
                r += 0.5 * 2; half_done = True; stop = entry
            if model == "C" and half_done and lo <= tp2:
                return r + 0.5 * 3
            if model == "B" and lo <= tp1:
                return 2.0
            if model == "A" and lo <= tgt:
                return (entry - tgt) / STOP_PTS
    # timeout: mark to last close
    last = rth[-1][4]
    move = (last - entry) / STOP_PTS if side == "long" else (entry - last) / STOP_PTS
    if model == "C" and half_done:
        return 0.5 * 2 + 0.5 * max(0.0, move)  # first half banked, runner at BE-or-better
    return move


print(f"acceptance-filtered trades (A={ACCEPT_MIN}): {len(trades)}\n")
print(f"{'date':<10}{'side':<6}{'A:next-lvl':>11}{'B:2R':>7}{'C:scaled':>10}")
tot = {"A": 0.0, "B": 0.0, "C": 0.0}
wins = {"A": 0, "B": 0, "C": 0}
for t in trades:
    rs_ = {m: walk(t, m) for m in ("A", "B", "C")}
    for m in rs_:
        tot[m] += rs_[m]
        if rs_[m] > 0:
            wins[m] += 1
    print(f"{t['date']:<10}{t['side']:<6}{rs_['A']:>+11.2f}{rs_['B']:>+7.2f}{rs_['C']:>+10.2f}")
n = len(trades)
print(f"\n{'TOTAL R':<16}{tot['A']:>+11.2f}{tot['B']:>+7.2f}{tot['C']:>+10.2f}")
print(f"{'win rate':<16}{wins['A']/n:>11.0%}{wins['B']/n:>7.0%}{wins['C']/n:>10.0%}")
print(f"{'expectancy R':<16}{tot['A']/n:>+11.2f}{tot['B']/n:>+7.2f}{tot['C']/n:>+10.2f}")
print("\n(R = múltiplos de riesgo; stop 1R = 6 pts. Modelo C = mitad a 2R + stop a BE + runner a 3R.)")
