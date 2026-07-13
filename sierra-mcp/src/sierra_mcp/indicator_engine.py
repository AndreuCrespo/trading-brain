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


DVA_SLOPE_WINDOW_MINUTES = 180
DVA_MIN_SPAN_MINUTES = 60
DVA_NORM_SLOPE_THRESHOLD = 0.25


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
    13-jul rotational (norm +0.163), Fri 10-jul imbalanced_up (+0.314), Tue
    7-jul rotational (-0.226) — on the last one the classifier contradicted
    our hand label and the author sided with the classifier. His reference
    magnitudes: ~+0.4 sigma/3h = imbalanced; ~-0.27 sigma/1.5h with price
    crossing sides = rotational. Keep collecting daily labels; a future
    refinement is price side-stability vs the bands (riding one side =
    imbalance, crossing through = rotational).
    """
    if not session_records:
        return {"state": "insufficient_data", "reason": "no session records"}

    cum_v = cum_pv = cum_p2v = 0.0
    series: list[tuple[float, float, float]] = []  # (ts, vwap, sigma)
    current_minute: int | None = None

    def snapshot(ts: float) -> None:
        if cum_v <= 0:
            return
        vwap = cum_pv / cum_v
        var = max(0.0, cum_p2v / cum_v - vwap * vwap)
        series.append((ts, vwap, var ** 0.5))

    for r in session_records:
        minute = int(r.unix_time // 60) * 60
        if current_minute is None:
            current_minute = minute
        elif minute != current_minute:
            snapshot(current_minute + 60)
            current_minute = minute
        v = float(r.volume)
        p = float(r.close)
        cum_v += v
        cum_pv += p * v
        cum_p2v += p * p * v
    snapshot(session_records[-1].unix_time)

    if len(series) < 2:
        return {"state": "insufficient_data", "reason": "session too short"}

    end_ts, vwap_now, sigma_now = series[-1]
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

    if bands_aligned_up and norm_slope >= DVA_NORM_SLOPE_THRESHOLD:
        state = "imbalanced_up"
    elif bands_aligned_down and norm_slope <= -DVA_NORM_SLOPE_THRESHOLD:
        state = "imbalanced_down"
    else:
        state = "rotational"

    return {
        "state": state,
        "window_minutes_used": round(span_minutes, 1),
        "vwap_delta_points": round(vwap_delta, 3),
        "sigma_now": round(sigma_now, 3),
        "norm_slope": round(norm_slope, 3),
        "upper_band_delta_points": round(upper_band_delta, 3),
        "lower_band_delta_points": round(lower_band_delta, 3),
        "bands_aligned": "up" if bands_aligned_up else ("down" if bands_aligned_down else "mixed"),
        "threshold_norm_slope": DVA_NORM_SLOPE_THRESHOLD,
        "definition": (
            "imbalanced when both 1st-deviation VWAP bands move together and "
            "|vwap displacement over window| >= threshold * sigma (donAdri); "
            "threshold provisional, calibrate visually against his chart"
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
        "relationships": relationships,
        "read_tags": read_parts,
        "derived_levels": derived,
    }
