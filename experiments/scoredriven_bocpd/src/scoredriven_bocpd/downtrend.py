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
    changepoint_threshold: float = 0.20
    short_run_threshold: float = 0.60
    bearish_threshold: float = 0.65
    direction_gain: float = 2.5
    scaler_alpha: float = 0.025
    scaler_clip: float = 8.0
    min_observations: int = 20

    def __post_init__(self) -> None:
        for name in (
            "changepoint_threshold",
            "short_run_threshold",
            "bearish_threshold",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
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
    standardized_features: FloatArray
    bocpd: BOCPDUpdate


class DowntrendDetector:
    """BOCPD regime-shift detector plus a post-change direction classifier.

    The BOCPD layer answers "did the data-generating regime change?".
    Direction is deliberately separated and computed from directional
    microstructure features so that volatility/spread shocks are not
    automatically mislabeled as bearish.
    """

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
        self._scaler = EWMStandardizer(
            len(self.feature_names),
            alpha=self.config.scaler_alpha,
            clip=self.config.scaler_clip,
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

        # Direction is a weighted average of the inferred regime location and
        # the newest observation. The new observation gives early response;
        # the regime estimate prevents one isolated print from dominating.
        regime_direction = float(np.dot(self._weights, update.regime_mean)) / self._weight_norm
        instant_direction = float(np.dot(self._weights, standardized)) / self._weight_norm
        direction = 0.70 * regime_direction + 0.30 * instant_direction
        bearish_score = 1.0 / (1.0 + math.exp(self.config.direction_gain * direction))

        change_detected = (
            self._count >= self.config.min_observations
            and (
                update.changepoint_probability >= self.config.changepoint_threshold
                or update.short_run_probability >= self.config.short_run_threshold
            )
        )
        triggered = change_detected and bearish_score >= self.config.bearish_threshold

        return DowntrendSignal(
            observation_count=self._count,
            change_detected=change_detected,
            bearish_score=bearish_score,
            triggered=triggered,
            standardized_features=standardized.copy(),
            bocpd=update,
        )
