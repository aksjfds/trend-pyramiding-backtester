from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
from numpy.typing import NDArray


FloatArray = NDArray[np.float64]


def _logsumexp(values: FloatArray) -> float:
    m = float(np.max(values))
    if not np.isfinite(m):
        return m
    return m + math.log(float(np.exp(values - m).sum()))


@dataclass(frozen=True)
class BOCPDConfig:
    """Configuration for robust score-driven AR(1) BOCPD.

    The regime mean is Bayesian and constant within a run-length hypothesis.
    Scale and AR correlation remain observation-driven/time-varying.
    """

    hazard_lambda: float = 120.0
    max_run_length: int = 256
    student_df: float = 5.0
    mean_prior_variance: float = 1.0
    scale_step: float = 0.03
    ar_step: float = 0.03
    ar_beta: float = 0.97
    phi_max: float = 0.98
    min_log_scale: float = -4.0
    max_log_scale: float = 4.0
    short_run_window: int = 5
    min_conditional_variance_ratio: float = 1e-3

    def __post_init__(self) -> None:
        if self.hazard_lambda <= 1.0:
            raise ValueError("hazard_lambda must be > 1")
        if self.max_run_length < 2:
            raise ValueError("max_run_length must be >= 2")
        if self.student_df <= 2.0:
            raise ValueError("student_df must be > 2")
        if self.mean_prior_variance <= 0.0:
            raise ValueError("mean_prior_variance must be > 0")
        if not 0.0 < self.ar_beta <= 1.0:
            raise ValueError("ar_beta must be in (0, 1]")
        if not 0.0 < self.phi_max < 1.0:
            raise ValueError("phi_max must be in (0, 1)")
        if self.short_run_window < 0:
            raise ValueError("short_run_window must be >= 0")
        if not 0.0 < self.min_conditional_variance_ratio <= 1.0:
            raise ValueError("min_conditional_variance_ratio must be in (0, 1]")


@dataclass
class _HypothesisState:
    mean: FloatArray
    mean_precision: FloatArray
    log_scale: FloatArray
    ar_latent: FloatArray
    previous: FloatArray | None = None

    def copy(self) -> "_HypothesisState":
        return _HypothesisState(
            mean=self.mean.copy(),
            mean_precision=self.mean_precision.copy(),
            log_scale=self.log_scale.copy(),
            ar_latent=self.ar_latent.copy(),
            previous=None if self.previous is None else self.previous.copy(),
        )


@dataclass(frozen=True)
class BOCPDUpdate:
    changepoint_probability: float
    short_run_probability: float
    map_run_length: int
    previous_map_run_length: int
    run_length_drop: float
    posterior: FloatArray
    regime_mean: FloatArray
    recent_regime_mean: FloatArray
    previous_regime_mean: FloatArray
    previous_regime_mean_std: FloatArray
    regime_mean_std: FloatArray
    regime_scale: FloatArray
    ar_coefficient: FloatArray


