from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .engine import BacktestConfig, _load_frame
from .metrics import summarize_equity


@dataclass
class BenchmarkResult:
    summary: dict[str, float | str]
    equity_curve: pd.DataFrame


def run_buy_and_hold_benchmark(
    data: pd.DataFrame | str | Path,
    cfg: BacktestConfig,
) -> BenchmarkResult:
    """Run a 1x buy-and-hold benchmark with the same fee/slippage assumptions."""
    frame = _load_frame(data)
    if frame.empty:
        raise ValueError("benchmark requires at least one bar")

    fee_rate = cfg.fee_bps / 10_000.0
    entry_raw = float(frame.iloc[0]["open"])
    entry_price = entry_raw * (1.0 + cfg.slippage_bps / 10_000.0)

    qty = cfg.initial_cash / (entry_price * (1.0 + fee_rate))
    entry_notional = qty * entry_price
    entry_fee = entry_notional * fee_rate
    cash = cfg.initial_cash - entry_notional - entry_fee

    rows: list[dict[str, float | pd.Timestamp]] = []
    for row in frame.itertuples(index=False):
        rows.append(
            {
                "timestamp": row.timestamp,
                "equity": cash + qty * float(row.close),
            }
        )

    last = frame.iloc[-1]
    exit_raw = float(last["close"])
    exit_price = exit_raw * (1.0 - cfg.slippage_bps / 10_000.0)
    exit_notional = qty * exit_price
    exit_fee = exit_notional * fee_rate
    final_equity = cash + exit_notional - exit_fee
    rows[-1]["equity"] = final_equity

    equity_curve = pd.DataFrame(rows)
    summary = summarize_equity(equity_curve, cfg.initial_cash)
    summary.update(
        {
            "name": "Buy & Hold",
            "entry_price": entry_price,
            "exit_price": exit_price,
            "fees_paid": entry_fee + exit_fee,
        }
    )
    return BenchmarkResult(summary=summary, equity_curve=equity_curve)


def compare_to_benchmark(
    strategy_summary: dict[str, float | int],
    benchmark_summary: dict[str, float | str],
) -> dict[str, float]:
    strategy_return = float(strategy_summary["total_return_pct"])
    benchmark_return = float(benchmark_summary["total_return_pct"])
    strategy_dd = abs(float(strategy_summary["max_drawdown_pct"]))
    benchmark_dd = abs(float(benchmark_summary["max_drawdown_pct"]))
    strategy_sharpe = float(strategy_summary["sharpe"])
    benchmark_sharpe = float(benchmark_summary["sharpe"])

    return {
        "excess_return_pct": strategy_return - benchmark_return,
        "drawdown_advantage_pct": benchmark_dd - strategy_dd,
        "sharpe_delta": strategy_sharpe - benchmark_sharpe,
        "final_equity_difference": (
            float(strategy_summary["final_equity"])
            - float(benchmark_summary["final_equity"])
        ),
    }
