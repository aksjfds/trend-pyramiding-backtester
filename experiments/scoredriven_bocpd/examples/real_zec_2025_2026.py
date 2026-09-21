from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import urllib.request
import zipfile

import numpy as np
import pandas as pd

from scoredriven_bocpd import BOCPDConfig, DowntrendConfig, DowntrendDetector, MarketFeatureBuilder


BASE = "https://data.binance.vision/data/spot"
CACHE = Path(".cache/zecusdt_1h")
SYMBOL = "ZECUSDT"
INTERVAL = "1h"
MODEL_REVISION = "bayesian-mean-map-reset-bull-to-bear-v3"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "scoredriven-bocpd-test/0.1"})
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read()


def verified_zip(url: str, cache_path: Path) -> bytes:
    CACHE.mkdir(parents=True, exist_ok=True)
    checksum_path = Path(str(cache_path) + ".CHECKSUM")

    if not cache_path.exists():
        cache_path.write_bytes(fetch(url))
    if not checksum_path.exists():
        checksum_path.write_bytes(fetch(url + ".CHECKSUM"))

    expected = checksum_path.read_text(encoding="utf-8").strip().split()[0].lower()
    payload = cache_path.read_bytes()
    actual = hashlib.sha256(payload).hexdigest().lower()
    if actual != expected:
        raise RuntimeError(f"checksum mismatch for {cache_path.name}: {actual} != {expected}")
    return payload


