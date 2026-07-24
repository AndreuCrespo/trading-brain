"""Replay barra a barra del detector de setup — validación event-driven.

Recorre las barras de volumen de cada sesión RTH EN ORDEN y evalúa el checklist
en cada CIERRE de barra (no en fotos a intervalos), que es como el giro de HA —
un evento puntual— debe capturarse. Cuando contexto + zona + giro + RR se
alinean, emite una señal y camina hacia delante hasta el desenlace
(target / stop / fin de sesión), stop-first en barras ambiguas.

Es a la vez: (1) la validación del detector v0, y (2) el primer backtest del
CHECKLIST COMPLETO (no los proxies de los backtests #12/#13/#17).

Uso:
    python backtests/replay_detector.py MESU26 14   # últimas 14 sesiones
"""
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, r"D:\inversion\sierra-mcp\src")
from sierra_mcp import indicator_engine, scid_reader

DATA = r"D:\SierraChart\Data"
TICK = 0.25
STOP_POINTS = {"MES": 6.0, "MNQ": 30.0}
ZONE_POINTS = {"MES": 3.0, "MNQ": 15.0}
RR_MIN = 2.0
VOL_BAR = 500
S2200 = 22 * 3600
# Aceptación: el giro solo cuenta si rompe una racha previa de >= N barras del
# color contrario. En chop los colores alternan cada 1-2 barras, así que esto
# descarta el parpadeo (el fenómeno de las 28 señales del 14-jul, obs #59).
MIN_PRIOR_STREAK = 3


