from __future__ import annotations

import pandas as pd

from .indicators import ema, prior_rolling_high


def breakout_long_signal(
    frame: pd.DataFrame,
    ema_period: int,
    breakout_lookback: int,
    trend_filter: pd.Series | None = None,
) -> pd.Series:
    """Default long signal: 1H breakout gated by a completed slow trend filter."""
    local_trend = frame["close"] > ema(frame["close"], ema_period)
    breakout = frame["close"] > prior_rolling_high(frame["high"], breakout_lookback)
    signal = local_trend & breakout
    if trend_filter is not None:
        signal = signal & trend_filter.reindex(frame.index, fill_value=False)
    return signal.fillna(False)


def resolve_entry_signal(
    frame: pd.DataFrame,
    signal_column: str | None,
    ema_period: int,
    breakout_lookback: int,
    trend_filter: pd.Series | None = None,
) -> pd.Series:
    if signal_column:
        if signal_column not in frame.columns:
            raise ValueError(f"signal column not found: {signal_column}")
        return frame[signal_column].fillna(False).astype(bool)
    return breakout_long_signal(
        frame,
        ema_period,
        breakout_lookback,
        trend_filter=trend_filter,
    )
