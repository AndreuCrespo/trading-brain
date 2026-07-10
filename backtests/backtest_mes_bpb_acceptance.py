"""Acceptance experiment over the BPB proxy (see backtest_mes.py for v1).

Acceptance definition tested: after the break bar, require
  - at least ACCEPT_MIN 1-minute bars closing beyond the level, AND
  - a max extension beyond the level of at least MIN_EXT points,
BEFORE the pullback touch. A touch of the level before acceptance is met
invalidates the setup (failed break -> no trade).

Runs a small grid of (ACCEPT_MIN, MIN_EXT) configs against the same sessions
and reports, per config: trades, wins, net points, and what the blocked
naive entries would have done (saved/lost points).
"""
import json
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
RR_MIN = 1.5
PULLBACK_WINDOW_MIN = 120
LOOKBACK_DAYS = 24

SC_OFFSET = (scid_reader.SC_EPOCH - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds()
S2200 = 22 * 3600
RTH_OPEN_S = 13 * 3600 + 30 * 60
RTH_CLOSE_S = 20 * 3600

now = time.time()
cutoff_scdt = int((now - LOOKBACK_DAYS * 86400 - SC_OFFSET) * 1e6)


def session_start(unix: float) -> int:
    return int((unix - S2200) // 86400) * 86400 + S2200


minute_bars: dict[int, list] = {}
vol_by_price: dict[int, defaultdict] = {}

with open(PATH, "rb") as f:
    f.seek(scid_reader.HEADER_SIZE)
    rs = scid_reader.RECORD_SIZE
    fmt = scid_reader.RECORD_STRUCT
    while True:
        blob = f.read(rs * 2_000_000)
        if not blob:
            break
        usable = len(blob) - (len(blob) % rs)
        for rec in fmt.iter_unpack(blob[:usable]):
            sc_dt = rec[0]
            if sc_dt < cutoff_scdt:
                continue
            unix = sc_dt * 1e-6 + SC_OFFSET
            close = rec[4]
            vol = rec[6]
            m = int(unix // 60) * 60
            b = minute_bars.get(m)
            if b is None:
                minute_bars[m] = [close, close, close, close, vol]
            else:
                if close > b[1]:
                    b[1] = close
                if close < b[2]:
                    b[2] = close
                b[3] = close
                b[4] += vol
            sess = session_start(unix)
            ladder = vol_by_price.get(sess)
            if ladder is None:
                ladder = vol_by_price[sess] = defaultdict(int)
            ladder[round(close / TICK) * TICK] += vol

minutes_sorted = sorted(minute_bars)
sessions = sorted(vol_by_price)


def profile(sess: int) -> dict:
    ladder = vol_by_price[sess]
    prices = sorted(ladder)
    total = sum(ladder.values())
    poc = max(prices, key=lambda p: ladder[p])
    target = total * VA_PCT
    sel = {poc}
    sel_vol = ladder[poc]
    lo = hi = prices.index(poc)
    while sel_vol < target and (lo > 0 or hi < len(prices) - 1):
        below = ladder[prices[lo - 1]] if lo > 0 else -1
        above = ladder[prices[hi + 1]] if hi < len(prices) - 1 else -1
        if above >= below and hi < len(prices) - 1:
            hi += 1
            sel.add(prices[hi]); sel_vol += above
        elif lo > 0:
            lo -= 1
            sel.add(prices[lo]); sel_vol += below
        else:
            break
    return {"poc": poc, "vah": max(sel), "val": min(sel)}


def bars_between(t0: float, t1: float) -> list[tuple]:
    return [(m, *minute_bars[m]) for m in minutes_sorted if t0 <= m < t1]


def hilo(bars):
    return (max(b[2] for b in bars), min(b[3] for b in bars)) if bars else (None, None)


# ---- precompute per-session context once ----
contexts = []
for i, sess in enumerate(sessions):
    if i == 0:
        continue
    prev = sessions[i - 1]
    if prev < sess - 4 * 86400:
        continue
    rth_open_t = session_start(sess) // 86400 * 86400 + 86400 + RTH_OPEN_S
    rth_close_t = rth_open_t - RTH_OPEN_S + RTH_CLOSE_S
    rth = bars_between(rth_open_t, rth_close_t)
    if len(rth) < 60:
        continue
    prev_bars = bars_between(prev, prev + 86400)
    on_bars = bars_between(sess, rth_open_t)
    p = profile(prev)
    prev_hod, prev_lod = hilo(prev_bars)
    onh, onl = hilo(on_bars)
    if onh is None:
        continue
    contexts.append({
        "date": datetime.fromtimestamp(rth_open_t, tz=timezone.utc).strftime("%m-%d %a"),
        "rth": rth, "p": p,
        "up_levels": sorted(x for x in [prev_hod, onh] if x),
        "dn_levels": sorted((x for x in [prev_lod, onl] if x), reverse=True),
    })


def simulate(rth, level, side, entry_j, up_levels, dn_levels):
    """Entry at `level` on bar index entry_j; walk to outcome."""
    entry = level
    if side == "long":
        stop = entry - STOP_PTS
        above = [x for x in up_levels if x > entry + 2]
        target = above[0] if above else entry + 2 * STOP_PTS
        rr = (target - entry) / STOP_PTS
    else:
        stop = entry + STOP_PTS
        below = [x for x in dn_levels if x < entry - 2]
        target = below[0] if below else entry - 2 * STOP_PTS
        rr = (entry - target) / STOP_PTS
    outcome, exit_px, mfe = "timeout", rth[-1][4], 0.0
    for k in range(entry_j + 1, len(rth)):
        bb = rth[k]
        if side == "long":
            mfe = max(mfe, bb[2] - entry)
            if bb[3] <= stop:
                outcome, exit_px = "stop", stop
                break
            if bb[2] >= target:
                outcome, exit_px = "target", target
                break
        else:
            mfe = max(mfe, entry - bb[3])
            if bb[2] >= stop:
                outcome, exit_px = "stop", stop
                break
            if bb[3] <= target:
                outcome, exit_px = "target", target
                break
    pts = (exit_px - entry) if side == "long" else (entry - exit_px)
    return {"rr": round(rr, 2), "outcome": outcome, "points": round(pts, 2), "mfe": round(mfe, 2)}


def run(accept_min: int, min_ext: float):
    trades, blocked = [], []
    for ctx in contexts:
        rth, p = ctx["rth"], ctx["p"]
        open_px = rth[0][1]
        break_side = None
        for j, b in enumerate(rth):
            if b[4] > p["vah"] + TICK and open_px <= p["vah"]:
                break_side = ("long", j, p["vah"])
                break
            if b[4] < p["val"] - TICK and open_px >= p["val"]:
                break_side = ("short", j, p["val"])
                break
        if not break_side:
            continue
        side, j0, level = break_side

        accepted_bars = 0
        max_ext = 0.0
        accepted = False
        naive_entry_j = None   # first touch regardless of acceptance
        entry_j = None
        for j in range(j0 + 1, min(j0 + 1 + PULLBACK_WINDOW_MIN, len(rth))):
            b = rth[j]
            beyond = b[4] > level if side == "long" else b[4] < level
            if beyond:
                accepted_bars += 1
                ext = (b[2] - level) if side == "long" else (level - b[3])
                max_ext = max(max_ext, ext)
                if accepted_bars >= accept_min and max_ext >= min_ext:
                    accepted = True
            touched = b[3] <= level if side == "long" else b[2] >= level
            if touched:
                if naive_entry_j is None:
                    naive_entry_j = j
                if accepted:
                    entry_j = j
                    break
                else:
                    break   # touch before acceptance -> failed break, no trade
        if entry_j is not None:
            r = simulate(rth, level, side, entry_j, ctx["up_levels"], ctx["dn_levels"])
            if r["rr"] >= RR_MIN:
                trades.append({"date": ctx["date"], "side": side, "entry": level, **r})
        elif naive_entry_j is not None:
            r = simulate(rth, level, side, naive_entry_j, ctx["up_levels"], ctx["dn_levels"])
            if r["rr"] >= RR_MIN:
                blocked.append({"date": ctx["date"], "side": side, **r})
    return trades, blocked


print(f"sessions: {len(contexts)}")
print(f"{'config':<18}{'trades':>7}{'wins':>6}{'net':>8}   blocked(n, net-if-taken)")
for accept_min, min_ext in [(1, 0.0), (2, 0.0), (3, 0.0), (4, 0.0), (5, 0.0), (1, 4.0), (2, 4.0)]:
    trades, blocked = run(accept_min, min_ext)
    wins = sum(1 for t in trades if t["points"] > 0)
    net = round(sum(t["points"] for t in trades), 2)
    bnet = round(sum(t["points"] for t in blocked), 2)
    print(f"A={accept_min:<3} E={min_ext:<10}{len(trades):>7}{wins:>6}{net:>8}   {len(blocked)}, {bnet}")

print()
print("## detail A=1 E=0")
trades, blocked = run(1, 0.0)
for t in trades:
    print("TRADE  ", json.dumps(t))
for t in blocked:
    print("BLOCKED", json.dumps(t))
