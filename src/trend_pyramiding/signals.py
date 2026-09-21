from __future__ import annotations

import pandas as pd

from .indicators import ema, prior_rolling_high


def breakout_long_signal(
    frame: pd.DataFrame,
    ema_period: int,
    breakout_lookback: int,
) -> pd.Series:
    """Placeholder direction signal; execution/risk management is the project focus."""
    trend = frame["close"] > ema(frame["close"], ema_period)
    breakout = frame["close"] > prior_rolling_high(frame["high"], breakout_lookback)
    return (trend & breakout).fillna(False)


def resolve_entry_signal(
    frame: pd.DataFrame,
    signal_column: str | None,
    ema_period: int,
    breakout_lookback: int,
) -> pd.Series:
    if signal_column:
        if signal_column not in frame.columns:
            raise ValueError(f"signal column not found: {signal_column}")
        return frame[signal_column].fillna(False).astype(bool)
    return breakout_long_signal(frame, ema_period, breakout_lookback)


def strong_close_signal(frame: pd.DataFrame) -> pd.Series:
    """A closed candle finishes in its upper half; no market-specific threshold."""
    return (frame["close"] >= (frame["high"] + frame["low"]) / 2).fillna(False)
