"""Setup detector v0 — el cerebro compartido de los modos semi/auto.

Dado un símbolo y un instante T, reconstruye el contexto "tal como se conocía
en T" (sin lookahead) y evalúa el checklist mecánico de los 4 setups approved
del playbook donAdri (BPB, RPB, EF, IPB). Devuelve candidatos con entry/stop/
target/RR y qué ítems del checklist se cumplen o faltan.

IMPORTANTE — esto es v0 MECÁNICO, no discrecional: cubre filtro DVA (K#12),
proximidad a zona, gatillo de HA y RR. NO juzga "aceptación" ni "shift in
condition" de forma fina (eso es discrecional del trader / requiere etiqueta).
Por eso su salida son CANDIDATOS para revisión (modo semi), nunca una orden
automática por sí sola. El modo auto exigirá además las guardas de sesión y un
historial de acierto en semi.

Uso:
    python backtests/setup_detector.py MESU26 2026-07-16T15:40:00
"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, r"D:\inversion\sierra-mcp\src")
from sierra_mcp import indicator_engine, scid_reader

DATA = r"D:\SierraChart\Data"
TICK = 0.25
STOP_POINTS = {"MES": 6.0, "MNQ": 30.0}      # riesgo 1R por defecto (de los backtests)
ZONE_POINTS = {"MES": 3.0, "MNQ": 15.0}       # "en la zona" = a esta distancia del nivel
RR_MIN = 2.0                                   # target 2R canónico (PPT §3)
VOL_BAR = {"MES": 500, "MNQ": 500}


def root_of(symbol: str) -> str:
    return "MNQ" if symbol.upper().startswith("MNQ") else "MES"


def detect(symbol: str, as_of: float) -> dict:
    root = root_of(symbol)
    stop_pts = STOP_POINTS[root]
    zone_pts = ZONE_POINTS[root]

    resolved = symbol if symbol.upper().endswith(".SCID") else f"{symbol}.scid"
    path = f"{DATA}\\{symbol}.scid"

    # Records up to T only (no lookahead), last ~72h for context.
    records = scid_reader.read_records_since(path, as_of - 72 * 3600, 6_000_000)
    records = [r for r in records if r.unix_time <= as_of]
    if len(records) < 100:
        return {"ok": False, "error": "not enough records up to T"}

    price = records[-1].close
    cur_start = indicator_engine.globex_equity_session_start(as_of)
    prev_start = indicator_engine.previous_nonempty_session_start(records, cur_start)
    cur_recs = indicator_engine.records_for_session(records, cur_start)
    prev_recs = indicator_engine.records_for_session(records, prev_start) if prev_start else []

    # Previous-session RTH value area = pVA del sistema donAdri
    prev_rth = indicator_engine.rth_records_for_session(prev_recs, prev_start) if prev_start else []
    pva = indicator_engine.volume_profile(prev_rth, TICK, 0.70) if prev_rth else {}
    # Current developing VA (full session so far)
    cva = indicator_engine.volume_profile(cur_recs, TICK, 0.70) if cur_recs else {}

    # DVA state + wVWAP bands as of T
    dva = indicator_engine.dva_state(cur_recs, adr=None, latest_price=price)
    wk = indicator_engine._anchored_vwap(records, indicator_engine._week_start(as_of))

    # HA trigger as of T
    vbars = scid_reader.aggregate_to_volume_bars(cur_recs, VOL_BAR[root])
    ha = indicator_engine.heikin_ashi_bars(vbars)
    trig = indicator_engine.ha_trigger_state(ha)

    state = dva.get("state", "insufficient_data")
    giro = trig.get("giro", False)
    giro_dir = trig.get("direction")  # alcista / bajista / None

    # Candidate levels (salir-total edges) for zone + target math
    levels = {
        "pVAH": pva.get("vah"), "pVAL": pva.get("val"), "pPOC": pva.get("poc"),
        "CVAH": cva.get("vah"), "CVAL": cva.get("val"),
        "wVWAP": wk.get("vwap"),
        "w+1": (wk.get("bands") or {}).get("plus_1"), "w-1": (wk.get("bands") or {}).get("minus_1"),
        "w+2": (wk.get("bands") or {}).get("plus_2"), "w-2": (wk.get("bands") or {}).get("minus_2"),
    }
    levels = {k: v for k, v in levels.items() if v is not None}

    def near(level):
        return level is not None and abs(price - level) <= zone_pts

    def next_level(direction):
        """Nearest salir-total level beyond price in `direction` (1 up / -1 down)."""
        cands = [v for v in levels.values() if (v - price) * direction > zone_pts]
        if not cands:
            return None
        return min(cands, key=lambda v: abs(v - price))

    candidates = []

    def rr_ok(direction, target):
        if target is None:
            return None, False
        reward = (target - price) * direction
        rr = reward / stop_pts
        return round(rr, 2), rr >= RR_MIN

    # --- Continuation family (BPB/IPB): requires imbalanced DVA (K#12) ---
    if state == "imbalanced_down":
        # BPB bajista: precio pegado al pVAL/CVAL roto, giro bajista, target abajo
        for name in ("pVAL", "CVAL"):
            lvl = levels.get(name)
            if near(lvl):
                tgt = next_level(-1)
                rr, ok = rr_ok(-1, tgt)
                candidates.append({
                    "setup": "BPB", "side": "short", "zone": name, "zone_level": lvl,
                    "entry": price, "stop": round(price + stop_pts, 2), "target": tgt,
                    "rr": rr, "checklist": {
                        "dva_filter": "imbalanced_down (continuación OK)",
                        "zona": f"precio en {name} (±{zone_pts})",
                        "giro_HA": "bajista OK" if giro and giro_dir == "bajista" else f"FALTA (giro={giro_dir})",
                        "rr>=2": ok if rr is not None else "sin nivel target",
                        "aceptacion/shift": "NO EVALUADO (discrecional)",
                    },
                    "operable": bool(giro and giro_dir == "bajista" and ok),
                })
    elif state == "imbalanced_up":
        for name in ("pVAH", "CVAH"):
            lvl = levels.get(name)
            if near(lvl):
                tgt = next_level(1)
                rr, ok = rr_ok(1, tgt)
                candidates.append({
                    "setup": "BPB", "side": "long", "zone": name, "zone_level": lvl,
                    "entry": price, "stop": round(price - stop_pts, 2), "target": tgt,
                    "rr": rr, "checklist": {
                        "dva_filter": "imbalanced_up (continuación OK)",
                        "zona": f"precio en {name} (±{zone_pts})",
                        "giro_HA": "alcista OK" if giro and giro_dir == "alcista" else f"FALTA (giro={giro_dir})",
                        "rr>=2": ok if rr is not None else "sin nivel target",
                        "aceptacion/shift": "NO EVALUADO (discrecional)",
                    },
                    "operable": bool(giro and giro_dir == "alcista" and ok),
                })

    # --- Reversion family (RPB/EF): requires rotational DVA (K#12) ---
    if state == "rotational":
        # EF/RPB en extremos del valor: fade hacia el otro lado
        for name, side, direction in (("CVAH", "short", -1), ("pVAH", "short", -1),
                                       ("CVAL", "long", 1), ("pVAL", "long", 1)):
            lvl = levels.get(name)
            if near(lvl):
                want = "bajista" if side == "short" else "alcista"
                tgt = levels.get("pPOC") or levels.get("wVWAP")
                rr, ok = rr_ok(direction, tgt)
                candidates.append({
                    "setup": "EF/RPB", "side": side, "zone": name, "zone_level": lvl,
                    "entry": price, "stop": round(price - direction * stop_pts, 2), "target": tgt,
                    "rr": rr, "checklist": {
                        "dva_filter": "rotational (fade/reversión OK)",
                        "zona": f"precio en {name} (±{zone_pts})",
                        "giro_HA": f"{want} OK" if giro and giro_dir == want else f"FALTA (giro={giro_dir})",
                        "rr>=2": ok if rr is not None else "sin nivel target",
                        "aceptacion/shift": "NO EVALUADO (discrecional)",
                    },
                    "operable": bool(giro and giro_dir == want and ok),
                })

    return {
        "ok": True,
        "symbol": symbol,
        "as_of": datetime.fromtimestamp(as_of, tz=timezone.utc).isoformat(),
        "price": price,
        "dva_state": state,
        "dva_criterion": dva.get("criterion"),
        "giro_HA": {"giro": giro, "direction": giro_dir, "last_closed": trig.get("last_closed_time")},
        "pva_rth": {k: pva.get(k) for k in ("poc", "vah", "val")},
        "levels": {k: round(v, 2) for k, v in levels.items()},
        "candidates": candidates,
        "verdict": (
            "SIN SETUP (fuera de zona o sin gatillo)" if not candidates
            else "CANDIDATO(S) — revisar checklist"
        ),
    }


if __name__ == "__main__":
    import json
    sym = sys.argv[1] if len(sys.argv) > 1 else "MESU26"
    ts = sys.argv[2] if len(sys.argv) > 2 else "2026-07-16T15:40:00"
    as_of = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).timestamp()
    print(json.dumps(detect(sym, as_of), indent=1, ensure_ascii=False))
