import pandas as pd

from trend_pyramiding.benchmark import compare_to_benchmark, run_buy_and_hold_benchmark
from trend_pyramiding.engine import BacktestConfig


def market_frame(closes: list[float]) -> pd.DataFrame:
    timestamps = pd.date_range("2026-01-01", periods=len(closes), freq="h", tz="UTC")
    opens = [closes[0], *closes[:-1]]
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": [max(o, c) + 0.1 for o, c in zip(opens, closes)],
            "low": [min(o, c) - 0.1 for o, c in zip(opens, closes)],
            "close": closes,
            "volume": 1_000.0,
        }
    )


def test_buy_and_hold_rises_with_market():
    cfg = BacktestConfig(initial_cash=10_000.0, fee_bps=5.0, slippage_bps=3.0)
    result = run_buy_and_hold_benchmark(market_frame([100, 102, 105, 110]), cfg)
    assert result.summary["name"] == "Buy & Hold"
    assert float(result.summary["total_return_pct"]) > 0
    assert float(result.summary["final_equity"]) > cfg.initial_cash


def test_buy_and_hold_flat_market_pays_costs():
    cfg = BacktestConfig(initial_cash=10_000.0, fee_bps=5.0, slippage_bps=3.0)
    result = run_buy_and_hold_benchmark(market_frame([100, 100, 100, 100]), cfg)
    assert float(result.summary["total_return_pct"]) < 0
    assert float(result.summary["fees_paid"]) > 0


def test_comparison_reports_excess_return_and_drawdown_advantage():
    strategy = {
        "total_return_pct": 12.0,
        "max_drawdown_pct": -5.0,
        "sharpe": 1.8,
        "final_equity": 112_000.0,
    }
    benchmark = {
        "total_return_pct": 8.0,
        "max_drawdown_pct": -10.0,
        "sharpe": 1.1,
        "final_equity": 108_000.0,
    }
    comparison = compare_to_benchmark(strategy, benchmark)
    assert comparison["excess_return_pct"] == 4.0
    assert comparison["drawdown_advantage_pct"] == 5.0
    assert comparison["sharpe_delta"] == 0.7
    assert comparison["final_equity_difference"] == 4_000.0