def parse_kline_zip(payload: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        members = [m for m in zf.namelist() if m.lower().endswith(".csv")]
        if len(members) != 1:
            raise RuntimeError(f"expected one CSV, got {members}")
        raw = zf.read(members[0]).decode("utf-8")

    rows = list(csv.reader(io.StringIO(raw)))
    if not rows:
        raise RuntimeError("empty CSV")
    if rows[0] and not rows[0][0].lstrip("-").isdigit():
        rows = rows[1:]

    columns = [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_volume",
        "num_trades",
        "taker_buy_base",
        "taker_buy_quote",
        "ignore",
    ]
    frame = pd.DataFrame(rows, columns=columns)
    for col in columns:
        frame[col] = pd.to_numeric(frame[col], errors="raise")

    frame["timestamp"] = pd.to_datetime(frame["open_time"], unit="us", utc=True)
    return frame


def monthly_frame(year: int, month: int) -> pd.DataFrame:
    name = f"{SYMBOL}-{INTERVAL}-{year:04d}-{month:02d}.zip"
    url = f"{BASE}/monthly/klines/{SYMBOL}/{INTERVAL}/{name}"
    return parse_kline_zip(verified_zip(url, CACHE / name))


def daily_frame(day: pd.Timestamp) -> pd.DataFrame:
    date = day.strftime("%Y-%m-%d")
    name = f"{SYMBOL}-{INTERVAL}-{date}.zip"
    url = f"{BASE}/daily/klines/{SYMBOL}/{INTERVAL}/{name}"
    return parse_kline_zip(verified_zip(url, CACHE / name))


def load_period(start: str, end: str) -> pd.DataFrame:
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC")

    frames: list[pd.DataFrame] = []
    cursor = start_ts.normalize().replace(day=1)

    while cursor <= end_ts:
        month_end = cursor + pd.offsets.MonthEnd(1) + pd.Timedelta(hours=23)
        if cursor >= start_ts and month_end <= end_ts:
            frames.append(monthly_frame(cursor.year, cursor.month))
        else:
            day = max(cursor.normalize(), start_ts.normalize())
            final_day = min(month_end.normalize(), end_ts.normalize())
            while day <= final_day:
                frames.append(daily_frame(day))
                day += pd.Timedelta(days=1)
        cursor = (cursor + pd.offsets.MonthBegin(1)).normalize()

    data = pd.concat(frames, ignore_index=True).sort_values("timestamp").reset_index(drop=True)
    data = data[(data["timestamp"] >= start_ts) & (data["timestamp"] <= end_ts)].reset_index(drop=True)

    if data["timestamp"].duplicated().any():
        raise RuntimeError("duplicate timestamps in Binance data")

    expected = pd.date_range(start_ts, end_ts, freq="1h")
    actual = pd.DatetimeIndex(data["timestamp"])
    if not actual.equals(expected):
        missing = expected.difference(actual)
        extra = actual.difference(expected)
        raise RuntimeError(
            f"timestamp continuity failed: expected={len(expected)} got={len(actual)} "
            f"missing={len(missing)} extra={len(extra)}"
        )

    return data


def prepare_features(data: pd.DataFrame) -> pd.DataFrame:
    total_volume = data["volume"].astype(float)
    taker_buy = data["taker_buy_base"].astype(float)
    feature_input = pd.DataFrame(
        {
            "close": data["close"].astype(float).to_numpy(),
            "high": data["high"].astype(float).to_numpy(),
            "low": data["low"].astype(float).to_numpy(),
            "volume": total_volume.to_numpy(),
            "buy_volume": taker_buy.to_numpy(),
            "sell_volume": (total_volume - taker_buy).clip(lower=0.0).to_numpy(),
        },
        index=pd.DatetimeIndex(data["timestamp"]),
    )
    features = MarketFeatureBuilder().build(feature_input)
    if float(features["log_return"].std()) <= 1e-12:
        raise RuntimeError("feature pipeline produced near-zero log_return variance")
    if "trade_flow_imbalance" in features and float(features["trade_flow_imbalance"].std()) <= 1e-12:
        raise RuntimeError("feature pipeline produced near-zero trade_flow_imbalance variance")
    return features


def forward_metrics(data: pd.DataFrame, i: int) -> dict[str, float]:
    close0 = float(data.at[i, "close"])

    def ret(hours: int) -> float:
        return float(data.at[i + hours, "close"] / close0 - 1.0)

    low24 = float(data.loc[i + 1 : i + 24, "low"].min())
    low48 = float(data.loc[i + 1 : i + 48, "low"].min())
    high24 = float(data.loc[i + 1 : i + 24, "high"].max())

    return {
        "ret_6h": ret(6),
        "ret_12h": ret(12),
        "ret_24h": ret(24),
        "ret_48h": ret(48),
        "drawdown_24h": low24 / close0 - 1.0,
        "drawdown_48h": low48 / close0 - 1.0,
        "runup_24h": high24 / close0 - 1.0,
    }


def summarize(events: list[dict[str, float]]) -> dict[str, float | int | None]:
    if not events:
        return {
            "count": 0,
            "mean_ret_6h": None,
            "mean_ret_12h": None,
            "mean_ret_24h": None,
            "mean_ret_48h": None,
            "median_ret_24h": None,
            "negative_24h_rate": None,
            "close_le_minus_1pct_24h_rate": None,
            "close_le_minus_2pct_24h_rate": None,
            "close_le_minus_5pct_24h_rate": None,
            "drawdown_le_minus_1pct_24h_rate": None,
            "drawdown_le_minus_2pct_24h_rate": None,
            "drawdown_le_minus_5pct_24h_rate": None,
            "mean_drawdown_24h": None,
        }

    def arr(name: str) -> np.ndarray:
        return np.asarray([float(e[name]) for e in events], dtype=float)

    r6 = arr("ret_6h")
    r12 = arr("ret_12h")
    r24 = arr("ret_24h")
    r48 = arr("ret_48h")
    dd24 = arr("drawdown_24h")

    return {
        "count": len(events),
        "mean_ret_6h": float(r6.mean()),
        "mean_ret_12h": float(r12.mean()),
        "mean_ret_24h": float(r24.mean()),
        "mean_ret_48h": float(r48.mean()),
        "median_ret_24h": float(np.median(r24)),
        "negative_24h_rate": float(np.mean(r24 < 0.0)),
        "close_le_minus_1pct_24h_rate": float(np.mean(r24 <= -0.01)),
        "close_le_minus_2pct_24h_rate": float(np.mean(r24 <= -0.02)),
        "close_le_minus_5pct_24h_rate": float(np.mean(r24 <= -0.05)),
        "drawdown_le_minus_1pct_24h_rate": float(np.mean(dd24 <= -0.01)),
        "drawdown_le_minus_2pct_24h_rate": float(np.mean(dd24 <= -0.02)),
        "drawdown_le_minus_5pct_24h_rate": float(np.mean(dd24 <= -0.05)),
        "mean_drawdown_24h": float(dd24.mean()),
    }


def run_period(label: str, start: str, end: str) -> dict:
    data = load_period(start, end)
    features = prepare_features(data)

    bocpd_config = BOCPDConfig()
    downtrend_config = DowntrendConfig()
    detector = DowntrendDetector(
        features.columns,
        bocpd_config=bocpd_config,
        config=downtrend_config,
    )

    events: list[dict[str, float | int | str]] = []
    raw_trigger_hours = 0
    previous_triggered = False
    last_event_i = -10000
    cooldown_hours = 24

    for i, (_, row) in enumerate(features.iterrows()):
        signal = detector.update(row.to_dict())
        if signal.triggered:
            raw_trigger_hours += 1

        rising_edge = signal.triggered and not previous_triggered
        cooled_down = i - last_event_i >= cooldown_hours
        has_future = i + 48 < len(data)

        if rising_edge and cooled_down and has_future:
            item = {
                "timestamp": data.at[i, "timestamp"].isoformat(),
                "close": float(data.at[i, "close"]),
                "cp_probability": signal.bocpd.changepoint_probability,
                "short_run_probability": signal.bocpd.short_run_probability,
                "bearish_score": signal.bearish_score,
                "map_run_length": signal.bocpd.map_run_length,
            }
            item.update(forward_metrics(data, i))
            events.append(item)
            last_event_i = i

        previous_triggered = signal.triggered

    baseline: list[dict[str, float]] = []
    for i in range(downtrend_config.min_observations, len(data) - 48):
        baseline.append(forward_metrics(data, i))

    signal_summary = summarize(events)
    baseline_summary = summarize(baseline)

    def lift(metric: str) -> float | None:
        s = signal_summary.get(metric)
        b = baseline_summary.get(metric)
        if s is None or b in (None, 0):
            return None
        return float(s) / float(b)

    return {
        "label": label,
        "dataset": {
            "venue": "Binance Spot",
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "start": str(data.at[0, "timestamp"]),
            "end": str(data.at[len(data) - 1, "timestamp"]),
            "rows": int(len(data)),
            "checksums_verified": True,
        },
        "test_protocol": {
            "parameters": "project defaults; no ZEC tuning",
            "cooldown_hours": cooldown_hours,
            "event_definition": "rising edge of DowntrendDetector.triggered, >=24h since previous event",
            "evaluation": "forward close returns and future intraperiod lows",
        },
        "model": {
            "bocpd": vars(bocpd_config),
            "downtrend": vars(downtrend_config),
            "features": list(features.columns),
        },
        "raw_trigger_hours": raw_trigger_hours,
        "signal_summary": signal_summary,
        "baseline_summary": baseline_summary,
        "lift_vs_baseline": {
            "negative_24h_rate": lift("negative_24h_rate"),
            "close_le_minus_1pct_24h_rate": lift("close_le_minus_1pct_24h_rate"),
            "close_le_minus_2pct_24h_rate": lift("close_le_minus_2pct_24h_rate"),
            "close_le_minus_5pct_24h_rate": lift("close_le_minus_5pct_24h_rate"),
            "drawdown_le_minus_1pct_24h_rate": lift("drawdown_le_minus_1pct_24h_rate"),
            "drawdown_le_minus_2pct_24h_rate": lift("drawdown_le_minus_2pct_24h_rate"),
            "drawdown_le_minus_5pct_24h_rate": lift("drawdown_le_minus_5pct_24h_rate"),
        },
        "events": events,
    }


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100.0 * value:.2f}%"


