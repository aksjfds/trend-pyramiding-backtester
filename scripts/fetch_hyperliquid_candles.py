from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_URL = "https://api.hyperliquid.xyz/info"
INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_utc(timestamp_ms: int) -> str:
    value = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def post_json(payload: dict) -> list[dict]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "trend-pyramiding-backtester/0.1",
        },
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.load(response)
            if not isinstance(result, list):
                raise RuntimeError(f"unexpected API response: {result!r}")
            return result
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code < 500 and exc.code != 429:
                raise
            time.sleep(2**attempt)
    raise RuntimeError(f"Hyperliquid API request failed: {last_error}")


def fetch_candles(coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    step_ms = INTERVAL_MS[interval]
    cursor = start_ms
    candles: dict[int, dict] = {}

    while cursor <= end_ms:
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
            },
        }
        batch = post_json(payload)
        if not batch:
            break

        usable = [
            candle
            for candle in batch
            if int(candle["t"]) >= cursor
            and int(candle["t"]) <= end_ms
            and int(candle["T"]) <= end_ms
        ]
        if not usable:
            break

        for candle in usable:
            candles[int(candle["t"])] = candle

        last_open = max(int(candle["t"]) for candle in usable)
        next_cursor = last_open + step_ms
        if next_cursor <= cursor:
            raise RuntimeError("pagination did not advance")
        cursor = next_cursor
        time.sleep(0.05)

    ordered = [candles[key] for key in sorted(candles)]
    if not ordered:
        raise RuntimeError("no candles returned")

    if int(ordered[0]["t"]) != start_ms:
        raise RuntimeError(
            f"first candle mismatch: expected {iso_utc(start_ms)}, "
            f"got {iso_utc(int(ordered[0]['t']))}"
        )

    for previous, current in zip(ordered, ordered[1:]):
        gap = int(current["t"]) - int(previous["t"])
        if gap != step_ms:
            raise RuntimeError(
                f"candle gap detected after {iso_utc(int(previous['t']))}: {gap} ms"
            )

    return ordered


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--coin", required=True)
    parser.add_argument("--interval", default="1h", choices=sorted(INTERVAL_MS))
    parser.add_argument("--start", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--metadata", required=True)
    args = parser.parse_args()

    step_ms = INTERVAL_MS[args.interval]
    start = parse_utc(args.start)
    start_ms = int(start.timestamp() * 1000)
    if start_ms % step_ms != 0:
        raise SystemExit("start must align to the candle interval")

    now = datetime.now(timezone.utc)
    current_interval_open_ms = int(now.timestamp() * 1000) // step_ms * step_ms
    end_ms = current_interval_open_ms - 1
    if end_ms < start_ms:
        raise SystemExit("requested range has no fully closed candles")

    candles = fetch_candles(args.coin, args.interval, start_ms, end_ms)

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
                    "timestamp": iso_utc(int(candle["t"])),
                    "open": candle["o"],
                    "high": candle["h"],
                    "low": candle["l"],
                    "close": candle["c"],
                    "volume": candle["v"],
                }
            )

    metadata = {
        "source": "Hyperliquid public API candleSnapshot",
        "source_url": API_URL,
        "market": f"{args.coin} perpetual",
        "coin": args.coin,
        "interval": args.interval,
        "requested_start": iso_utc(start_ms),
        "first_candle_open": iso_utc(int(candles[0]["t"])),
        "last_candle_open": iso_utc(int(candles[-1]["t"])),
        "last_candle_close": iso_utc(int(candles[-1]["T"])),
        "bars": len(candles),
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
