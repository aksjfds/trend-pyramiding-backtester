from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    if period < 1:
        raise ValueError("period must be >= 1")
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder-style ATR using only current and past bars."""
    if period < 1:
        raise ValueError("period must be >= 1")
    prev_close = frame["close"].shift(1)
    tr = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - prev_close).abs(),
            (frame["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def prior_rolling_high(series: pd.Series, lookback: int) -> pd.Series:
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    return series.rolling(lookback, min_periods=lookback).max().shift(1)


def rolling_structure_low(series: pd.Series, lookback: int) -> pd.Series:
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    return series.rolling(lookback, min_periods=lookback).min()


def completed_timeframe_trend_filter(
    frame: pd.DataFrame,
    *,
    timeframe: str,
    ema_period: int,
    slope_lookback: int,
) -> pd.Series:
    """Trend filter aligned only to fully completed higher-timeframe candles.

    Input timestamps are treated as bar-open timestamps. A bar's close time is
    inferred from the median source-bar spacing. Higher-timeframe candles are
    right-labelled by their close time, then merge_asof exposes only candles
    whose close time is <= the current source bar close time.
    """
    if ema_period < 1:
        raise ValueError("ema_period must be >= 1")
    if slope_lookback < 1:
        raise ValueError("slope_lookback must be >= 1")
    if len(frame) < 2:
        return pd.Series(False, index=frame.index, dtype=bool)

    timestamps = pd.to_datetime(frame["timestamp"], utc=True)
    diffs = timestamps.diff().dropna().dt.total_seconds()
    base_seconds = float(diffs.median())
    if not pd.notna(base_seconds) or base_seconds <= 0:
        raise ValueError("cannot infer source bar interval")
    bar_delta = pd.to_timedelta(base_seconds, unit="s")
    close_times = timestamps + bar_delta

    source = pd.DataFrame(
        {
            "close_time": close_times,
            "close": pd.to_numeric(frame["close"], errors="raise").to_numpy(),
        }
    ).set_index("close_time")

    try:
        higher = (
            source.resample(timeframe, closed="right", label="right", origin="epoch")
            .agg(close=("close", "last"))
            .dropna()
        )
    except ValueError as exc:
        raise ValueError(f"invalid trend_filter_timeframe: {timeframe}") from exc

    higher["trend_ema"] = ema(higher["close"], ema_period)
    higher["trend_ok"] = (
        (higher["close"] > higher["trend_ema"])
        & (higher["trend_ema"] > higher["trend_ema"].shift(slope_lookback))
    )

    aligned = pd.merge_asof(
        pd.DataFrame({"close_time": close_times}).sort_values("close_time"),
        higher[["trend_ok"]].reset_index().sort_values("close_time"),
        on="close_time",
        direction="backward",
        allow_exact_matches=True,
    )
    aligned.index = frame.index
    return aligned["trend_ok"].eq(True)