def report_markdown(results: list[dict]) -> str:
    lines = [
        "# ZECUSDT Score-Driven BOCPD Real-Data Test",
        "",
        "Data: Binance Spot 1h public archives; SHA-256 checksums verified.",
        "",
        "Parameters: project defaults, no ZEC-specific tuning.",
        "",
        "| Period | Rows | Signals | Raw trigger hours | Mean 24h return | 24h negative rate | Baseline negative rate | Mean 24h drawdown |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        s = r["signal_summary"]
        b = r["baseline_summary"]
        lines.append(
            f'| {r["label"]} | {r["dataset"]["rows"]} | {s["count"]} | '
            f'{r["raw_trigger_hours"]} | {pct(s["mean_ret_24h"])} | '
            f'{pct(s["negative_24h_rate"])} | {pct(b["negative_24h_rate"])} | '
            f'{pct(s["mean_drawdown_24h"])} |'
        )

    lines += ["", "## Event details", ""]
    for r in results:
        lines.append(f'### {r["label"]}')
        lines.append("")
        if not r["events"]:
            lines.append("No downtrend events were emitted.")
            lines.append("")
            continue
        lines.append("| Timestamp | Close | Bearish score | CP prob. | Short-run prob. | 24h return | 24h drawdown |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for e in r["events"]:
            lines.append(
                f'| {e["timestamp"]} | {e["close"]:.4f} | {e["bearish_score"]:.4f} | '
                f'{e["cp_probability"]:.4f} | {e["short_run_probability"]:.4f} | '
                f'{pct(e["ret_24h"])} | {pct(e["drawdown_24h"])} |'
            )
        lines.append("")

    lines += [
        "## Method",
        "",
        "Signal events are rising edges of DowntrendDetector.triggered, with a 24-hour cooldown.",
        "Forward returns and future lows are evaluated only after the signal timestamp.",
        "",
        "This is a detector evaluation, not a trading PnL backtest.",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="zec_realtest_results")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    periods = [
        ("2025 full year", "2025-01-01 00:00:00", "2025-12-31 23:00:00"),
        ("2026 YTD through 2026-09-20", "2026-01-01 00:00:00", "2026-09-20 23:00:00"),
    ]

    results = []
    for label, start, end in periods:
        print(f"Running {label}...", flush=True)
        result = run_period(label, start, end)
        results.append(result)
        year = start[:4]
        (out_dir / f"zec_{year}_result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            f'{label}: rows={result["dataset"]["rows"]} '
            f'signals={result["signal_summary"]["count"]} '
            f'raw_trigger_hours={result["raw_trigger_hours"]}',
            flush=True,
        )

    summary = {
        "generated_by": "feature/scoredriven-bocpd",
        "model_revision": MODEL_REVISION,
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "results": results,
    }
    (out_dir / "zec_2025_2026_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown = report_markdown(results)
    (out_dir / "zec_2025_2026_summary.md").write_text(markdown, encoding="utf-8")

    print("ZEC_REAL_DATA_JSON_BEGIN")
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    print("ZEC_REAL_DATA_JSON_END")


if __name__ == "__main__":
    main()
