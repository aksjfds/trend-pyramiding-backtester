from __future__ import annotations

import numpy as np
import pandas as pd

from scoredriven_bocpd import (
    BOCPDConfig,
    DowntrendDetector,
    MarketFeatureBuilder,
)


def synthetic_market(seed: int = 7, n: int = 240) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    switch = n // 2

    returns = np.r_[
        rng.normal(0.0008, 0.0020, switch),
        rng.normal(-0.0030, 0.0030, n - switch),
    ]
    close = 100.0 * np.exp(np.cumsum(returns))
    high = close * (1.0 + rng.uniform(0.0002, 0.0020, n))
    low = close * (1.0 - rng.uniform(0.0002, 0.0020, n))

    imbalance = np.r_[
        rng.normal(0.25, 0.10, switch),
        rng.normal(-0.55, 0.12, n - switch),
    ]
    flow = np.r_[
        rng.normal(0.20, 0.12, switch),
        rng.normal(-0.60, 0.15, n - switch),
    ]

    total_book = rng.uniform(800, 1400, n)
    bid_size = total_book * (1.0 + imbalance) / 2.0
    ask_size = total_book - bid_size

    traded = rng.uniform(400, 900, n)
    buy_volume = traded * (1.0 + flow) / 2.0
    sell_volume = traded - buy_volume

    mid = close
    half_spread = mid * 0.00008
    return pd.DataFrame(
        {
            "close": close,
            "high": high,
            "low": low,
            "volume": traded,
            "bid_price": mid - half_spread,
            "ask_price": mid + half_spread,
            "bid_size": bid_size,
            "ask_size": ask_size,
            "buy_volume": buy_volume,
            "sell_volume": sell_volume,
        }
    )


def main() -> None:
    raw = synthetic_market()
    features = MarketFeatureBuilder(volatility_window=24).build(raw)

    detector = DowntrendDetector(
        features.columns,
        bocpd_config=BOCPDConfig(
            hazard_lambda=100,
            max_run_length=192,
            short_run_window=5,
        ),
    )

    rows = []
    for index, row in features.iterrows():
        signal = detector.update(row.to_dict())
        rows.append(
            {
                "index": index,
                "cp_probability": signal.bocpd.changepoint_probability,
                "short_run_probability": signal.bocpd.short_run_probability,
                "map_run_length": signal.bocpd.map_run_length,
                "bearish_score": signal.bearish_score,
                "triggered": signal.triggered,
            }
        )

    output = pd.DataFrame(rows)
    print(output.loc[output["triggered"]].head(20).to_string(index=False))


if __name__ == "__main__":
    main()
