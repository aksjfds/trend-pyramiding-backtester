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
