from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .engine import BacktestConfig, _load_frame, run_backtest


def locked_parameter_walk_forward(
    data: pd.DataFrame | str | Path,
    cfg: BacktestConfig,
    *,
    folds: int = 4,
    warmup_bars: int = 720,
    signal_column: str | None = None,
) -> tuple[dict[str, float | int], pd.DataFrame]:
    """Chronological rolling OOS evaluation with the parameter set locked.

    No optimization occurs inside the folds. Each test fold receives only past
    bars as indicator warm-up, entries are disabled before the fold boundary,
    and each fold is evaluated independently.
    """
    if folds < 2:
        raise ValueError("folds must be >= 2")
    if warmup_bars < 1:
        raise ValueError("warmup_bars must be >= 1")

    frame = _load_frame(data)
    if len(frame) <= warmup_bars + folds:
        raise ValueError("not enough bars for requested walk-forward")

    remaining = len(frame) - warmup_bars
    fold_size = remaining // folds
    if fold_size < 1:
        raise ValueError("walk-forward fold size is empty")

    rows: list[dict[str, float | int | str]] = []
    for fold in range(folds):
        test_start_idx = warmup_bars + fold * fold_size
        test_end_idx = len(frame) if fold == folds - 1 else test_start_idx + fold_size

        test_start = frame.iloc[test_start_idx]["timestamp"]
        test_end = (
            frame.iloc[test_end_idx]["timestamp"] if test_end_idx < len(frame) else None
        )
        warm_start_idx = max(0, test_start_idx - warmup_bars)
        fold_frame = frame.iloc[warm_start_idx:test_end_idx].copy()

        result = run_backtest(
            fold_frame,
            cfg,
            signal_column=signal_column,
            entry_start=test_start,
            entry_end=test_end,
        )
        rows.append(
            {
                "fold": fold + 1,
                "test_start": str(test_start),
                "test_end": (
                    str(test_end)
                    if test_end is not None
                    else str(frame.iloc[-1]["timestamp"])
                ),
                **result.summary,
            }
        )

    folds_df = pd.DataFrame(rows)
    returns = folds_df["total_return_pct"].astype(float)
    drawdowns = folds_df["max_drawdown_pct"].astype(float)
    profit_factors = folds_df["profit_factor"].astype(float)
    sharpes = folds_df["sharpe"].astype(float)

    aggregate: dict[str, float | int] = {
        "folds": int(len(folds_df)),
        "positive_folds": int((returns > 0).sum()),
        "positive_fold_ratio": float((returns > 0).mean()),
        "median_return_pct": float(returns.median()),
        "mean_return_pct": float(returns.mean()),
        "worst_fold_return_pct": float(returns.min()),
        "worst_fold_drawdown_pct": float(drawdowns.min()),
        "median_profit_factor": float(profit_factors.median()),
        "median_sharpe": float(sharpes.median()),
        "total_trades": int(folds_df["trades"].astype(int).sum()),
    }
    for key, value in list(aggregate.items()):
        if isinstance(value, float) and not np.isfinite(value):
            aggregate[key] = 999999.0 if value > 0 else -999999.0

    return aggregate, folds_df
