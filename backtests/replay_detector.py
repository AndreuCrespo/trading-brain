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

    # MISMOS helpers que el detector en vivo (paridad por construcción, obs #64):
    # barras de volumen RTH -> HA -> giros -> evaluación del checklist.
    vbars = scid_reader.aggregate_to_volume_bars(rth_recs, VOL_BAR)
    ha = indicator_engine.heikin_ashi_bars(vbars)
    giros = indicator_engine.giro_bars(ha, MIN_PRIOR_STREAK)
    week_window = indicator_engine.build_week_window(all_recs, rth_end)

    signals = []
    zones_fired = set()  # una señal por zona (pVAH/pVAL) por sesión
    for giro in giros:
        sig = indicator_engine.evaluate_giro_setup(
            giro, vbars, ha, cur_recs, week_window, pva,
            tick_size=TICK, stop_points=stop_pts, zone_points=zone_pts, rr_min=RR_MIN,
        )
        if sig is None or sig["zone"] in zones_fired:
            continue
        zones_fired.add(sig["zone"])

        entry, stop, target, direction = sig["entry"], sig["stop"], sig["target"], sig["direction"]
        bar_ts = sig["bar_ts"]
        # camina hacia delante hasta desenlace (stop-first)
        outcome, exit_px = "timeout", rth_recs[-1].close
        for r in rth_recs:
            if r.unix_time <= bar_ts:
                continue
            if direction == 1:
                if r.close <= stop: outcome, exit_px = "stop", stop; break
                if r.close >= target: outcome, exit_px = "target", target; break
            else:
                if r.close >= stop: outcome, exit_px = "stop", stop; break
                if r.close <= target: outcome, exit_px = "target", target; break
        pts = (exit_px - entry) * direction
        signals.append({
            "time": datetime.fromtimestamp(bar_ts, tz=timezone.utc).strftime("%m-%d %H:%M"),
            "setup": sig["setup"], "side": sig["side"], "zone": sig["zone"], "dva": sig["dva_state"],
            "entry": entry, "stop": stop, "target": target,
            "rr": sig["rr"], "outcome": outcome, "r": round(pts / stop_pts, 2),
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
