from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from numpy.typing import NDArray


FloatArray = NDArray[np.float64]


@dataclass
class EWMStandardizer:
    """Causal exponentially weighted standardizer for streaming features."""

    dimension: int
    alpha: float = 0.025
    clip: float = 8.0
    epsilon: float = 1e-8

    def __post_init__(self) -> None:
        if self.dimension < 1:
            raise ValueError("dimension must be >= 1")
        if not 0.0 < self.alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")
        self.reset()

    def reset(self) -> None:
        self._initialized = False
        self._mean = np.zeros(self.dimension, dtype=np.float64)
        self._variance = np.ones(self.dimension, dtype=np.float64)

    def transform(self, values: FloatArray | list[float]) -> FloatArray:
        x = np.asarray(values, dtype=np.float64)
        if x.shape != (self.dimension,):
            raise ValueError(f"expected shape ({self.dimension},), got {x.shape}")
        if not np.all(np.isfinite(x)):
            raise ValueError("features contain NaN or infinity")

        if not self._initialized:
            self._mean = x.copy()
            self._variance.fill(1.0)
            self._initialized = True
            return np.zeros_like(x)

        std = np.sqrt(np.maximum(self._variance, self.epsilon))
        z = np.clip((x - self._mean) / std, -self.clip, self.clip)

        delta = x - self._mean
        self._mean = self._mean + self.alpha * delta
        # Stable EW variance recursion using the pre-update delta.
        self._variance = (1.0 - self.alpha) * (
            self._variance + self.alpha * delta * delta
        )
        self._variance = np.maximum(self._variance, self.epsilon)
        return z


@dataclass(frozen=True)
class MarketFeatureBuilder:
    """Build causal bar/microstructure features from a pandas DataFrame.

    Required:
      close

    Optional:
      high, low, volume
      bid_price, ask_price, bid_size, ask_size
      buy_volume, sell_volume
      open_interest, funding_rate
    """

    volatility_window: int = 32

    def __post_init__(self) -> None:
        if self.volatility_window < 2:
            raise ValueError("volatility_window must be >= 2")

    @staticmethod
    def _safe_ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
        denominator = denominator.replace(0.0, np.nan)
        return (numerator / denominator).replace([np.inf, -np.inf], np.nan)

    def build(self, frame: pd.DataFrame) -> pd.DataFrame:
        if "close" not in frame.columns:
            raise ValueError("input frame must contain 'close'")

        close = frame["close"].astype(float).clip(lower=np.finfo(float).tiny)
        log_return = np.log(close).diff()

        features: dict[str, pd.Series] = {
            "log_return": log_return,
            "realized_vol": np.sqrt(
                log_return.pow(2)
                .rolling(self.volatility_window, min_periods=2)
                .mean()
            ),
        }

        if {"high", "low"}.issubset(frame.columns):
            high = frame["high"].astype(float)
            low = frame["low"].astype(float)
            features["range_bps"] = self._safe_ratio(high - low, close) * 10_000.0

        if "volume" in frame.columns:
            volume = frame["volume"].astype(float).clip(lower=0.0)
            features["log_volume_change"] = np.log1p(volume).diff()

        if {"bid_price", "ask_price"}.issubset(frame.columns):
            bid = frame["bid_price"].astype(float)
            ask = frame["ask_price"].astype(float)
            midpoint = (bid + ask) / 2.0
            features["spread_bps"] = self._safe_ratio(ask - bid, midpoint) * 10_000.0

        if {"bid_size", "ask_size"}.issubset(frame.columns):
            bid_size = frame["bid_size"].astype(float)
            ask_size = frame["ask_size"].astype(float)
            total = bid_size + ask_size
            features["order_imbalance"] = self._safe_ratio(
                bid_size - ask_size, total
            )

        if {"buy_volume", "sell_volume"}.issubset(frame.columns):
            buy = frame["buy_volume"].astype(float)
            sell = frame["sell_volume"].astype(float)
            total = buy + sell
            features["trade_flow_imbalance"] = self._safe_ratio(buy - sell, total)

        if "open_interest" in frame.columns:
            oi = frame["open_interest"].astype(float).replace(0.0, np.nan)
            features["open_interest_change"] = oi.pct_change(fill_method=None)

        if "funding_rate" in frame.columns:
            features["funding_rate"] = frame["funding_rate"].astype(float)

        result = pd.DataFrame(features, index=frame.index)
        return result.replace([np.inf, -np.inf], np.nan).fillna(0.0)
