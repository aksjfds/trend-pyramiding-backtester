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
    """Configuration for the online detector.

    hazard_lambda is the expected regime length in observations under a
    constant hazard model. The observation model is a diagonal multivariate
    Student-t with score-driven location, scale and AR(1) coefficient.
    """

    hazard_lambda: float = 120.0
    max_run_length: int = 256
    student_df: float = 5.0
    location_step: float = 0.08
    scale_step: float = 0.03
    ar_step: float = 0.03
    ar_beta: float = 0.97
    phi_max: float = 0.98
    min_log_scale: float = -4.0
    max_log_scale: float = 4.0
    short_run_window: int = 5

    def __post_init__(self) -> None:
        if self.hazard_lambda <= 1.0:
            raise ValueError("hazard_lambda must be > 1")
        if self.max_run_length < 2:
            raise ValueError("max_run_length must be >= 2")
        if self.student_df <= 2.0:
            raise ValueError("student_df must be > 2")
        if not 0.0 < self.ar_beta <= 1.0:
            raise ValueError("ar_beta must be in (0, 1]")
        if not 0.0 < self.phi_max < 1.0:
            raise ValueError("phi_max must be in (0, 1)")
        if self.short_run_window < 0:
            raise ValueError("short_run_window must be >= 0")


@dataclass
class _HypothesisState:
    mean: FloatArray
    log_scale: FloatArray
    ar_latent: FloatArray
    previous: FloatArray | None = None

    def copy(self) -> "_HypothesisState":
        return _HypothesisState(
            mean=self.mean.copy(),
            log_scale=self.log_scale.copy(),
            ar_latent=self.ar_latent.copy(),
            previous=None if self.previous is None else self.previous.copy(),
        )


@dataclass(frozen=True)
class BOCPDUpdate:
    changepoint_probability: float
    short_run_probability: float
    map_run_length: int
    posterior: FloatArray
    regime_mean: FloatArray
    regime_scale: FloatArray
    ar_coefficient: FloatArray


class MultivariateScoreDrivenBOCPD:
    """Online BOCPD with robust score-driven AR dynamics.

    Each run-length hypothesis owns a separate observation-model state. The
    multivariate likelihood is diagonal Student-t: this deliberately avoids
    estimating a dense covariance matrix online when order-book features are
    numerous or collinear. Dependence through time is modeled by a bounded
    AR(1) coefficient per feature.

    This is a practical multivariate extension of score-driven autoregressive
    BOCPD, not a verbatim implementation of any single paper.
    """

    def __init__(self, dimension: int, config: BOCPDConfig | None = None) -> None:
        if dimension < 1:
            raise ValueError("dimension must be >= 1")
        self.dimension = dimension
        self.config = config or BOCPDConfig()
        self._hazard = 1.0 / self.config.hazard_lambda
        self._base = _HypothesisState(
            mean=np.zeros(dimension, dtype=np.float64),
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

    def _predictive_logpdf(self, state: _HypothesisState, x: FloatArray) -> float:
        cfg = self.config
        scale = np.exp(state.log_scale)
        phi = cfg.phi_max * np.tanh(state.ar_latent)

        if state.previous is None:
            location = state.mean
        else:
            location = state.mean + phi * (state.previous - state.mean)

        z = (x - location) / scale
        nu = cfg.student_df
        log_c = (
            math.lgamma((nu + 1.0) / 2.0)
            - math.lgamma(nu / 2.0)
            - 0.5 * math.log(nu * math.pi)
        )
        terms = log_c - np.log(scale) - ((nu + 1.0) / 2.0) * np.log1p((z * z) / nu)
        return float(np.sum(terms))

    def _updated_state(self, state: _HypothesisState, x: FloatArray) -> _HypothesisState:
        cfg = self.config
        out = state.copy()
        scale = np.exp(out.log_scale)
        phi = cfg.phi_max * np.tanh(out.ar_latent)

        if out.previous is None:
            location = out.mean
            centered_previous = np.zeros_like(x)
        else:
            centered_previous = (out.previous - out.mean) / scale
            location = out.mean + phi * (out.previous - out.mean)

        z = (x - location) / scale
        nu = cfg.student_df

        # Natural-score-like bounded updates for Student-t location and log scale.
        location_score = ((nu + 1.0) * z) / (nu + z * z)
        scale_score = -1.0 + ((nu + 1.0) * z * z) / (nu + z * z)

        # Score contribution of the AR coefficient; centering and Student-t
        # weighting stop isolated prints from exploding the recursion.
        ar_score = location_score * centered_previous

        out.mean = out.mean + cfg.location_step * scale * location_score
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

        cfg = self.config
        predictive = np.array(
            [self._predictive_logpdf(state, x) for state in self._states],
            dtype=np.float64,
        )
        prior_predictive = self._predictive_logpdf(self._base, x)

        # Classic BOCPD recursion:
        # r_t=0 uses the fresh-regime prior predictive; growth branches use
        # the predictive density of their surviving regime hypothesis.
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

        # Saturating run-length cap: probability mass is preserved instead of
        # silently throwing away an old, long-lived regime.
        cap = cfg.max_run_length
        if len(new_states) > cap + 1:
            probabilities = np.exp(new_log_mass)
            merged = probabilities[cap] + probabilities[cap + 1]
            if probabilities[cap + 1] > probabilities[cap]:
                new_states[cap] = new_states[cap + 1]
            probabilities = probabilities[: cap + 1]
            probabilities[cap] = merged
            probabilities /= probabilities.sum()
            new_log_mass = np.log(np.maximum(probabilities, np.finfo(float).tiny))
            new_states = new_states[: cap + 1]

        self._states = new_states
        self._log_posterior = new_log_mass

        posterior = np.exp(new_log_mass)
        map_run = int(np.argmax(posterior))
        map_state = self._states[map_run]
        short_end = min(cfg.short_run_window + 1, len(posterior))

        return BOCPDUpdate(
            changepoint_probability=float(posterior[0]),
            short_run_probability=float(posterior[:short_end].sum()),
            map_run_length=map_run,
            posterior=posterior.copy(),
            regime_mean=map_state.mean.copy(),
            regime_scale=np.exp(map_state.log_scale.copy()),
            ar_coefficient=cfg.phi_max * np.tanh(map_state.ar_latent.copy()),
        )
