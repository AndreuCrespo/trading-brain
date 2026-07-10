"""Reader for Sierra Chart .scid intraday tick files.

Format (modern, post-2017):
  Header (56 bytes): "SCID" + uint32 header_size(56) + uint32 record_size(40)
                   + uint16 version + uint16 unused + uint32 utc_start_index
                   + 36 bytes reserved
  Each record (40 bytes): int64 SCDateTime (microseconds since 1899-12-30 UTC)
                          + 4x float (OHLC)
                          + 4x uint32 (NumTrades, TotalVolume, BidVolume, AskVolume)

For tick records (Intraday Data Storage Time Unit = 1 Tick) all OHLC fields
hold the same trade price.
"""

import os
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

HEADER_SIZE = 56
RECORD_SIZE = 40
RECORD_STRUCT = struct.Struct("<qffffIIII")

SC_EPOCH = datetime(1899, 12, 30, tzinfo=timezone.utc)


def sc_datetime_to_unix(sc_dt: int) -> float:
    return (SC_EPOCH + timedelta(microseconds=sc_dt) - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds()


@dataclass(slots=True)
class TickRecord:
    sc_datetime: int
    unix_time: float
    open: float
    high: float
    low: float
    close: float
    num_trades: int
    volume: int
    bid_volume: int
    ask_volume: int


def read_tail_records(path: str, max_records: int) -> list[TickRecord]:
    """Read up to `max_records` records from the end of the file, in chronological order."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        file_size = f.tell()
        data_bytes = file_size - HEADER_SIZE
        if data_bytes <= 0:
            return []
        n_in_file = data_bytes // RECORD_SIZE
        n_to_read = min(max_records, n_in_file)
        start = HEADER_SIZE + (n_in_file - n_to_read) * RECORD_SIZE
        f.seek(start)
        blob = f.read(n_to_read * RECORD_SIZE)

    records: list[TickRecord] = []
    for i in range(n_to_read):
        chunk = blob[i * RECORD_SIZE : (i + 1) * RECORD_SIZE]
        sc_dt, o, h, l, c, nt, vol, bvol, avol = RECORD_STRUCT.unpack(chunk)
        records.append(TickRecord(
            sc_datetime=sc_dt,
            unix_time=sc_datetime_to_unix(sc_dt),
            open=o, high=h, low=l, close=c,
            num_trades=nt, volume=vol,
            bid_volume=bvol, ask_volume=avol,
        ))
    return records


def read_records_since(path: str, since_unix: float, max_records: int = 0) -> list[TickRecord]:
    """Read records with unix_time >= since_unix, in chronological order.

    Records are fixed-size and time-ordered, so the start offset is found with
    a binary search on the SCDateTime field. Dense symbols (e.g. MNQ tick data)
    can exceed a fixed record-count tail within a couple of sessions; a
    time-based window keeps the previous-session lookback correct regardless of
    tick density. If the window holds more than max_records (when nonzero),
    only the most recent max_records are returned.
    """
    target_scdt = int((since_unix - SC_EPOCH.timestamp()) * 1_000_000)
    with open(path, "rb") as f:
        f.seek(0, 2)
        data_bytes = f.tell() - HEADER_SIZE
        if data_bytes < RECORD_SIZE:
            return []
        n_in_file = data_bytes // RECORD_SIZE

        lo, hi = 0, n_in_file  # first index with sc_dt >= target
        while lo < hi:
            mid = (lo + hi) // 2
            f.seek(HEADER_SIZE + mid * RECORD_SIZE)
            (sc_dt,) = struct.unpack("<q", f.read(8))
            if sc_dt < target_scdt:
                lo = mid + 1
            else:
                hi = mid

        start = lo
        if max_records and n_in_file - start > max_records:
            start = n_in_file - max_records
        if start >= n_in_file:
            return []
        f.seek(HEADER_SIZE + start * RECORD_SIZE)
        blob = f.read((n_in_file - start) * RECORD_SIZE)

    records: list[TickRecord] = []
    for chunk in RECORD_STRUCT.iter_unpack(blob):
        sc_dt, o, h, l, c, nt, vol, bvol, avol = chunk
        records.append(TickRecord(
            sc_datetime=sc_dt,
            unix_time=sc_datetime_to_unix(sc_dt),
            open=o, high=h, low=l, close=c,
            num_trades=nt, volume=vol,
            bid_volume=bvol, ask_volume=avol,
        ))
    return records


def read_last_record(path: str) -> TickRecord | None:
    """Read the last complete record from a .scid file."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        file_size = f.tell()
        data_bytes = file_size - HEADER_SIZE
        if data_bytes < RECORD_SIZE:
            return None
        n_in_file = data_bytes // RECORD_SIZE
        start = HEADER_SIZE + (n_in_file - 1) * RECORD_SIZE
        f.seek(start)
        chunk = f.read(RECORD_SIZE)

    sc_dt, o, h, l, c, nt, vol, bvol, avol = RECORD_STRUCT.unpack(chunk)
    return TickRecord(
        sc_datetime=sc_dt,
        unix_time=sc_datetime_to_unix(sc_dt),
        open=o,
        high=h,
        low=l,
        close=c,
        num_trades=nt,
        volume=vol,
        bid_volume=bvol,
        ask_volume=avol,
    )


def file_status(path: str) -> dict:
    """Return lightweight status for a .scid file."""
    stat = os.stat(path)
    data_bytes = max(0, stat.st_size - HEADER_SIZE)
    return {
        "path": path,
        "size_bytes": stat.st_size,
        "record_count": data_bytes // RECORD_SIZE,
        "modified_unix": stat.st_mtime,
        "modified_time": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def aggregate_to_bars(records: list[TickRecord], interval_seconds: int) -> list[dict]:
    """Aggregate tick records into OHLCV bars at the given interval.

    For tick records (where OHLC are all the same trade price), each bar's open is
    the first trade in the bucket, close is the last, high/low are min/max.
    """
    if interval_seconds <= 0 or not records:
        return []

    bars: dict[int, dict] = {}
    for r in records:
        bucket = int(r.unix_time // interval_seconds) * interval_seconds
        bar = bars.get(bucket)
        if bar is None:
            bars[bucket] = {
                "time": bucket,
                "open": r.close,
                "high": r.close,
                "low": r.close,
                "close": r.close,
                "volume": r.volume,
                "num_trades": r.num_trades,
                "bid_volume": r.bid_volume,
                "ask_volume": r.ask_volume,
            }
        else:
            price = r.close
            if price > bar["high"]:
                bar["high"] = price
            if price < bar["low"]:
                bar["low"] = price
            bar["close"] = price
            bar["volume"] += r.volume
            bar["num_trades"] += r.num_trades
            bar["bid_volume"] += r.bid_volume
            bar["ask_volume"] += r.ask_volume

    ordered = sorted(bars.values(), key=lambda b: b["time"])
    for b in ordered:
        b["time"] = datetime.fromtimestamp(b["time"], tz=timezone.utc).isoformat()
    return ordered


def aggregate_session_stats(records: list[TickRecord]) -> dict:
    """Aggregate records into simple session-level stats."""
    if not records:
        return {}

    first = records[0]
    last = records[-1]
    high = max(r.close for r in records)
    low = min(r.close for r in records)
    volume = sum(r.volume for r in records)
    bid_volume = sum(r.bid_volume for r in records)
    ask_volume = sum(r.ask_volume for r in records)
    vwap_num = sum(r.close * r.volume for r in records)
    vwap = (vwap_num / volume) if volume else None

    return {
        "start_time": datetime.fromtimestamp(first.unix_time, tz=timezone.utc).isoformat(),
        "end_time": datetime.fromtimestamp(last.unix_time, tz=timezone.utc).isoformat(),
        "open": first.close,
        "high": high,
        "low": low,
        "last": last.close,
        "range": high - low,
        "volume": volume,
        "bid_volume": bid_volume,
        "ask_volume": ask_volume,
        "delta": ask_volume - bid_volume,
        "vwap": vwap,
    }
