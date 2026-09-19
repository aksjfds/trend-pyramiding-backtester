from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/regression_1h.csv")
    args = parser.parse_args()

    n = 900
    t = np.arange(n, dtype=float)
    drift = np.piecewise(
        t,
        [t < 120, (t >= 120) & (t < 340), (t >= 340) & (t < 470),
         (t >= 470) & (t < 760), t >= 760],
        [0.002, 0.200, -0.090, 0.170, -0.060],
    )
    close = 100.0 + np.cumsum(drift + 0.045 * np.sin(t / 7.0) + 0.015 * np.sin(t / 2.5))
    open_ = np.r_[close[0], close[:-1] + 0.03 * np.sin(t[1:] / 3.0)]
    spread = 0.16 + 0.03 * (1.0 + np.sin(t / 5.0))
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = 1_000 + 120 * (1 + np.sin(t / 11.0))

    signal = np.zeros(n, dtype=int)
    signal[125] = 1
    signal[478] = 1

    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "signal_long": signal,
        }
    )
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)


if __name__ == "__main__":
    main()
