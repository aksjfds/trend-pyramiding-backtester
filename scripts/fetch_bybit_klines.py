from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_URL = "https://api.bybit.com/v5/market/kline"
INTERVAL_MS = {
    "1": 60_000,
    "3": 180_000,
    "5": 300_000,
    "15": 900_000,
    "30": 1_800_000,
    "60": 3_600_000,
    "120": 7_200_000,
    "240": 14_400_000,
    "360": 21_600_000,
    "720": 43_200_000,
    "D": 86_400_000,
}


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_utc(timestamp_ms: int) -> str:
    value = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def request_klines(
    *,
    category: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[list[str]]:
    params = urllib.parse.urlencode(
        {
            "category": category,
            "symbol": symbol,
            "interval": interval,
            "start": start_ms,
            "end": end_ms,
            "limit": 1000,
        }
    )
    url = f"{API_URL}?{params}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "trend-pyramiding-backtester/0.1"},
    )

    last_error: Exception | None = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
            if int(payload.get("retCode", -1)) != 0:
                raise RuntimeError(f"Bybit API error: {payload!r}")
            rows = payload.get("result", {}).get("list", [])
            if not isinstance(rows, list):
                raise RuntimeError(f"unexpected Bybit response: {payload!r}")
            return rows
        except Exception as exc:
            last_error = exc
            if attempt == 4:
                break
            time.sleep(2**attempt)
    raise RuntimeError(f"Bybit kline request failed: {last_error}")


def fetch_all(
    *,
    category: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[list[str]]:
    step_ms = INTERVAL_MS[interval]
    cursor_end = end_ms
    candles: dict[int, list[str]] = {}

    while cursor_end >= start_ms:
        rows = request_klines(
            category=category,
            symbol=symbol,
            interval=interval,
            start_ms=start_ms,
            end_ms=cursor_end,
        )
        if not rows:
            break

        parsed: list[tuple[int, list[str]]] = []
        for row in rows:
            ts = int(row[0])
            if start_ms <= ts <= end_ms:
                parsed.append((ts, row))
                candles[ts] = row

        if not parsed:
            break

        earliest = min(ts for ts, _ in parsed)
        next_end = earliest - 1
        if next_end >= cursor_end:
            raise RuntimeError("pagination did not move backward")
        cursor_end = next_end
        time.sleep(0.05)

    ordered = [candles[key] for key in sorted(candles)]
    if not ordered:
        raise RuntimeError("no Bybit candles returned")

    last_ts = int(ordered[-1][0])

    for previous, current in zip(ordered, ordered[1:]):
        gap = int(current[0]) - int(previous[0])
        if gap != step_ms:
            raise RuntimeError(
                f"candle gap detected after {iso_utc(int(previous[0]))}: {gap} ms"
            )

    if last_ts + step_ms - 1 > end_ms:
        raise RuntimeError("last candle is not fully closed inside requested range")

    return ordered


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="HYPEUSDT")
    parser.add_argument("--category", default="linear")
    parser.add_argument("--interval", default="60", choices=sorted(INTERVAL_MS))
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", default=None)
    parser.add_argument("--output", required=True)
    parser.add_argument("--metadata", required=True)
    args = parser.parse_args()

    step_ms = INTERVAL_MS[args.interval]
    start_ms = int(parse_utc(args.start).timestamp() * 1000)

    now = datetime.now(timezone.utc)
    if args.end is None:
        current_open_ms = int(now.timestamp() * 1000) // step_ms * step_ms
        end_exclusive_ms = current_open_ms
    else:
        end_exclusive_ms = int(parse_utc(args.end).timestamp() * 1000)

    end_ms = end_exclusive_ms - 1
    rows = fetch_all(
        category=args.category,
        symbol=args.symbol,
        interval=args.interval,
        start_ms=start_ms,
        end_ms=end_ms,
    )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["timestamp", "open", "high", "low", "close", "volume"],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "timestamp": iso_utc(int(row[0])),
                    "open": row[1],
                    "high": row[2],
                    "low": row[3],
                    "close": row[4],
                    "volume": row[5],
                }
            )

    first_ts = int(rows[0][0])
    last_ts = int(rows[-1][0])
    metadata = {
        "source": "Bybit public API v5 /market/kline",
        "source_url": API_URL,
        "market": f"{args.symbol} {args.category}",
        "symbol": args.symbol,
        "category": args.category,
        "interval": args.interval,
        "requested_start": iso_utc(start_ms),
        "first_candle_open": iso_utc(first_ts),
        "last_candle_open": iso_utc(last_ts),
        "last_candle_close": iso_utc(last_ts + step_ms - 1),
        "bars": len(rows),
        "fetched_at": now.isoformat().replace("+00:00", "Z"),
    }

    metadata_path = Path(args.metadata)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
