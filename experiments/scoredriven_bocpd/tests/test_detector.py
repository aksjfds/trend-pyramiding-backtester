from __future__ import annotations

import numpy as np
import pandas as pd

from scoredriven_bocpd import (
    BOCPDConfig,
    DowntrendConfig,
    DowntrendDetector,
    MarketFeatureBuilder,
    MultivariateScoreDrivenBOCPD,
)


def test_posterior_is_normalized_and_bounded() -> None:
    rng = np.random.default_rng(1)
    detector = MultivariateScoreDrivenBOCPD(
        4,
        BOCPDConfig(hazard_lambda=100, max_run_length=32),
    )

    for _ in range(200):
        result = detector.update(rng.normal(0.0, 1.0, 4))
        assert np.isclose(result.posterior.sum(), 1.0, atol=1e-12)
        assert len(result.posterior) <= 33
        assert 0.0 <= result.changepoint_probability <= 1.0
        assert 0.0 <= result.short_run_probability <= 1.0


def test_detects_strong_multivariate_downshift() -> None:
    rng = np.random.default_rng(2)
    detector = MultivariateScoreDrivenBOCPD(
        3,
        BOCPDConfig(
            hazard_lambda=80,
            max_run_length=192,
            short_run_window=5,
        ),
    )

    cp = []
    short = []
    map_run = []
    drops = []
    for i in range(160):
        mean = 0.5 if i < 80 else -3.0
        result = detector.update(rng.normal(mean, 0.30, 3))
        cp.append(result.changepoint_probability)
        short.append(result.short_run_probability)
        map_run.append(result.map_run_length)
        drops.append(result.run_length_drop)

    assert max(cp[78:90]) > 0.05
    assert max(short[78:90]) > 0.80
    assert min(map_run[80:86]) <= 5
    assert max(drops[80:86]) > 0.80


def test_downtrend_wrapper_fires_after_regime_change() -> None:
    rng = np.random.default_rng(3)
    names = ("log_return", "order_imbalance", "trade_flow_imbalance")
    detector = DowntrendDetector(
        names,
        bocpd_config=BOCPDConfig(
            hazard_lambda=70,
            max_run_length=160,
            short_run_window=5,
        ),
        config=DowntrendConfig(
            short_run_threshold=0.60,
            max_recent_run_length=5,
            min_previous_run_length=12,
            bearish_threshold=0.60,
            min_observations=20,
        ),
    )

    fired = []
    for i in range(140):
        mean = 0.4 if i < 70 else -2.5
        x = rng.normal(mean, 0.20, len(names))
        signal = detector.update(x)
        fired.append(signal.triggered)

    assert any(fired[70:85])



def test_regime_mean_uncertainty_shrinks_on_stable_data() -> None:
    rng = np.random.default_rng(11)
    detector = MultivariateScoreDrivenBOCPD(
        2,
        BOCPDConfig(hazard_lambda=1000, max_run_length=128),
    )
    first = detector.update(rng.normal(0.2, 0.1, 2))
    last = first
    for _ in range(80):
        last = detector.update(rng.normal(0.2, 0.1, 2))

    assert np.all(last.regime_mean_std < first.regime_mean_std)


def test_stationary_stream_does_not_repeatedly_trigger_downtrend() -> None:
    rng = np.random.default_rng(12)
    names = ("log_return", "order_imbalance", "trade_flow_imbalance")
    detector = DowntrendDetector(
        names,
        bocpd_config=BOCPDConfig(
            hazard_lambda=120,
            max_run_length=160,
            short_run_window=5,
        ),
    )

    triggers = 0
    for _ in range(300):
        signal = detector.update(rng.normal(0.0, 0.25, len(names)))
        triggers += int(signal.triggered)

    assert triggers <= 3

def test_market_feature_builder_uses_available_microstructure_columns() -> None:
    n = 80
    close = np.linspace(100.0, 102.0, n)
    frame = pd.DataFrame(
        {
            "close": close,
            "high": close * 1.001,
            "low": close * 0.999,
            "volume": np.linspace(1000, 1500, n),
            "bid_price": close - 0.01,
            "ask_price": close + 0.01,
            "bid_size": np.linspace(700, 900, n),
            "ask_size": np.linspace(500, 600, n),
            "buy_volume": np.linspace(600, 800, n),
            "sell_volume": np.linspace(400, 500, n),
            "open_interest": np.linspace(10_000, 10_800, n),
            "funding_rate": np.full(n, 0.0001),
        }
    )

    features = MarketFeatureBuilder(volatility_window=16).build(frame)
    expected = {
        "log_return",
        "realized_vol",
        "range_bps",
        "log_volume_change",
        "spread_bps",
        "order_imbalance",
        "trade_flow_imbalance",
        "open_interest_change",
        "funding_rate",
    }
    assert expected.issubset(features.columns)
    assert np.isfinite(features.to_numpy()).all()


def test_relative_bearish_shift_does_not_require_positive_prior_direction() -> None:
    rng = np.random.default_rng(21)
    names = ("log_return", "order_imbalance", "trade_flow_imbalance")
    detector = DowntrendDetector(
        names,
        bocpd_config=BOCPDConfig(
            hazard_lambda=70,
            max_run_length=160,
            short_run_window=5,
        ),
        config=DowntrendConfig(
            short_run_threshold=0.60,
            max_recent_run_length=5,
            min_previous_run_length=12,
            bearish_threshold=0.60,
            min_direction_shift=0.10,
            require_bullish_previous_regime=False,
            min_observations=20,
        ),
    )

    fired = []
    shifts = []
    for i in range(140):
        mean = -0.05 if i < 70 else -2.5
        signal = detector.update(rng.normal(mean, 0.15, len(names)))
        fired.append(signal.triggered)
        shifts.append(signal.direction_shift)

    assert max(shifts[70:85]) > 0.10
    assert any(fired[70:85])


def test_market_feature_builder_rejects_missing_close_values() -> None:
    frame = pd.DataFrame(
        {
            "close": [100.0, np.nan, 101.0],
            "volume": [1.0, 2.0, 3.0],
        }
    )
    with np.testing.assert_raises(ValueError):
        MarketFeatureBuilder().build(frame)
