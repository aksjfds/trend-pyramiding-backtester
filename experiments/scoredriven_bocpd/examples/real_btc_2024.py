from __future__ import annotations

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


BASE = "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1h"
CACHE = Path(".cache/btcusdt_1h_2024")
RESULT = Path("btc_2024_result.json")


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "scoredriven-bocpd-test/0.1"})
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read()


def download_verified_month(year: int, month: int) -> pd.DataFrame:
    CACHE.mkdir(parents=True, exist_ok=True)
    name = f"BTCUSDT-1h-{year:04d}-{month:02d}.zip"
    zip_path = CACHE / name
    checksum_path = CACHE / f"{name}.CHECKSUM"

    if not zip_path.exists():
        zip_path.write_bytes(fetch(f"{BASE}/{name}"))
    if not checksum_path.exists():
        checksum_path.write_bytes(fetch(f"{BASE}/{name}.CHECKSUM"))

    checksum_text = checksum_path.read_text(encoding="utf-8").strip()
    expected = checksum_text.split()[0].lower()
    actual = hashlib.sha256(zip_path.read_bytes()).hexdigest().lower()
    if actual != expected:
        raise RuntimeError(f"checksum mismatch for {name}: {actual} != {expected}")

    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.namelist() if m.lower().endswith(".csv")]
        if len(members) != 1:
            raise RuntimeError(f"expected one CSV in {name}, got {members}")
        raw = zf.read(members[0]).decode("utf-8")

    rows = list(csv.reader(io.StringIO(raw)))
    if not rows:
        raise RuntimeError(f"empty CSV in {name}")

    # Binance Vision kline archives may include a header row in newer files.
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

    numeric = columns
    for col in numeric:
        frame[col] = pd.to_numeric(frame[col], errors="raise")

    # 2024 Spot archive timestamps are milliseconds.
    frame["timestamp"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    return frame


def load_2024() -> pd.DataFrame:
    frames = [download_verified_month(2024, month) for month in range(1, 13)]
    data = pd.concat(frames, ignore_index=True).sort_values("timestamp").reset_index(drop=True)

    if data["timestamp"].duplicated().any():
        raise RuntimeError("duplicate timestamps in Binance data")

    expected_index = pd.date_range(
        "2024-01-01 00:00:00+00:00",
        "2024-12-31 23:00:00+00:00",
        freq="1h",
    )
    if len(data) != len(expected_index):
        raise RuntimeError(f"expected {len(expected_index)} rows, got {len(data)}")
    actual_index = pd.DatetimeIndex(data["timestamp"])
    if not actual_index.equals(expected_index):
        missing = expected_index.difference(actual_index)
        extra = actual_index.difference(expected_index)
        raise RuntimeError(
            f"timestamp continuity failed: missing={len(missing)} extra={len(extra)}"
        )

    return data


def prepare_features(data: pd.DataFrame) -> pd.DataFrame:
    # Kline taker-buy base volume is a genuine executed-flow field from Binance.
    feature_input = pd.DataFrame(
        {
            "close": data["close"].astype(float).to_numpy(),
            "high": data["high"].astype(float).to_numpy(),
            "low": data["low"].astype(float).to_numpy(),
            "volume": data["volume"].astype(float).to_numpy(),
            "buy_volume": data["taker_buy_base"].astype(float).to_numpy(),
            "sell_volume": (
                data["volume"].astype(float) - data["taker_buy_base"].astype(float)
            ).clip(lower=0.0).to_numpy(),
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


def backward_metrics(data: pd.DataFrame, i: int) -> dict[str, float | None]:
    close0 = float(data.at[i, "close"])

    def back(hours: int) -> float | None:
        if i < hours:
            return None
        return float(close0 / float(data.at[i - hours, "close"]) - 1.0)

    prior_24h_before_recent_6h = None
    if i >= 30:
        prior_24h_before_recent_6h = float(
            float(data.at[i - 6, "close"]) / float(data.at[i - 30, "close"]) - 1.0
        )

    return {
        "back_ret_3h": back(3),
        "back_ret_6h": back(6),
        "back_ret_12h": back(12),
        "back_ret_24h": back(24),
        "prior_24h_before_recent_6h": prior_24h_before_recent_6h,
    }


def summarize_events(events: list[dict[str, float]]) -> dict[str, float | int | None]:
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
            "drawdown_le_minus_1pct_24h_rate": None,
            "drawdown_le_minus_2pct_24h_rate": None,
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
        "drawdown_le_minus_1pct_24h_rate": float(np.mean(dd24 <= -0.01)),
        "drawdown_le_minus_2pct_24h_rate": float(np.mean(dd24 <= -0.02)),
        "mean_drawdown_24h": float(dd24.mean()),
    }


def main() -> None:
    data = load_2024()
    features = prepare_features(data)

    # Intentionally use project defaults. No tuning on BTC 2024.
    bocpd_config = BOCPDConfig()
    downtrend_config = DowntrendConfig()
    detector = DowntrendDetector(
        features.columns,
        bocpd_config=bocpd_config,
        config=downtrend_config,
    )

    raw_trigger_hours = 0
    cp_values: list[float] = []
    short_values: list[float] = []
    bearish_values: list[float] = []
    map_run_values: list[int] = []
    events: list[dict[str, float | int | str | bool]] = []
    previous_triggered = False
    last_event_i = -10_000
    cooldown_hours = 24

    for i, (_, row) in enumerate(features.iterrows()):
        signal = detector.update(row.to_dict())
        cp_values.append(signal.bocpd.changepoint_probability)
        short_values.append(signal.bocpd.short_run_probability)
        bearish_values.append(signal.bearish_score)
        map_run_values.append(signal.bocpd.map_run_length)
        if signal.triggered:
            raw_trigger_hours += 1

        rising_edge = signal.triggered and not previous_triggered
        has_future = i + 48 < len(data)
        cooled_down = i - last_event_i >= cooldown_hours

        if rising_edge and cooled_down and has_future:
            item = {
                "timestamp": data.at[i, "timestamp"].isoformat(),
                "close": float(data.at[i, "close"]),
                "cp_probability": signal.bocpd.changepoint_probability,
                "short_run_probability": signal.bocpd.short_run_probability,
                "bearish_score": signal.bearish_score,
                "map_run_length": signal.bocpd.map_run_length,
            }
            item.update(backward_metrics(data, i))
            item.update(forward_metrics(data, i))
            events.append(item)
            last_event_i = i

        previous_triggered = signal.triggered

    # Unconditional baseline over every eligible hour after detector warm-up.
    baseline: list[dict[str, float]] = []
    start_i = downtrend_config.min_observations
    for i in range(start_i, len(data) - 48):
        baseline.append(forward_metrics(data, i))

    event_summary = summarize_events(events)
    baseline_summary = summarize_events(baseline)

    def filtered_summary(predicate) -> dict[str, float | int | None]:
        return summarize_events([event for event in events if predicate(event)])

    causal_filter_summaries = {
        "back_6h_negative": filtered_summary(
            lambda e: e.get("back_ret_6h") is not None and e["back_ret_6h"] < 0.0
        ),
        "back_12h_negative": filtered_summary(
            lambda e: e.get("back_ret_12h") is not None and e["back_ret_12h"] < 0.0
        ),
        "back_6h_and_24h_negative": filtered_summary(
            lambda e: (
                e.get("back_ret_6h") is not None
                and e.get("back_ret_24h") is not None
                and e["back_ret_6h"] < 0.0
                and e["back_ret_24h"] < 0.0
            )
        ),
        "reversal_recent6_down_prior24_up": filtered_summary(
            lambda e: (
                e.get("back_ret_6h") is not None
                and e.get("prior_24h_before_recent_6h") is not None
                and e["back_ret_6h"] < 0.0
                and e["prior_24h_before_recent_6h"] >= 0.0
            )
        ),
        "back_6h_le_minus_1pct": filtered_summary(
            lambda e: e.get("back_ret_6h") is not None and e["back_ret_6h"] <= -0.01
        ),
    }

    def lift(metric: str) -> float | None:
        s = event_summary.get(metric)
        b = baseline_summary.get(metric)
        if s is None or b in (None, 0):
            return None
        return float(s) / float(b)

    def distribution(values: list[float]) -> dict[str, float]:
        a = np.asarray(values, dtype=float)
        return {
            "q50": float(np.quantile(a, 0.50)),
            "q90": float(np.quantile(a, 0.90)),
            "q95": float(np.quantile(a, 0.95)),
            "q99": float(np.quantile(a, 0.99)),
            "q999": float(np.quantile(a, 0.999)),
            "max": float(np.max(a)),
        }

    diagnostics = {
        "changepoint_probability": distribution(cp_values),
        "short_run_probability": distribution(short_values),
        "bearish_score": distribution(bearish_values),
        "max_map_run_length": int(max(map_run_values)),
        "counts": {
            "cp_ge_005": int(np.sum(np.asarray(cp_values) >= 0.05)),
            "cp_ge_010": int(np.sum(np.asarray(cp_values) >= 0.10)),
            "cp_ge_020": int(np.sum(np.asarray(cp_values) >= 0.20)),
            "short_ge_010": int(np.sum(np.asarray(short_values) >= 0.10)),
            "short_ge_020": int(np.sum(np.asarray(short_values) >= 0.20)),
            "short_ge_030": int(np.sum(np.asarray(short_values) >= 0.30)),
            "short_ge_060": int(np.sum(np.asarray(short_values) >= 0.60)),
            "bearish_ge_055": int(np.sum(np.asarray(bearish_values) >= 0.55)),
            "bearish_ge_060": int(np.sum(np.asarray(bearish_values) >= 0.60)),
            "bearish_ge_065": int(np.sum(np.asarray(bearish_values) >= 0.65)),
            "joint_short020_bearish060": int(np.sum(
                (np.asarray(short_values) >= 0.20) & (np.asarray(bearish_values) >= 0.60)
            )),
            "joint_short010_bearish060": int(np.sum(
                (np.asarray(short_values) >= 0.10) & (np.asarray(bearish_values) >= 0.60)
            )),
        },
    }

    result = {
        "dataset": {
            "venue": "Binance Spot",
            "symbol": "BTCUSDT",
            "interval": "1h",
            "start": str(data.at[0, "timestamp"]),
            "end": str(data.at[len(data) - 1, "timestamp"]),
            "rows": int(len(data)),
            "source": "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1h/",
            "checksums_verified": True,
        },
        "test_protocol": {
            "parameters": "project defaults; no BTC-2024 tuning",
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
        "diagnostics": diagnostics,
        "causal_filter_summaries": causal_filter_summaries,
        "signal_summary": event_summary,
        "baseline_summary": baseline_summary,
        "lift_vs_baseline": {
            "negative_24h_rate": lift("negative_24h_rate"),
            "close_le_minus_1pct_24h_rate": lift("close_le_minus_1pct_24h_rate"),
            "close_le_minus_2pct_24h_rate": lift("close_le_minus_2pct_24h_rate"),
            "drawdown_le_minus_1pct_24h_rate": lift("drawdown_le_minus_1pct_24h_rate"),
            "drawdown_le_minus_2pct_24h_rate": lift("drawdown_le_minus_2pct_24h_rate"),
        },
        "events": events,
    }

    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("REAL_DATA_TEST_JSON_BEGIN")
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    print("REAL_DATA_TEST_JSON_END")


if __name__ == "__main__":
    main()
