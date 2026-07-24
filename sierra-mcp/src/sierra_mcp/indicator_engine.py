"""Indicator calculations over Sierra Chart SCID tick records.

This module is the structured indicator engine for the trading brain. It
calculates levels from local tick records rather than reading visual studies
from a Sierra Chart window.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Iterable

from sierra_mcp import scid_reader


GLOBEX_SESSION_START_UTC = dt_time(22, 0, tzinfo=timezone.utc)
RTH_OPEN_UTC = dt_time(13, 30, tzinfo=timezone.utc)
INITIAL_BALANCE_MINUTES = 60


def datetime_from_unix(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def round_to_tick(price: float, tick_size: float) -> float:
    return round(round(price / tick_size) * tick_size, 10)


def distance(price: float, level: float | None) -> float | None:
    return round(price - level, 4) if level is not None else None


def globex_equity_session_start(last_tick_unix: float) -> float:
    """Approximate CME equity index futures session start in UTC.

    ES/NQ/MES/MNQ trade nearly 24h with the main daily Globex session starting
    at 17:00 Chicago. During US daylight saving time this is 22:00 UTC, which
    matches the current project setup. This can be parameterized later if we
    need exact DST handling.
    """
    dt = datetime.fromtimestamp(last_tick_unix, tz=timezone.utc)
    session_date = dt.date()
    if dt.timetz() < GLOBEX_SESSION_START_UTC:
        session_date = session_date - timedelta(days=1)
    session_start = datetime.combine(session_date, GLOBEX_SESSION_START_UTC)
    return session_start.timestamp()


def records_for_session(records: list[scid_reader.TickRecord], session_start: float) -> list[scid_reader.TickRecord]:
    next_start = session_start + 24 * 60 * 60
    return [r for r in records if session_start <= r.unix_time < next_start]


def previous_nonempty_session_start(records: list[scid_reader.TickRecord], current_start: float) -> float | None:
    starts = sorted(
        {
            globex_equity_session_start(r.unix_time)
            for r in records
            if r.unix_time < current_start
        },
        reverse=True,
    )
    return starts[0] if starts else None


def position_vs_value(price: float, profile: dict) -> str:
    val = profile.get("val")
    vah = profile.get("vah")
    if val is None or vah is None:
        return "unknown"
    if price > vah:
        return "above_value"
    if price < val:
        return "below_value"
    return "inside_value"


def volume_profile(
    records: list[scid_reader.TickRecord],
    tick_size: float,
    value_area_percent: float,
) -> dict:
    if not records:
        return {}

    volume_by_price: defaultdict[float, int] = defaultdict(int)
    bid_volume_by_price: defaultdict[float, int] = defaultdict(int)
    ask_volume_by_price: defaultdict[float, int] = defaultdict(int)
    for r in records:
        price = round_to_tick(float(r.close), tick_size)
        volume_by_price[price] += int(r.volume)
        bid_volume_by_price[price] += int(r.bid_volume)
        ask_volume_by_price[price] += int(r.ask_volume)

    if not volume_by_price:
        return {}

    prices = sorted(volume_by_price)
    total_volume = sum(volume_by_price.values())
    poc = max(prices, key=lambda p: (volume_by_price[p], -abs(p - records[-1].close)))
    target_volume = total_volume * value_area_percent

    selected = {poc}
    selected_volume = volume_by_price[poc]
    poc_index = prices.index(poc)
    low_index = poc_index
    high_index = poc_index

    while selected_volume < target_volume and (low_index > 0 or high_index < len(prices) - 1):
        below_price = prices[low_index - 1] if low_index > 0 else None
        above_price = prices[high_index + 1] if high_index < len(prices) - 1 else None
        below_volume = volume_by_price[below_price] if below_price is not None else -1
        above_volume = volume_by_price[above_price] if above_price is not None else -1

        if above_volume >= below_volume and above_price is not None:
            high_index += 1
            selected.add(above_price)
            selected_volume += above_volume
        elif below_price is not None:
            low_index -= 1
            selected.add(below_price)
            selected_volume += below_volume
        else:
            break

    high_volume_nodes = sorted(
        (
            {
                "price": p,
                "volume": volume_by_price[p],
                "bid_volume": bid_volume_by_price[p],
                "ask_volume": ask_volume_by_price[p],
                "delta": ask_volume_by_price[p] - bid_volume_by_price[p],
            }
            for p in prices
        ),
        key=lambda row: row["volume"],
        reverse=True,
    )[:10]

    return {
        "tick_size": tick_size,
        "value_area_percent": value_area_percent,
        "poc": poc,
        "vah": max(selected),
        "val": min(selected),
        "value_area_volume": selected_volume,
        "total_volume": total_volume,
        "value_area_actual_percent": round(selected_volume / total_volume * 100, 2) if total_volume else None,
        "high_volume_nodes": high_volume_nodes,
    }


def profile_context(records: list[scid_reader.TickRecord], tick_size: float, value_area_percent: float) -> dict:
    stats = scid_reader.aggregate_session_stats(records)
    profile = volume_profile(records, tick_size, value_area_percent)
    if not stats:
        return {}
    return {
        **stats,
        "volume_profile": profile,
    }


def _records_between(
    records: list[scid_reader.TickRecord],
    start_ts: float,
    end_ts: float,
) -> list[scid_reader.TickRecord]:
    return [r for r in records if start_ts <= r.unix_time < end_ts]


def _range_levels(records: list[scid_reader.TickRecord]) -> dict:
    if not records:
        return {}
    return {
        "start_time": datetime_from_unix(records[0].unix_time),
        "end_time": datetime_from_unix(records[-1].unix_time),
        "high": max(r.close for r in records),
        "low": min(r.close for r in records),
        "range": max(r.close for r in records) - min(r.close for r in records),
        "volume": sum(r.volume for r in records),
        "delta": sum(r.ask_volume for r in records) - sum(r.bid_volume for r in records),
    }


def _rth_open_for_session(session_start: float) -> float:
    session_dt = datetime.fromtimestamp(session_start, tz=timezone.utc)
    rth_date = (session_dt + timedelta(days=1)).date()
    return datetime.combine(rth_date, RTH_OPEN_UTC).timestamp()


def _session_ranges(records: list[scid_reader.TickRecord], current_start: float, limit: int) -> list[dict]:
    starts = sorted(
        {
            globex_equity_session_start(r.unix_time)
            for r in records
            if globex_equity_session_start(r.unix_time) < current_start
        },
        reverse=True,
    )

    ranges: list[dict] = []
    for start in starts[:limit]:
        session_records = records_for_session(records, start)
        if not session_records:
            continue
        levels = _range_levels(session_records)
        ranges.append({
            "session_start": datetime_from_unix(start),
            "high": levels["high"],
            "low": levels["low"],
            "range": levels["range"],
        })
    return ranges


def _average(values: Iterable[float]) -> float | None:
    values = list(values)
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _anchored_vwap(records: list[scid_reader.TickRecord], start_ts: float) -> dict:
    anchored = [r for r in records if r.unix_time >= start_ts]
    if not anchored:
        return {}

    volume = sum(r.volume for r in anchored)
    if volume <= 0:
        return {}

    vwap = sum(r.close * r.volume for r in anchored) / volume
    variance = sum(r.volume * ((r.close - vwap) ** 2) for r in anchored) / volume
    std_dev = math.sqrt(variance)

    return {
        "start_time": datetime_fromtimestamp_safe(start_ts),
        "end_time": datetime_from_unix(anchored[-1].unix_time),
        "vwap": vwap,
        "volume": volume,
        "std_dev": std_dev,
        "bands": {
            "plus_1": vwap + std_dev,
            "minus_1": vwap - std_dev,
            "plus_2": vwap + 2 * std_dev,
            "minus_2": vwap - 2 * std_dev,
        },
    }


def datetime_fromtimestamp_safe(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _week_start(last_tick_unix: float) -> float:
    dt = datetime.fromtimestamp(last_tick_unix, tz=timezone.utc)
    days_since_sunday = (dt.weekday() + 1) % 7
    sunday = (dt - timedelta(days=days_since_sunday)).date()
    start = datetime.combine(sunday, GLOBEX_SESSION_START_UTC)
    if dt.timestamp() < start.timestamp():
        start -= timedelta(days=7)
    return start.timestamp()


def _month_start(last_tick_unix: float) -> float:
    dt = datetime.fromtimestamp(last_tick_unix, tz=timezone.utc)
    start = datetime(dt.year, dt.month, 1, tzinfo=timezone.utc)
    return start.timestamp()


def derived_levels(
    records: list[scid_reader.TickRecord],
    current_start: float,
    previous_records: list[scid_reader.TickRecord],
    adr_sessions: int = 5,
) -> dict:
    """Calculate secondary playbook levels over loaded SCID records."""
    if not records:
        return {}

    rth_open = _rth_open_for_session(current_start)
    ib_end = rth_open + INITIAL_BALANCE_MINUTES * 60

    overnight_records = _records_between(records, current_start, rth_open)
    initial_balance_records = _records_between(records, rth_open, ib_end)
    session_ranges = _session_ranges(records, current_start, adr_sessions)
    adr = _average(row["range"] for row in session_ranges)
    latest_price = records[-1].close

    out = {
        "overnight": _range_levels(overnight_records),
        "initial_balance": _range_levels(initial_balance_records),
        "previous_day": _range_levels(previous_records),
        "adr": {
            "sessions": len(session_ranges),
            "period": adr_sessions,
            "value": adr,
            "sample": session_ranges,
        },
        "anchored_vwaps": {
            "weekly": _anchored_vwap(records, _week_start(records[-1].unix_time)),
            "monthly": _anchored_vwap(records, _month_start(records[-1].unix_time)),
        },
        "distances": {},
        "calculation_notes": [
            "Globex session model uses fixed 22:00 UTC starts.",
            "RTH open/IB use fixed 13:30 UTC and 60-minute IB for CME equity futures.",
            "Weekly VWAP starts Sunday 22:00 UTC; monthly VWAP starts calendar month 00:00 UTC.",
            "All levels are calculated from loaded SCID tick records, not Sierra visual studies.",
        ],
    }

    level_map = {
        "onh": out["overnight"].get("high"),
        "onl": out["overnight"].get("low"),
        "ibh": out["initial_balance"].get("high"),
        "ibl": out["initial_balance"].get("low"),
        "phod": out["previous_day"].get("high"),
        "plod": out["previous_day"].get("low"),
        "weekly_vwap": out["anchored_vwaps"]["weekly"].get("vwap"),
        "monthly_vwap": out["anchored_vwaps"]["monthly"].get("vwap"),
    }
    out["distances"] = {
        name: distance(float(latest_price), level)
        for name, level in level_map.items()
    }
    return out


RTH_DURATION_SECONDS = int(6.5 * 3600)  # CME equity RTH 13:30-20:00 UTC


def rth_records_for_session(
    records: list[scid_reader.TickRecord],
    session_start: float,
) -> list[scid_reader.TickRecord]:
    """Slice a session's records down to the RTH window.

    donAdri's TPO/value-area system builds profiles on the RTH session only
    (PPT §3); the full-Globex profile skews the VAL downward with overnight
    volume (calibration 2026-07-13: 10-point pVAL gap vs his chart).
    """
    rth_open = _rth_open_for_session(session_start)
    return _records_between(records, rth_open, rth_open + RTH_DURATION_SECONDS)


def heikin_ashi_bars(bars: list[dict]) -> list[dict]:
    """Transform OHLC bars into Heikin Ashi bars with color.

    donAdri's entry trigger is the "giro de HA" on the trigger chart
    (volume bars): the HA candle color flipping against the previous one.
    """
    ha: list[dict] = []
    for b in bars:
        o = float(b["open"])
        h = float(b["high"])
        low = float(b["low"])
        c = float(b["close"])
        ha_close = (o + h + low + c) / 4.0
        ha_open = (o + c) / 2.0 if not ha else (ha[-1]["ha_open"] + ha[-1]["ha_close"]) / 2.0
        ha.append({
            "time": b.get("time"),
            "ha_open": round(ha_open, 4),
            "ha_high": round(max(h, ha_open, ha_close), 4),
            "ha_low": round(min(low, ha_open, ha_close), 4),
            "ha_close": round(ha_close, 4),
            "color": "verde" if ha_close >= ha_open else "roja",
            "volume": b.get("volume"),
            "delta": int(b.get("ask_volume", 0)) - int(b.get("bid_volume", 0)),
            "forming": bool(b.get("forming", False)),
        })
    return ha


def ha_trigger_state(ha_bars: list[dict]) -> dict:
    """Evaluate the giro-de-HA state on the most recent CLOSED bar.

    The last bar is usually forming; a giro only counts when a closed bar
    flips color versus the closed bar before it. The forming bar's color is
    reported separately as an early (revocable) signal.
    """
    closed = [b for b in ha_bars if not b["forming"]]
    if len(closed) < 2:
        return {"giro": False, "reason": "insufficient closed bars"}
    last, prev = closed[-1], closed[-2]
    giro = last["color"] != prev["color"]
    out = {
        "giro": giro,
        "direction": ("alcista" if last["color"] == "verde" else "bajista") if giro else None,
        "last_closed_color": last["color"],
        "prev_closed_color": prev["color"],
        "last_closed_time": last["time"],
        "streak": 0,
    }
    streak = 1
    for b in reversed(closed[:-1]):
        if b["color"] == last["color"]:
            streak += 1
        else:
            break
    out["streak"] = streak
    forming = next((b for b in reversed(ha_bars) if b["forming"]), None)
    if forming is not None:
        out["forming_bar_color"] = forming["color"]
        out["forming_bar_flips"] = forming["color"] != last["color"]
    return out


def detect_setup(
    records: list[scid_reader.TickRecord],
    *,
    tick_size: float,
    stop_points: float,
    zone_points: float,
    vol_bar: int = 500,
    min_prior_streak: int = 3,
    rr_min: float = 2.0,
    lookback_bars: int = 6,
    value_area_percent: float = 0.70,
) -> dict:
    """Live setup detector — el cerebro compartido de los modos semi/auto.

    Reconstruye el contexto en el instante actual (dva_state + pVA RTH + bandas
    wVWAP + barras HA) y busca la señal VÁLIDA más reciente en las últimas
    `lookback_bars` barras de volumen CERRADAS. Aplica el checklist mecánico:
    filtro K#12 (imbalanced->BPB continuación / rotational->EF-RPB reversión),
    proximidad a borde del pVA, giro de HA que rompe una racha previa
    >= min_prior_streak (aceptación anti-chop), y RR >= rr_min al siguiente
    nivel. Escanear varias barras evita perder el giro cuando se consulta a
    intervalos (el giro es un evento puntual — obs #58/#59).

    NO evalúa "shift in condition" discrecional: su salida es un CANDIDATO para
    confirmación humana (modo semi), no una orden.

    ⚠ ESTADO (2026-07-24): WIP — NO reproduce todavía las señales del replay
    validado (backtests/replay_detector.py). En un test de paridad sobre el
    15-jul (donde el replay halló un BPB short @pVAL a las 16:31) esta función
    NO dispara. Falla hacia "sin señal" (conservador: nunca propone de más),
    pero NO usar para decisiones semi reales hasta reconciliar la paridad
    barra-a-barra con el replay. Pendiente: comparar las series de barras de
    volumen/HA de ambos caminos en un mismo instante.
    """
    if not records:
        return {"signal": False, "reason": "no records"}
    now_price = float(records[-1].close)
    now_ts = records[-1].unix_time
    cur_start = globex_equity_session_start(now_ts)
    prev_start = previous_nonempty_session_start(records, cur_start)
    if prev_start is None:
        return {"signal": False, "reason": "no previous session for pVA"}
    cur_recs = records_for_session(records, cur_start)
    prev_recs = records_for_session(records, prev_start)
    prev_rth = rth_records_for_session(prev_recs, prev_start)
    pva = volume_profile(prev_rth, tick_size, value_area_percent) if prev_rth else {}
    if not pva:
        return {"signal": False, "reason": "no previous RTH value area"}

    wk = _anchored_vwap(records, _week_start(now_ts))
    bands = wk.get("bands") or {}
    levels = {"pVAH": pva.get("vah"), "pVAL": pva.get("val"), "pPOC": pva.get("poc"),
              "wVWAP": wk.get("vwap"), "w+1": bands.get("plus_1"), "w-1": bands.get("minus_1"),
              "w+2": bands.get("plus_2"), "w-2": bands.get("minus_2")}
    levels = {k: v for k, v in levels.items() if v is not None}

    # Barras de volumen SOLO con ticks RTH (reset en la apertura RTH), igual que
    # el replay validado — construirlas sobre la sesión Globex completa cambia
    # los límites de barra y descuadra los giros (paridad, obs #60).
    rth_open = _rth_open_for_session(cur_start)
    rth_recs = [r for r in cur_recs if r.unix_time >= rth_open]
    if len(rth_recs) < 100:
        return {"signal": False, "reason": "RTH not open yet or too few RTH ticks", "levels": levels}
    vbars = scid_reader.aggregate_to_volume_bars(rth_recs, vol_bar)
    ha = heikin_ashi_bars(vbars)
    closed_idx = [i for i, b in enumerate(ha) if not b["forming"]]
    if len(closed_idx) < min_prior_streak + 1:
        return {"signal": False, "reason": "not enough closed bars", "levels": levels}

    def near(lv):
        return lv is not None and abs(now_price - lv) <= zone_points

    # Escanea de la barra cerrada más reciente hacia atrás (hasta lookback_bars)
    for i in reversed(closed_idx[-lookback_bars:]):
        if i == 0 or ha[i]["color"] == ha[i - 1]["color"]:
            continue  # no es barra de giro
        prior = 0
        for j in range(i - 1, -1, -1):
            if ha[j]["color"] == ha[i - 1]["color"]:
                prior += 1
            else:
                break
        if prior < min_prior_streak:
            continue  # aceptación insuficiente (parpadeo de chop)
        giro_dir = "alcista" if ha[i]["color"] == "verde" else "bajista"
        bar_price = float(vbars[i]["close"])
        bar_ts = _bar_close_ts(vbars, i)

        def zone_hit(lv):
            return lv is not None and abs(bar_price - lv) <= zone_points

        w0 = bar_ts - DVA_SLOPE_WINDOW_MINUTES * 60 - 60
        dva_slice = [r for r in cur_recs if w0 <= r.unix_time <= bar_ts]
        if len(dva_slice) < 100:
            continue
        state = dva_state(dva_slice, adr=None, latest_price=bar_price).get("state")

        setup = side = zone = None
        direction = 0
        if state == "imbalanced_down" and giro_dir == "bajista" and zone_hit(levels.get("pVAL")):
            setup, side, zone, direction = "BPB", "short", "pVAL", -1
        elif state == "imbalanced_up" and giro_dir == "alcista" and zone_hit(levels.get("pVAH")):
            setup, side, zone, direction = "BPB", "long", "pVAH", 1
        elif state == "rotational":
            if zone_hit(levels.get("pVAH")) and giro_dir == "bajista":
                setup, side, zone, direction = "EF/RPB", "short", "pVAH", -1
            elif zone_hit(levels.get("pVAL")) and giro_dir == "alcista":
                setup, side, zone, direction = "EF/RPB", "long", "pVAL", 1
        if setup is None:
            continue

        cands = [v for v in levels.values() if (v - bar_price) * direction > zone_points]
        if not cands:
            continue
        target = min(cands, key=lambda v: abs(v - bar_price))
        entry = levels[zone]
        stop = entry - direction * stop_points
        rr = (target - entry) * direction / stop_points
        if rr < rr_min:
            continue

        bars_ago = closed_idx[-1] - i
        return {
            "signal": True,
            "setup": setup, "side": side, "zone": zone,
            "dva_state": state, "giro": giro_dir, "prior_streak": prior,
            "entry": round(entry, 2), "stop": round(stop, 2),
            "target": round(target, 2), "rr": round(rr, 2),
            "bar_time": datetime.fromtimestamp(bar_ts, tz=timezone.utc).isoformat(),
            "bars_ago": bars_ago,
            "current_price": now_price,
            "still_near_zone": near(levels.get(zone)),
            "levels": {k: round(v, 2) for k, v in levels.items()},
            "checklist": {
                "dva_filter": state,
                "zona": f"{zone} (borde pVA)",
                "aceptacion": f"racha previa {prior} barras (>= {min_prior_streak})",
                "giro_HA": f"{giro_dir} OK",
                "rr>=min": True,
                "shift_in_condition": "NO EVALUADO (discrecional — confirmación humana)",
            },
        }
    return {"signal": False, "reason": "sin giro válido en zona en las últimas barras",
            "levels": {k: round(v, 2) for k, v in levels.items()}}


def _bar_close_ts(vbars: list[dict], i: int) -> float:
    """Timestamp de cierre de la barra i (apertura de la siguiente, o la propia)."""
    ref = vbars[i + 1]["time"] if i + 1 < len(vbars) else vbars[i]["time"]
    return datetime.fromisoformat(ref).timestamp()


DVA_SLOPE_WINDOW_MINUTES = 180
DVA_MIN_SPAN_MINUTES = 60
DVA_NORM_SLOPE_THRESHOLD = 0.25
# Persistencia: fracción mínima de la ventana con el precio más allá de la 1ª
# desviación para declarar imbalance sin pendiente (donAdri: "cabalgando").
DVA_PERSISTENCE_MIN_FRAC = 0.60
# Cruce bilateral: fracción mínima a CADA lado del VWAP para forzar rotational.
DVA_TWO_SIDED_MIN_FRAC = 0.25


def dva_state(
    session_records: list[scid_reader.TickRecord],
    adr: float | None,
    latest_price: float,
    window_minutes: int = DVA_SLOPE_WINDOW_MINUTES,
) -> dict:
    """Classify the session DVA as imbalanced or rotational.

    donAdri's mechanical definition (2026-07-13): the DVA is imbalanced when
    the 1st-deviation VWAP bands ("bandas grises") have slope. The session
    VWAP is cumulative, so its raw points-per-hour slope decays as volume
    accumulates; the usable signal is the VWAP displacement over a trailing
    window NORMALIZED by the current 1st deviation (sigma), plus requiring
    BOTH bands to move in the same direction (early-session sigma expansion
    lifts the upper band while the DVA is still rotational).

    Calibrated against donAdri's own labels (2026-07-13, 3/3 match): Mon
    13-jul rotational, Fri 10-jul imbalanced_up, Tue 7-jul rotational.

    v2 (2026-07-16): added the second branch of donAdri's dual criterion —
    PERSISTENCE (price riding beyond the 1st deviation for most of the
    window) and the two-sided-VWAP-cross rotational override. Motivated by
    the 15-jul MNQ blind spot (obs #42): a 581-pt trend day kept reading
    rotational because the selloff inflated sigma and crushed norm_slope,
    while price rode below the lower band for hours.
    """
    if not session_records:
        return {"state": "insufficient_data", "reason": "no session records"}

    cum_v = cum_pv = cum_p2v = 0.0
    last_price = float(session_records[0].close)
    series: list[tuple[float, float, float, float]] = []  # (ts, vwap, sigma, price)
    current_minute: int | None = None

    def snapshot(ts: float) -> None:
        if cum_v <= 0:
            return
        vwap = cum_pv / cum_v
        var = max(0.0, cum_p2v / cum_v - vwap * vwap)
        series.append((ts, vwap, var ** 0.5, last_price))

    for r in session_records:
        minute = int(r.unix_time // 60) * 60
        if current_minute is None:
            current_minute = minute
        elif minute != current_minute:
            snapshot(current_minute + 60)
            current_minute = minute
        v = float(r.volume)
        p = float(r.close)
        last_price = p
        cum_v += v
        cum_pv += p * v
        cum_p2v += p * p * v
    snapshot(session_records[-1].unix_time)

    if len(series) < 2:
        return {"state": "insufficient_data", "reason": "session too short"}

    end_ts, vwap_now, sigma_now, _ = series[-1]
    target_ts = end_ts - window_minutes * 60
    past = series[0]
    for row in series:
        if row[0] <= target_ts:
            past = row
        else:
            break
    span_minutes = (end_ts - past[0]) / 60
    if span_minutes < DVA_MIN_SPAN_MINUTES:
        return {
            "state": "insufficient_data",
            "reason": f"only {span_minutes:.1f} minutes of session data",
        }

    vwap_delta = vwap_now - past[1]
    sigma_delta = sigma_now - past[2]
    upper_band_delta = vwap_delta + sigma_delta
    lower_band_delta = vwap_delta - sigma_delta
    norm_slope = (vwap_delta / sigma_now) if sigma_now > 0 else 0.0
    bands_aligned_up = min(upper_band_delta, lower_band_delta) > 0
    bands_aligned_down = max(upper_band_delta, lower_band_delta) < 0

    # Rama 2 del criterio donAdri — PERSISTENCIA: qué fracción de la ventana
    # pasó el precio más allá de la 1ª desviación, y si cruzó el VWAP por los
    # dos lados. En trend days la sigma se infla con el propio movimiento y
    # aplasta norm_slope (punto ciego detectado el 15-jul en MNQ, obs #42);
    # el precio "cabalgando" fuera de la banda es la señal que sobrevive.
    window_rows = [row for row in series if row[0] > target_ts]
    n_rows = len(window_rows)
    above_band = below_band = above_vwap = below_vwap = 0
    for _, w_vwap, w_sigma, w_price in window_rows:
        if w_price > w_vwap:
            above_vwap += 1
        elif w_price < w_vwap:
            below_vwap += 1
        if w_price > w_vwap + w_sigma:
            above_band += 1
        elif w_price < w_vwap - w_sigma:
            below_band += 1
    frac_above_band = above_band / n_rows if n_rows else 0.0
    frac_below_band = below_band / n_rows if n_rows else 0.0
    frac_above_vwap = above_vwap / n_rows if n_rows else 0.0
    frac_below_vwap = below_vwap / n_rows if n_rows else 0.0

    two_sided = min(frac_above_vwap, frac_below_vwap) >= DVA_TWO_SIDED_MIN_FRAC
    persistence_up = frac_above_band >= DVA_PERSISTENCE_MIN_FRAC
    persistence_down = frac_below_band >= DVA_PERSISTENCE_MIN_FRAC
    slope_up = bands_aligned_up and norm_slope >= DVA_NORM_SLOPE_THRESHOLD
    slope_down = bands_aligned_down and norm_slope <= -DVA_NORM_SLOPE_THRESHOLD

    # "Si el precio cruza el VWAP por ambos lados, es rotacional aunque haya
    # deriva" (donAdri) — el cruce bilateral domina sobre la pendiente.
    if two_sided:
        state = "rotational"
        criterion = "two_sided_vwap_cross"
    elif slope_up or persistence_up:
        state = "imbalanced_up"
        criterion = "slope" if slope_up else "persistence"
    elif slope_down or persistence_down:
        state = "imbalanced_down"
        criterion = "slope" if slope_down else "persistence"
    else:
        state = "rotational"
        criterion = "none"

    return {
        "state": state,
        "criterion": criterion,
        "window_minutes_used": round(span_minutes, 1),
        "vwap_delta_points": round(vwap_delta, 3),
        "sigma_now": round(sigma_now, 3),
        "norm_slope": round(norm_slope, 3),
        "upper_band_delta_points": round(upper_band_delta, 3),
        "lower_band_delta_points": round(lower_band_delta, 3),
        "bands_aligned": "up" if bands_aligned_up else ("down" if bands_aligned_down else "mixed"),
        "frac_above_band": round(frac_above_band, 3),
        "frac_below_band": round(frac_below_band, 3),
        "frac_above_vwap": round(frac_above_vwap, 3),
        "frac_below_vwap": round(frac_below_vwap, 3),
        "threshold_norm_slope": DVA_NORM_SLOPE_THRESHOLD,
        "threshold_persistence": DVA_PERSISTENCE_MIN_FRAC,
        "definition": (
            "criterio doble donAdri: pendiente normalizada de las bandas O "
            "persistencia del precio fuera de la 1ª desviación; cruce del VWAP "
            "por ambos lados fuerza rotational aunque haya deriva"
        ),
    }


def build_market_features(
    records: list[scid_reader.TickRecord],
    tick_size: float,
    value_area_percent: float,
) -> dict:
    """Build current/previous session features and derived playbook levels."""
    last_record = records[-1]
    current_start = globex_equity_session_start(last_record.unix_time)
    previous_start = previous_nonempty_session_start(records, current_start)

    current_records = records_for_session(records, current_start)
    previous_records = records_for_session(records, previous_start) if previous_start is not None else []
    if not current_records:
        current_records = records

    current = profile_context(current_records, tick_size, value_area_percent)
    previous = profile_context(previous_records, tick_size, value_area_percent)
    latest_price = float(last_record.close)

    current_profile = current.get("volume_profile", {})
    previous_profile = previous.get("volume_profile", {})
    current_vwap = current.get("vwap")
    previous_vwap = previous.get("vwap")

    relationships = {
        "current_value_location": position_vs_value(latest_price, current_profile),
        "previous_value_location": position_vs_value(latest_price, previous_profile),
        "distance_to_current_vwap": distance(latest_price, current_vwap),
        "distance_to_previous_vwap": distance(latest_price, previous_vwap),
        "distance_to_current_vah": distance(latest_price, current_profile.get("vah")),
        "distance_to_current_val": distance(latest_price, current_profile.get("val")),
        "distance_to_current_poc": distance(latest_price, current_profile.get("poc")),
        "distance_to_previous_vah": distance(latest_price, previous_profile.get("vah")),
        "distance_to_previous_val": distance(latest_price, previous_profile.get("val")),
        "distance_to_previous_poc": distance(latest_price, previous_profile.get("poc")),
    }

    derived = derived_levels(records, current_start, previous_records)
    adr_value = (derived.get("adr") or {}).get("value")
    current["dva_state"] = dva_state(current_records, adr_value, latest_price)

    # RTH-only profiles — the donAdri-system value areas (see rth_records_for_session).
    current_rth = profile_context(
        rth_records_for_session(current_records, current_start), tick_size, value_area_percent
    )
    previous_rth = profile_context(
        rth_records_for_session(previous_records, previous_start), tick_size, value_area_percent
    ) if previous_start is not None else {}

    read_parts: list[str] = []
    current_loc = relationships["current_value_location"]
    previous_loc = relationships["previous_value_location"]
    if current_loc != "unknown":
        read_parts.append(f"price_{current_loc}_current_value")
    if previous_loc != "unknown":
        read_parts.append(f"price_{previous_loc}_previous_value")
    if current_vwap is not None:
        read_parts.append("above_current_vwap" if latest_price > current_vwap else "below_current_vwap")
    if current.get("delta") is not None:
        read_parts.append("positive_delta" if current["delta"] > 0 else "negative_delta")
    dva = current["dva_state"].get("state")
    if dva and dva != "insufficient_data":
        read_parts.append(f"dva_{dva}")

    return {
        "current_start": current_start,
        "previous_start": previous_start,
        "current_records": current_records,
        "previous_records": previous_records,
        "current_session": current,
        "previous_session": previous,
        "current_session_rth": current_rth,
        "previous_session_rth": previous_rth,
        "relationships": relationships,
        "read_tags": read_parts,
        "derived_levels": derived,
    }
