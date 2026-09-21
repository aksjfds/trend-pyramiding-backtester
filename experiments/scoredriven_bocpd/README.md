# Score-Driven BOCPD for Market Microstructure

A standalone Python experiment for detecting an online market regime change and
then deciding whether the new regime is directionally bearish.

The project combines:

1. Bayesian Online Change-Point Detection (BOCPD) run-length inference.
2. Autoregressive dependence inside each candidate regime.
3. Generalized Autoregressive Score (GAS) updates for time-varying location,
   scale, and AR coefficients.
4. A multivariate market-microstructure feature vector.
5. A separate post-change direction score so a volatility shock is not
   automatically treated as a downtrend.

## Important implementation note

The 2025 Tsaknaki-Lillo-Mazzarisi autoregressive/score-driven BOCPD work is
formulated for a univariate series. This project implements the same design
principles but extends the observation model to a **diagonal multivariate
Student-t likelihood**. Every run-length hypothesis owns its own GAS/AR state.

That choice is intentional:

- Student-t scores are robust to isolated fat-tail prints.
- A diagonal likelihood avoids unstable online inversion of a dense
  microstructure covariance matrix.
- Temporal dependence is still modeled independently for every feature.
- The BOCPD posterior fuses all feature likelihood contributions jointly.

This is an engineering multivariate extension, not a line-for-line reproduction
of the paper.

## Model

For feature vector x_t and regime state R, each feature uses

~~~text
loc_t = mu_R + phi_t * (x_{t-1} - mu_R)
x_t ~ StudentT(nu, loc_t, sigma_t)
phi_t = phi_max * tanh(eta_t)
~~~

with score-driven recursions

~~~text
mu_{t+1}        = mu_t + a_mu * scaled_score_location
log_sigma_{t+1} = log_sigma_t + a_sigma * score_log_scale
eta_{t+1}       = beta * eta_t + a_phi * score_phi
~~~

BOCPD maintains a posterior over all candidate run lengths. A constant hazard
sets the prior expected regime duration. The implementation uses a saturating
run-length cap so old-regime posterior probability is not silently discarded.

## Market inputs

MarketFeatureBuilder requires close and uses any optional columns that are
present:

- high, low
- volume
- bid_price, ask_price
- bid_size, ask_size
- buy_volume, sell_volume
- open_interest
- funding_rate

It can generate:

- log return
- realized volatility
- intrabar range in bps
- log-volume change
- spread in bps
- top-of-book imbalance
- trade-flow imbalance
- open-interest change
- funding rate

EWMStandardizer is causal: a new observation is standardized using only state
accumulated before that observation, then the state is updated.

## Downtrend signal

The statistical jobs are separated:

- BOCPD: "did the generating regime change?"
- Direction layer: "does the new regime point down?"

By default, direction uses return, order-book imbalance, trade-flow imbalance,
open-interest change and funding. Volatility and spread can contribute to
change detection without being assigned a bearish sign.

A signal is emitted only after the minimum warm-up and when:

~~~text
change evidence >= configured threshold
AND
bearish score >= configured threshold
~~~

Thresholds are calibration parameters, not universal constants.

## Install

From this directory:

~~~bash
python -m pip install -e ".[test]"
~~~

## Demo

~~~bash
python examples/demo.py
~~~

The demo creates a synthetic bullish regime followed by a strong bearish
microstructure regime and prints detected bearish change points.

## Minimal use

~~~python
import pandas as pd
from scoredriven_bocpd import (
    BOCPDConfig,
    DowntrendDetector,
    MarketFeatureBuilder,
)

bars = pd.read_csv("market.csv")
features = MarketFeatureBuilder().build(bars)

detector = DowntrendDetector(
    features.columns,
    bocpd_config=BOCPDConfig(hazard_lambda=120),
)

for timestamp, row in features.iterrows():
    signal = detector.update(row.to_dict())
    if signal.triggered:
        print(
            timestamp,
            signal.bocpd.changepoint_probability,
            signal.bocpd.short_run_probability,
            signal.bearish_score,
        )
~~~

## Output

BOCPDUpdate exposes:

- changepoint_probability
- short_run_probability
- map_run_length
- full run-length posterior
- MAP regime location
- MAP regime scale
- MAP AR coefficient

short_run_probability is useful when posterior mass spreads across run lengths
0..k instead of concentrating exactly at r=0.

## Calibration

For real data, calibrate at least:

- hazard_lambda
- Student-t degrees of freedom
- GAS step sizes
- run-length cap
- directional feature weights
- change threshold
- bearish threshold

Calibration should be done on historical periods disjoint from final
evaluation data.

## References

- Adams, R. P. and MacKay, D. J. C. (2007), Bayesian Online Changepoint
  Detection: https://arxiv.org/abs/0710.3742
- Creal, D., Koopman, S. J., Lucas, A. (2013), Generalized Autoregressive Score
  Models with Applications, Journal of Applied Econometrics,
  DOI 10.1002/jae.1279
- Tsaknaki, I.-Y., Lillo, F., Mazzarisi, P. (2025), Bayesian autoregressive
  online change-point detection with time-varying parameters,
  DOI 10.1016/j.cnsns.2024.108500
- Tsaknaki, I.-Y., Lillo, F., Mazzarisi, P. (2025), Online learning of order
  flow and market impact with Bayesian change-point detection methods,
  Quantitative Finance, DOI 10.1080/14697688.2024.2337300

This package is a research detector, not a complete trading strategy.
