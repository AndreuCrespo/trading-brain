"""Reasoned walkthrough backtest over MESU26 .scid sessions.

For each Globex session with RTH data:
  1. Reconstruct what was knowable at RTH open (previous-session value area,
     pHOD/pLOD, overnight range, weekly VWAP) -- no lookahead.
  2. Mechanize the draft BPB setup (K#3): break of the previous value-area
     edge after RTH open, then pullback to the broken level -> entry.
     Stop fixed at 6 pts. Target = nearest known level beyond entry.
     Skip if RR < 1.5 (draft asks RR>=3 to the next level; we record both).
  3. Walk 1-minute bars to the outcome (stop-first on ambiguous bars).
Outputs one JSON object per session plus a summary.
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
PULLBACK_WINDOW_MIN = 120   # minutes after break to wait for pullback
ENTRY_NO_EARLIER_MIN = 30   # hypothesis: skip entries during opening chop
LOOKBACK_DAYS = 24

SC_OFFSET = (scid_reader.SC_EPOCH - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds()
S2200 = 22 * 3600
RTH_OPEN_S = 13 * 3600 + 30 * 60   # seconds from midnight UTC
RTH_CLOSE_S = 20 * 3600

now = time.time()
cutoff_unix = now - LOOKBACK_DAYS * 86400
cutoff_scdt = int((cutoff_unix - SC_OFFSET) * 1e6)


def session_start(unix: float) -> int:
    return int((unix - S2200) // 86400) * 86400 + S2200


def week_anchor(sess: int) -> int:
    d = datetime.fromtimestamp(sess, tz=timezone.utc)
    days_since_sunday = (d.weekday() + 1) % 7
    return sess - days_since_sunday * 86400


# ---- stream the file, aggregate to 1-min bars + per-session price ladders ----
minute_bars: dict[int, list] = {}          # minute_ts -> [o,h,l,c,vol,bid,ask]
vol_by_price: dict[int, defaultdict] = {}  # session -> price -> vol
sess_delta: dict[int, int] = defaultdict(int)

with open(PATH, "rb") as f:
    f.seek(scid_reader.HEADER_SIZE)
    rs = scid_reader.RECORD_SIZE
    fmt = scid_reader.RECORD_STRUCT
    n_used = 0
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
            bid, ask = rec[7], rec[8]
            m = int(unix // 60) * 60
            b = minute_bars.get(m)
            if b is None:
                minute_bars[m] = [close, close, close, close, vol, bid, ask]
            else:
                if close > b[1]:
                    b[1] = close
                if close < b[2]:
                    b[2] = close
                b[3] = close
                b[4] += vol
                b[5] += bid
                b[6] += ask
            sess = session_start(unix)
            ladder = vol_by_price.get(sess)
            if ladder is None:
                ladder = vol_by_price[sess] = defaultdict(int)
            ladder[round(close / TICK) * TICK] += vol
            sess_delta[sess] += ask - bid
            n_used += 1

print(f"# records used: {n_used}, minute bars: {len(minute_bars)}", file=sys.stderr)

minutes_sorted = sorted(minute_bars)
sessions = sorted(vol_by_price)


def profile(sess: int) -> dict:
    ladder = vol_by_price[sess]
    if not ladder:
        return {}
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
    return {"poc": poc, "vah": max(sel), "val": min(sel), "volume": total}


def bars_between(t0: float, t1: float) -> list[tuple]:
    return [(m, *minute_bars[m]) for m in minutes_sorted if t0 <= m < t1]


def hilo(bars: list) -> tuple:
    if not bars:
        return None, None
    return max(b[2] for b in bars), min(b[3] for b in bars)


def vwap_of(bars: list):
    v = sum(b[5] for b in bars)
    if v <= 0:
        return None
    return sum(b[4] * b[5] for b in bars) / v


results = []
for i, sess in enumerate(sessions):
    if i == 0:
        continue
    prev = sessions[i - 1]
    if prev < sess - 4 * 86400:   # gap too big (data hole)
        continue
    sess_dt = datetime.fromtimestamp(sess, tz=timezone.utc)
    rth_open_t = session_start(sess) // 86400 * 86400 + 86400 + RTH_OPEN_S  # next UTC day 13:30
    # session starting 22:00 day D -> RTH is day D+1 13:30-20:00
    rth_close_t = rth_open_t - RTH_OPEN_S + RTH_CLOSE_S

    rth = bars_between(rth_open_t, rth_close_t)
    if len(rth) < 60:
        continue

    prev_bars = bars_between(prev, prev + 86400)
    prev_hod, prev_lod = hilo(prev_bars)
    on_bars = bars_between(sess, rth_open_t)
    onh, onl = hilo(on_bars)
    p = profile(prev)
    if not p or onh is None:
        continue
    wk = week_anchor(sess)
    wvwap = vwap_of(bars_between(wk, rth_open_t))

    open_px = rth[0][1]
    loc = "inside_pva"
    if open_px > p["vah"]:
        loc = "above_pva"
    elif open_px < p["val"]:
        loc = "below_pva"

    up_levels = sorted(x for x in [prev_hod, onh, (wvwap or 0)] if x)
    dn_levels = sorted((x for x in [prev_lod, onl, (wvwap or 1e12)] if x), reverse=True)

    trade = None
    break_side = None
    # find first break of pVAH (close above) or pVAL (close below) after open
    for j, b in enumerate(rth):
        if b[4] > p["vah"] + TICK and open_px <= p["vah"]:
            break_side = ("long", j, p["vah"])
            break
        if b[4] < p["val"] - TICK and open_px >= p["val"]:
            break_side = ("short", j, p["val"])
            break

    if break_side:
        side, j0, level = break_side
        # pullback: later bar touches the broken level
        for j in range(j0 + 1, min(j0 + 1 + PULLBACK_WINDOW_MIN, len(rth))):
            b = rth[j]
            touched = b[3] <= level if side == "long" else b[2] >= level
            if not touched:
                continue
            if b[0] < rth_open_t + ENTRY_NO_EARLIER_MIN * 60:
                continue
            entry = level
            entry_t = b[0]
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
            for k in range(j + 1, len(rth)):
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
            trade = {
                "side": side, "entry": entry, "stop": stop,
                "target": round(target, 2), "rr_available": round(rr, 2),
                "taken": rr >= RR_MIN,
                "entry_time": datetime.fromtimestamp(entry_t, tz=timezone.utc).strftime("%H:%M"),
                "break_time": datetime.fromtimestamp(rth[j0][0], tz=timezone.utc).strftime("%H:%M"),
                "outcome": outcome, "points": round(pts, 2), "mfe": round(mfe, 2),
            }
            break
        if trade is None:
            trade = {"side": side, "note": "break sin pullback al nivel (se fue sin nosotros)",
                     "break_time": datetime.fromtimestamp(rth[j0][0], tz=timezone.utc).strftime("%H:%M")}

    rth_h, rth_l = hilo(rth)
    results.append({
        "session_rth_date": datetime.fromtimestamp(rth_open_t, tz=timezone.utc).strftime("%Y-%m-%d %a"),
        "open": open_px, "close": rth[-1][4],
        "open_location": loc,
        "prev": {"pVAH": p["vah"], "pVAL": p["val"], "pPOC": p["poc"],
                 "pHOD": prev_hod, "pLOD": prev_lod},
        "on": {"high": onh, "low": onl, "range": round(onh - onl, 2)},
        "wvwap_at_open": round(wvwap, 2) if wvwap else None,
        "rth_range": round(rth_h - rth_l, 2),
        "session_delta": sess_delta[sess],
        "trade": trade,
    })

print(json.dumps(results, indent=1))

taken = [r["trade"] for r in results if r["trade"] and r["trade"].get("taken")]
skipped_rr = [r["trade"] for r in results if r["trade"] and r["trade"].get("taken") is False]
no_pullback = [r for r in results if r["trade"] and "note" in r["trade"]]
no_setup = [r for r in results if not r["trade"]]
wins = [t for t in taken if t["points"] > 0]
print("## SUMMARY", file=sys.stderr)
print(f"sessions analyzed: {len(results)}", file=sys.stderr)
print(f"setup triggered+taken: {len(taken)} | wins: {len(wins)} | total pts: {round(sum(t['points'] for t in taken),2)}", file=sys.stderr)
print(f"filtered by RR<{RR_MIN}: {len(skipped_rr)} (would have made {round(sum(t['points'] for t in skipped_rr),2)} pts)", file=sys.stderr)
print(f"break without pullback: {len(no_pullback)} | no setup at all: {len(no_setup)}", file=sys.stderr)
