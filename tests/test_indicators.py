import pandas as pd

from trend_pyramiding.indicators import atr, prior_rolling_high


def test_indicators_do_not_use_future_rows():
    frame = pd.DataFrame(
        {
            "high": [11, 12, 13, 14, 15, 16],
            "low": [9, 10, 11, 12, 13, 14],
            "close": [10, 11, 12, 13, 14, 15],
        }
    )
    base_atr = atr(frame, 3)
    base_high = prior_rolling_high(frame["high"], 3)

    changed = frame.copy()
    changed.loc[5, ["high", "low", "close"]] = [1000, 1, 900]
    changed_atr = atr(changed, 3)
    changed_high = prior_rolling_high(changed["high"], 3)

    pd.testing.assert_series_equal(base_atr.iloc[:5], changed_atr.iloc[:5])
    pd.testing.assert_series_equal(base_high.iloc[:5], changed_high.iloc[:5])
