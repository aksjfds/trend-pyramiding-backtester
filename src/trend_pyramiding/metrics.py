from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    return float(drawdown.min()) if len(drawdown) else 0.0


def _annualized_sharpe(equity: pd.Series, bars_per_year: float) -> float:
    returns = equity.pct_change().dropna()
    if len(returns) < 2 or float(returns.std(ddof=1)) == 0.0:
        return 0.0
    return float(returns.mean() / returns.std(ddof=1) * math.sqrt(bars_per_year))


def summarize_equity(
    equity_curve: pd.DataFrame,
    initial_cash: float,
) -> dict[str, float]:
    equity = equity_curve["equity"].astype(float)
    final_equity = float(equity.iloc[-1]) if len(equity) else float(initial_cash)
    total_return = final_equity / initial_cash - 1.0

    if len(equity_curve) >= 2:
        ts = pd.to_datetime(equity_curve["timestamp"], utc=True)
        median_seconds = float(ts.diff().dropna().dt.total_seconds().median())
        bars_per_year = 365.25 * 24 * 3600 / median_seconds if median_seconds > 0 else 365.25
    else:
        bars_per_year = 365.25

    summary = {
        "initial_cash": float(initial_cash),
        "final_equity": final_equity,
        "total_return_pct": total_return * 100.0,
        "max_drawdown_pct": _max_drawdown(equity) * 100.0,
        "sharpe": _annualized_sharpe(equity, bars_per_year),
    }
    for key, value in list(summary.items()):
        if not np.isfinite(value):
            summary[key] = 999999.0 if value > 0 else -999999.0
    return summary


def summarize(
    equity_curve: pd.DataFrame,
    trades: pd.DataFrame,
    initial_cash: float,
) -> dict[str, float | int]:
    summary: dict[str, float | int] = summarize_equity(equity_curve, initial_cash)

    if trades.empty:
        win_rate = 0.0
        profit_factor = 0.0
        avg_r = 0.0
    else:
        pnl = trades["net_pnl"].astype(float)
        wins = pnl[pnl > 0]
        losses = pnl[pnl < 0]
        win_rate = float((pnl > 0).mean())
        gross_profit = float(wins.sum())
        gross_loss = abs(float(losses.sum()))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        avg_r = float(trades["r_multiple"].mean())

    summary.update(
        {
            "trades": int(len(trades)),
            "win_rate_pct": win_rate * 100.0,
            "profit_factor": profit_factor,
            "average_r": avg_r,
        }
    )
    for key, value in list(summary.items()):
        if isinstance(value, float) and not np.isfinite(value):
            summary[key] = 999999.0 if value > 0 else -999999.0
    return summary
