from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

API_URL = "https://api.coinw.com/api/v1/public"
SOURCE_PERIOD_MS = 30 * 60 * 1000
HOUR_MS = 60 * 60 * 1000
WINDOW_MS = 50 * HOUR_MS


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_utc(timestamp_ms: int) -> str:
    value = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def request_window(pair: str, start_ms: int, end_ms: int) -> list[dict]:
    query = urllib.parse.urlencode(
        {
            "command": "returnChartData",
            "currencyPair": pair,
            "period": 1800,
            "start": start_ms,
            "end": end_ms,
        }
    )
    request = urllib.request.Request(
        f"{API_URL}?{query}",
        headers={"User-Agent": "trend-pyramiding-backtester/0.1"},
    )

    last_error: Exception | None = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
            if str(payload.get("code")) != "200" or not payload.get("success", False):
                raise RuntimeError(f"CoinW API error: {payload!r}")
            rows = payload.get("data", [])
            if not isinstance(rows, list):
                raise RuntimeError(f"unexpected CoinW response: {payload!r}")
            return rows
        except Exception as exc:
            last_error = exc
            if attempt == 4:
                break
            time.sleep(2**attempt)
    raise RuntimeError(f"CoinW kline request failed: {last_error}")


def fetch_30m(pair: str, start_ms: int, end_ms: int) -> list[dict]:
    candles: dict[int, dict] = {}
    cursor = start_ms

    while cursor <= end_ms:
        window_end = min(cursor + WINDOW_MS - 1, end_ms)
        rows = request_window(pair, cursor, window_end)
        for row in rows:
            ts = int(row["date"])
            if cursor <= ts <= window_end:
                candles[ts] = row
        cursor = window_end + 1
        time.sleep(0.11)

    return [candles[key] for key in sorted(candles)]


def aggregate_1h(rows: list[dict]) -> tuple[list[dict], int]:
    grouped: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        ts = int(row["date"])
        hour = ts // HOUR_MS * HOUR_MS
        grouped[hour].append(row)

    candles: list[dict] = []
    dropped_incomplete_hours = 0
    for hour in sorted(grouped):
        group = sorted(grouped[hour], key=lambda item: int(item["date"]))
        expected = [hour, hour + SOURCE_PERIOD_MS]
        actual = [int(item["date"]) for item in group]
        if actual != expected:
            dropped_incomplete_hours += 1
            continue

        candles.append(
            {
                "timestamp": hour,
                "open": group[0]["open"],
                "high": max(float(item["high"]) for item in group),
                "low": min(float(item["low"]) for item in group),
                "close": group[-1]["close"],
                "volume": sum(float(item["volume"]) for item in group),
            }
        )

    return candles, dropped_incomplete_hours


def count_missing_hours(candles: list[dict]) -> int:
    if len(candles) < 2:
        return 0
    missing = 0
    for previous, current in zip(candles, candles[1:]):
        gap = int(current["timestamp"]) - int(previous["timestamp"])
        if gap > HOUR_MS:
            missing += gap // HOUR_MS - 1
    return missing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default="HYPE_USDT")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", default=None)
    parser.add_argument("--output", required=True)
    parser.add_argument("--metadata", required=True)
    args = parser.parse_args()

    start_ms = int(parse_utc(args.start).timestamp() * 1000)
    now = datetime.now(timezone.utc)

    if args.end is None:
        current_30m_open = int(now.timestamp() * 1000) // SOURCE_PERIOD_MS * SOURCE_PERIOD_MS
        end_exclusive_ms = current_30m_open
    else:
        end_exclusive_ms = int(parse_utc(args.end).timestamp() * 1000)

    end_ms = end_exclusive_ms - 1
    source_rows = fetch_30m(args.pair, start_ms, end_ms)
    if not source_rows:
        raise RuntimeError("CoinW returned no HYPE data")

    candles, dropped = aggregate_1h(source_rows)
    if not candles:
        raise RuntimeError("no complete 1H candles could be constructed")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["timestamp", "open", "high", "low", "close", "volume"],
        )
        writer.writeheader()
        for candle in candles:
            writer.writerow(
                {
                    "timestamp": iso_utc(int(candle["timestamp"])),
                    "open": candle["open"],
                    "high": candle["high"],
                    "low": candle["low"],
                    "close": candle["close"],
                    "volume": candle["volume"],
                }
            )

    missing_hours = count_missing_hours(candles)
    first_ts = int(candles[0]["timestamp"])
    last_ts = int(candles[-1]["timestamp"])
    metadata = {
        "source": "CoinW public spot API returnChartData; 30m aggregated to 1h",
        "source_url": API_URL,
        "market": args.pair,
        "interval": "1h",
        "requested_start": iso_utc(start_ms),
        "first_candle_open": iso_utc(first_ts),
        "last_candle_open": iso_utc(last_ts),
        "last_candle_close": iso_utc(last_ts + HOUR_MS - 1),
        "bars": len(candles),
        "source_30m_bars": len(source_rows),
        "dropped_incomplete_hours": dropped,
        "missing_hours_inside_range": missing_hours,
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