def root_of(sym): return "MNQ" if sym.upper().startswith("MNQ") else "MES"
def session_anchor(unix): return int((unix - S2200) // 86400) * 86400 + S2200


def replay_session(all_recs, cur_start, root):
    stop_pts, zone_pts = STOP_POINTS[root], ZONE_POINTS[root]
    rth_open = indicator_engine._rth_open_for_session(cur_start)
    rth_end = rth_open + indicator_engine.RTH_DURATION_SECONDS

    prev_start = indicator_engine.previous_nonempty_session_start(all_recs, cur_start)
    prev_recs = indicator_engine.records_for_session(all_recs, prev_start) if prev_start else []
    prev_rth = indicator_engine.rth_records_for_session(prev_recs, prev_start) if prev_start else []
    pva = indicator_engine.volume_profile(prev_rth, TICK, 0.70) if prev_rth else {}
    if not pva:
        return []

    cur_recs = indicator_engine.records_for_session(all_recs, cur_start)
    rth_recs = [r for r in cur_recs if rth_open <= r.unix_time < rth_end]
    if len(rth_recs) < 200:
        return []
    # wVWAP: se computa UNA vez por sesión (a la apertura RTH) — las bandas
    # evolucionan despacio; aproximación aceptable para validar el detector.
    week_start = indicator_engine._week_start(rth_open)
    week_recs = [r for r in all_recs if week_start <= r.unix_time <= rth_open]
    wk = indicator_engine._anchored_vwap(week_recs, week_start) if week_recs else {}
    bands = wk.get("bands") or {}
    # Niveles estáticos de la sesión (pVA fijo + bandas wVWAP): el pre-check
    # de proximidad usa esto y evita computar dva_state salvo en zona.
    levels = {"pVAH": pva.get("vah"), "pVAL": pva.get("val"), "pPOC": pva.get("poc"),
              "wVWAP": wk.get("vwap"), "w+1": bands.get("plus_1"), "w-1": bands.get("minus_1"),
              "w+2": bands.get("plus_2"), "w-2": bands.get("minus_2")}
    levels = {k: v for k, v in levels.items() if v is not None}

    # Barras de volumen de la sesión RTH + HA + color
    vbars = scid_reader.aggregate_to_volume_bars(rth_recs, VOL_BAR)
    ha = indicator_engine.heikin_ashi_bars(vbars)
    # marca de tiempo de cierre de cada barra (aprox: última del bucket)
    # aggregate_to_volume_bars pone time = apertura; el cierre es la apertura de la siguiente
    closes = [datetime.fromisoformat(b["time"]).timestamp() for b in vbars]

    signals = []
    zones_fired = set()  # una señal por zona (pVAH/pVAL) por sesión
    for i in range(2, len(ha) - 1):
        if ha[i]["forming"]:
            continue
        # giro = cambio de color respecto a la barra cerrada anterior
        if ha[i]["color"] == ha[i - 1]["color"]:
            continue
        # ACEPTACIÓN: el giro debe romper una racha previa de >= N barras del
        # color contrario (no un parpadeo de chop). Cuenta barras iguales antes.
        prior_streak = 0
        for j in range(i - 1, -1, -1):
            if ha[j]["color"] == ha[i - 1]["color"]:
                prior_streak += 1
            else:
                break
        if prior_streak < MIN_PRIOR_STREAK:
            continue
        giro_dir = "alcista" if ha[i]["color"] == "verde" else "bajista"
        bar_close_ts = closes[i + 1] if i + 1 < len(closes) else closes[i]
        price = vbars[i]["close"]

        def near(lv): return lv is not None and abs(price - lv) <= zone_pts
        def next_lvl(d):
            c = [v for v in levels.values() if (v - price) * d > zone_pts]
            return min(c, key=lambda v: abs(v - price)) if c else None

        # PRE-CHECK barato: ¿el precio está en alguna zona operable? Si no, no
        # gastamos dva_state (lo caro). Setups en bordes del pVA.
        if not (near(levels.get("pVAH")) or near(levels.get("pVAL"))):
            continue

        # dva_state solo aquí: ventana trailing ~3h en el cierre de esta barra
        w0 = bar_close_ts - indicator_engine.DVA_SLOPE_WINDOW_MINUTES * 60 - 60
        dva_slice = [r for r in cur_recs if w0 <= r.unix_time <= bar_close_ts]
        if len(dva_slice) < 100:
            continue
        state = indicator_engine.dva_state(dva_slice, adr=None, latest_price=price).get("state")

        setup = side = zone = None
        direction = 0
        if state == "imbalanced_down" and giro_dir == "bajista" and near(levels.get("pVAL")):
            setup, side, zone, direction = "BPB", "short", "pVAL", -1
        elif state == "imbalanced_up" and giro_dir == "alcista" and near(levels.get("pVAH")):
            setup, side, zone, direction = "BPB", "long", "pVAH", 1
        elif state == "rotational":
            if near(levels.get("pVAH")) and giro_dir == "bajista":
                setup, side, zone, direction = "EF/RPB", "short", "pVAH", -1
            elif near(levels.get("pVAL")) and giro_dir == "alcista":
                setup, side, zone, direction = "EF/RPB", "long", "pVAL", 1
        if setup is None:
            continue
        if zone in zones_fired:   # ya operamos esta zona hoy: no re-entrar en chop
            continue

        target = next_lvl(direction)
        if target is None:
            continue
        entry = price
        stop = entry - direction * stop_pts
        rr = (target - entry) * direction / stop_pts
        if rr < RR_MIN:
            continue

        # camina hacia delante hasta desenlace (stop-first)
        outcome, exit_px = "timeout", rth_recs[-1].close
        for r in rth_recs:
            if r.unix_time <= bar_close_ts:
                continue
            if direction == 1:
                if r.close <= stop: outcome, exit_px = "stop", stop; break
                if r.close >= target: outcome, exit_px = "target", target; break
            else:
                if r.close >= stop: outcome, exit_px = "stop", stop; break
                if r.close <= target: outcome, exit_px = "target", target; break
        pts = (exit_px - entry) * direction
        zones_fired.add(zone)
        signals.append({
            "time": datetime.fromtimestamp(bar_close_ts, tz=timezone.utc).strftime("%m-%d %H:%M"),
            "setup": setup, "side": side, "zone": zone, "dva": state,
            "entry": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2),
            "rr": round(rr, 2), "outcome": outcome, "r": round(pts / stop_pts, 2),
        })
    return signals


def main(symbol, n_sessions):
    root = root_of(symbol)
    import time as _t
    since = _t.time() - (n_sessions + 9) * 86400
    recs = scid_reader.read_records_since(f"{DATA}\\{symbol}.scid", since, 40_000_000)
    if not recs:
        print("sin datos"); return
    starts = sorted({session_anchor(r.unix_time) for r in recs})[-(n_sessions + 1):]
    all_sig = []
    for cs in starts[1:]:
        sig = replay_session(recs, cs, root)
        for s in sig:
            all_sig.append(s)
            print(f"{s['time']} {s['setup']:<7} {s['side']:<5} @{s['zone']:<5} dva={s['dva']:<16} "
                  f"entry {s['entry']} stop {s['stop']} tgt {s['target']} RR{s['rr']} -> {s['outcome']:<7} {s['r']:+.2f}R")
    if all_sig:
        wins = [s for s in all_sig if s["r"] > 0]
        tot = sum(s["r"] for s in all_sig)
        print(f"\nSEÑALES: {len(all_sig)} | ganadoras: {len(wins)} ({len(wins)/len(all_sig):.0%}) | "
              f"total: {tot:+.2f}R | expectativa: {tot/len(all_sig):+.2f}R/señal")
    else:
        print("\n0 señales en el rango")


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "MESU26"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 14
    main(sym, n)
