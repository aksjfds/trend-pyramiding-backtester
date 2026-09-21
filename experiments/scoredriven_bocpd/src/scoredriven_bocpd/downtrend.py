from __future__ import annotations

from dataclasses import dataclass
import math
from collections.abc import Mapping, Sequence

import numpy as np
from numpy.typing import NDArray

from .detector import BOCPDConfig, BOCPDUpdate, MultivariateScoreDrivenBOCPD
from .features import EWMStandardizer


FloatArray = NDArray[np.float64]


DEFAULT_DIRECTION_WEIGHTS = {
    "log_return": 1.50,
    "order_imbalance": 1.00,
    "trade_flow_imbalance": 1.25,
    "open_interest_change": 0.30,
    "funding_rate": 0.15,
}


@dataclass(frozen=True)
class DowntrendConfig:
    short_run_threshold: float = 0.60
    max_recent_run_length: int = 5
    min_previous_run_length: int = 12
    bearish_threshold: float = 0.65
    min_direction_shift: float = 0.10
    min_previous_nonbearish_probability: float = 0.20
    require_recent_price_negative: bool = True
    direction_gain: float = 2.5
    scaler_alpha: float = 0.025
    scaler_clip: float = 8.0
    min_observations: int = 20

    def __post_init__(self) -> None:
        for name in ("short_run_threshold", "bearish_threshold"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.max_recent_run_length < 0:
            raise ValueError("max_recent_run_length must be >= 0")
        if self.min_previous_run_length < 1:
            raise ValueError("min_previous_run_length must be >= 1")
        if self.min_direction_shift < 0.0:
            raise ValueError("min_direction_shift must be >= 0")
        if not 0.0 <= self.min_previous_nonbearish_probability <= 1.0:
            raise ValueError(
                "min_previous_nonbearish_probability must be in [0, 1]"
            )
        if self.direction_gain <= 0.0:
            raise ValueError("direction_gain must be > 0")
        if self.min_observations < 1:
            raise ValueError("min_observations must be >= 1")


@dataclass(frozen=True)
class DowntrendSignal:
    observation_count: int
    change_detected: bool
    bearish_score: float
    triggered: bool
    recent_direction: float
    previous_direction: float
    direction_shift: float
    map_reset: bool
    directional_reversal: bool
    previous_price_nonbearish_probability: float
    recent_price_direction: float
    standardized_features: FloatArray
    bocpd: BOCPDUpdate


class DowntrendDetector:
    """BOCPD regime reset plus a post-change bearish direction classifier."""

    def __init__(
        self,
        feature_names: Sequence[str],
        *,
        bocpd_config: BOCPDConfig | None = None,
        config: DowntrendConfig | None = None,
        direction_weights: Mapping[str, float] | None = None,
    ) -> None:
        if len(feature_names) == 0:
            raise ValueError("feature_names cannot be empty")

        self.feature_names = tuple(feature_names)
        self.config = config or DowntrendConfig()
        weights = dict(DEFAULT_DIRECTION_WEIGHTS)
        if direction_weights is not None:
            weights.update(direction_weights)

        self._weights = np.array(
            [float(weights.get(name, 0.0)) for name in self.feature_names],
            dtype=np.float64,
        )
        if np.allclose(self._weights, 0.0):
            raise ValueError(
                "no directional feature has a non-zero weight; "
                "provide direction_weights"
            )
        self._weight_norm = float(np.sum(np.abs(self._weights)))
        self._price_index = (
            self.feature_names.index("log_return")
            if "log_return" in self.feature_names
            else None
        )
        self._scaler = EWMStandardizer(
            len(self.feature_names),
            alpha=self.config.scaler_alpha,
            clip=self.config.scaler_clip,
            center=False,
        )
        self._detector = MultivariateScoreDrivenBOCPD(
            len(self.feature_names),
            bocpd_config,
        )
        self._count = 0

    def reset(self) -> None:
        self._count = 0
        self._scaler.reset()
        self._detector.reset()

    def _vector(self, features: Mapping[str, float] | Sequence[float]) -> FloatArray:
        if isinstance(features, Mapping):
            return np.array(
                [float(features[name]) for name in self.feature_names],
                dtype=np.float64,
            )
        x = np.asarray(features, dtype=np.float64)
        if x.shape != (len(self.feature_names),):
            raise ValueError(
                f"expected {len(self.feature_names)} features, got shape {x.shape}"
            )
        return x

    def update(
        self, features: Mapping[str, float] | Sequence[float]
    ) -> DowntrendSignal:
        raw = self._vector(features)
        standardized = self._scaler.transform(raw)
        update = self._detector.update(standardized)
        self._count += 1

        recent_direction = (
            float(np.dot(self._weights, update.recent_regime_mean))
            / self._weight_norm
        )
        previous_direction = (
            float(np.dot(self._weights, update.previous_regime_mean))
            / self._weight_norm
        )
        instant_direction = (
            float(np.dot(self._weights, standardized))
            / self._weight_norm
        )
        direction = 0.80 * recent_direction + 0.20 * instant_direction
        bearish_score = 1.0 / (
            1.0 + math.exp(self.config.direction_gain * direction)
        )

        # The source methodology identifies regimes from the most likely
        # run-length path. Require an actual reset from a mature regime into a
        # recent regime, supported by posterior mass on recent run lengths.
        map_reset = (
            update.previous_map_run_length >= self.config.min_previous_run_length
            and update.map_run_length <= self.config.max_recent_run_length
        )
        direction_shift = previous_direction - recent_direction

        if self._price_index is None:
            previous_price_nonbearish_probability = 1.0
            recent_price_direction = 0.0
            price_context_ok = True
        else:
            price_idx = self._price_index
            previous_price_mean = float(update.previous_regime_mean[price_idx])
            previous_price_std = max(
                float(update.previous_regime_mean_std[price_idx]),
                np.finfo(float).tiny,
            )
            previous_price_z = previous_price_mean / previous_price_std
            previous_price_nonbearish_probability = 0.5 * (
                1.0 + math.erf(previous_price_z / math.sqrt(2.0))
            )
            recent_price_direction = float(update.recent_regime_mean[price_idx])
            price_context_ok = (
                previous_price_nonbearish_probability
                >= self.config.min_previous_nonbearish_probability
                and (
                    not self.config.require_recent_price_negative
                    or recent_price_direction < 0.0
                )
            )

        directional_reversal = (
            recent_direction < 0.0
            and direction_shift >= self.config.min_direction_shift
            and price_context_ok
        )
        change_detected = (
            self._count >= self.config.min_observations
            and map_reset
            and directional_reversal
            and update.short_run_probability >= self.config.short_run_threshold
        )
        triggered = (
            change_detected
            and bearish_score >= self.config.bearish_threshold
        )

        return DowntrendSignal(
            observation_count=self._count,
            change_detected=change_detected,
            bearish_score=bearish_score,
            triggered=triggered,
            recent_direction=recent_direction,
            previous_direction=previous_direction,
            direction_shift=direction_shift,
            map_reset=map_reset,
            directional_reversal=directional_reversal,
            previous_price_nonbearish_probability=(
                previous_price_nonbearish_probability
            ),
            recent_price_direction=recent_price_direction,
            standardized_features=standardized.copy(),
            bocpd=update,
        )