class MultivariateScoreDrivenBOCPD:
    """Online BOCPD with Bayesian regime means and score-driven AR dynamics.

    Each run-length hypothesis owns a separate state. The observation model is
    a diagonal multivariate Student-t extension. Regime means are updated via
    Bayesian precision accumulation; scale and AR correlation use robust GAS
    updates. This preserves mean breaks instead of allowing a constant-gain
    location filter to absorb them.
    """

    def __init__(self, dimension: int, config: BOCPDConfig | None = None) -> None:
        if dimension < 1:
            raise ValueError("dimension must be >= 1")
        self.dimension = dimension
        self.config = config or BOCPDConfig()
        self._hazard = 1.0 / self.config.hazard_lambda
        prior_precision = 1.0 / self.config.mean_prior_variance
        self._base = _HypothesisState(
            mean=np.zeros(dimension, dtype=np.float64),
            mean_precision=np.full(dimension, prior_precision, dtype=np.float64),
            log_scale=np.zeros(dimension, dtype=np.float64),
            ar_latent=np.zeros(dimension, dtype=np.float64),
        )
        self.reset()

    def reset(self) -> None:
        self._states = [self._base.copy()]
        self._log_posterior = np.array([0.0], dtype=np.float64)

    @property
    def posterior(self) -> FloatArray:
        return np.exp(self._log_posterior.copy())

    def _components(
        self, state: _HypothesisState
    ) -> tuple[FloatArray, FloatArray, FloatArray, FloatArray]:
        cfg = self.config
        unconditional_scale = np.exp(state.log_scale)
        phi = cfg.phi_max * np.tanh(state.ar_latent)

        if state.previous is None:
            loading = np.ones(self.dimension, dtype=np.float64)
            location = state.mean
            innovation_variance = unconditional_scale * unconditional_scale
        else:
            loading = 1.0 - phi
            location = state.mean + phi * (state.previous - state.mean)
            variance_ratio = np.maximum(
                1.0 - phi * phi,
                cfg.min_conditional_variance_ratio,
            )
            innovation_variance = (
                unconditional_scale * unconditional_scale * variance_ratio
            )

        return location, innovation_variance, loading, phi

    def _predictive_logpdf(self, state: _HypothesisState, x: FloatArray) -> float:
        location, innovation_variance, loading, _ = self._components(state)

        mean_variance = 1.0 / state.mean_precision
        predictive_variance = innovation_variance + (
            loading * loading * mean_variance
        )
        predictive_scale = np.sqrt(
            np.maximum(predictive_variance, np.finfo(float).tiny)
        )

        z = (x - location) / predictive_scale
        nu = self.config.student_df
        log_c = (
            math.lgamma((nu + 1.0) / 2.0)
            - math.lgamma(nu / 2.0)
            - 0.5 * math.log(nu * math.pi)
        )
        terms = (
            log_c
            - np.log(predictive_scale)
            - ((nu + 1.0) / 2.0) * np.log1p((z * z) / nu)
        )
        return float(np.sum(terms))

    def _updated_state(self, state: _HypothesisState, x: FloatArray) -> _HypothesisState:
        cfg = self.config
        out = state.copy()

        location, innovation_variance, loading, phi = self._components(out)
        innovation_scale = np.sqrt(
            np.maximum(innovation_variance, np.finfo(float).tiny)
        )
        residual = x - location
        z = residual / innovation_scale
        nu = cfg.student_df

        # Student-t latent precision weight: large isolated residuals contribute
        # less information to the regime mean than ordinary observations.
        robust_weight = np.clip(
            (nu + 1.0) / (nu + z * z),
            0.05,
            1.20,
        )

        if out.previous is None:
            transformed = x
        else:
            transformed = x - phi * out.previous

        obs_precision = (
            robust_weight * loading * loading / innovation_variance
        )
        new_precision = out.mean_precision + obs_precision
        information = (
            out.mean_precision * out.mean
            + robust_weight * loading * transformed / innovation_variance
        )
        out.mean = information / np.maximum(new_precision, np.finfo(float).tiny)
        out.mean_precision = new_precision

        # GAS updates are reserved for non-mean dynamics, matching the source
        # model's separation between regime mean and time-varying moments.
        location_score = ((nu + 1.0) * z) / (nu + z * z)
        scale_score = -1.0 + ((nu + 1.0) * z * z) / (nu + z * z)

        if out.previous is None:
            ar_score = np.zeros_like(x)
        else:
            centered_previous = (
                out.previous - state.mean
            ) / np.maximum(innovation_scale, np.finfo(float).tiny)
            ar_score = location_score * centered_previous

        out.log_scale = np.clip(
            out.log_scale + cfg.scale_step * scale_score,
            cfg.min_log_scale,
            cfg.max_log_scale,
        )
        out.ar_latent = np.clip(
            cfg.ar_beta * out.ar_latent + cfg.ar_step * ar_score,
            -3.0,
            3.0,
        )
        out.previous = x.copy()
        return out

    def update(self, observation: FloatArray | list[float]) -> BOCPDUpdate:
        x = np.asarray(observation, dtype=np.float64)
        if x.shape != (self.dimension,):
            raise ValueError(f"expected shape ({self.dimension},), got {x.shape}")
        if not np.all(np.isfinite(x)):
            raise ValueError("observation contains NaN or infinity")

        previous_posterior = np.exp(self._log_posterior)
        previous_map = int(np.argmax(previous_posterior))
        previous_regime_mean = self._states[previous_map].mean.copy()
        previous_regime_mean_std = np.sqrt(
            1.0 / self._states[previous_map].mean_precision
        )

        predictive = np.array(
            [self._predictive_logpdf(state, x) for state in self._states],
            dtype=np.float64,
        )
        prior_predictive = self._predictive_logpdf(self._base, x)

        cp_log_mass = _logsumexp(
            self._log_posterior
            + math.log(self._hazard)
            + prior_predictive
        )
        growth_log_mass = (
            self._log_posterior
            + math.log1p(-self._hazard)
            + predictive
        )

        new_log_mass = np.concatenate(
            [np.array([cp_log_mass], dtype=np.float64), growth_log_mass]
        )
        new_log_mass -= _logsumexp(new_log_mass)

        new_states = [self._updated_state(self._base, x)]
        new_states.extend(self._updated_state(state, x) for state in self._states)

        cap = self.config.max_run_length
        if len(new_states) > cap + 1:
            probabilities = np.exp(new_log_mass)
            merged = probabilities[cap] + probabilities[cap + 1]
            if probabilities[cap + 1] > probabilities[cap]:
                new_states[cap] = new_states[cap + 1]
            probabilities = probabilities[: cap + 1]
            probabilities[cap] = merged
            probabilities /= probabilities.sum()
            new_log_mass = np.log(
                np.maximum(probabilities, np.finfo(float).tiny)
            )
            new_states = new_states[: cap + 1]

        self._states = new_states
        self._log_posterior = new_log_mass

        posterior = np.exp(new_log_mass)
        map_run = int(np.argmax(posterior))
        map_state = self._states[map_run]
        short_end = min(self.config.short_run_window + 1, len(posterior))
        short_probability = float(
            np.clip(posterior[:short_end].sum(), 0.0, 1.0)
        )

        if short_probability > 0.0:
            short_weights = posterior[:short_end] / short_probability
            short_means = np.stack(
                [state.mean for state in self._states[:short_end]], axis=0
            )
            recent_mean = np.sum(
                short_weights[:, None] * short_means,
                axis=0,
            )
        else:
            recent_mean = map_state.mean.copy()

        expected_growth = min(previous_map + 1, cap)
        if expected_growth > 0 and map_run < expected_growth:
            run_length_drop = (
                expected_growth - map_run
            ) / expected_growth
        else:
            run_length_drop = 0.0

        return BOCPDUpdate(
            changepoint_probability=float(posterior[0]),
            short_run_probability=short_probability,
            map_run_length=map_run,
            previous_map_run_length=previous_map,
            run_length_drop=float(np.clip(run_length_drop, 0.0, 1.0)),
            posterior=posterior.copy(),
            regime_mean=map_state.mean.copy(),
            recent_regime_mean=recent_mean.copy(),
            previous_regime_mean=previous_regime_mean,
            previous_regime_mean_std=previous_regime_mean_std,
            regime_mean_std=np.sqrt(1.0 / map_state.mean_precision),
            regime_scale=np.exp(map_state.log_scale.copy()),
            ar_coefficient=self.config.phi_max
            * np.tanh(map_state.ar_latent.copy()),
        )
