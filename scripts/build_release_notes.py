from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def signed(value: float, decimals: int = 2) -> str:
    return f"{value:+.{decimals}f}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts-dir", default="artifacts")
    parser.add_argument("--output", default="artifacts/release_notes.md")
    args = parser.parse_args()

    root = Path(args.artifacts_dir)
    summary = load_json(root / "summary.json")
    benchmark = load_json(root / "benchmark_summary.json")
    comparison = load_json(root / "comparison.json")

    metadata_path = root / "market_metadata.json"
    metadata = load_json(metadata_path) if metadata_path.exists() else None
    market_files = sorted(root.glob("*_1h.csv"))

    sha = os.environ.get("GITHUB_SHA", "local")[:7]
    run_number = os.environ.get("GITHUB_RUN_NUMBER", "local")
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")

    strategy_return = float(summary["total_return_pct"])
    benchmark_return = float(benchmark["total_return_pct"])
    excess = float(comparison["excess_return_pct"])
    dd_advantage = float(comparison["drawdown_advantage_pct"])
    sharpe_delta = float(comparison["sharpe_delta"])
    equity_delta = float(comparison["final_equity_difference"])

    outcome = "Outperformed Buy & Hold" if excess >= 0 else "Underperformed Buy & Hold"

    lines = [
        f"# Backtest #{run_number} · attempt {run_attempt}",
        "",
        f"**{outcome} by {signed(excess)} percentage points.**",
        "",
        "## Performance comparison",
        "",
        "| Metric | Strategy | Buy & Hold | Difference |",
        "| --- | ---: | ---: | ---: |",
        (
            f"| Total return | **{signed(strategy_return)}%** | "
            f"{signed(benchmark_return)}% | **{signed(excess)} pp** |"
        ),
        (
            f"| Final equity | {float(summary['final_equity']):,.2f} | "
            f"{float(benchmark['final_equity']):,.2f} | {signed(equity_delta, 2)} |"
        ),
        (
            f"| Max drawdown | {float(summary['max_drawdown_pct']):.2f}% | "
            f"{float(benchmark['max_drawdown_pct']):.2f}% | "
            f"{signed(dd_advantage)} pp advantage |"
        ),
        (
            f"| Sharpe | {float(summary['sharpe']):.3f} | "
            f"{float(benchmark['sharpe']):.3f} | {signed(sharpe_delta, 3)} |"
        ),
        "",
        "## Strategy diagnostics",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Trades | {int(summary['trades'])} |",
        f"| Win rate | {float(summary['win_rate_pct']):.2f}% |",
        f"| Profit factor | {float(summary['profit_factor']):.3f} |",
        f"| Average R | {float(summary['average_r']):.3f} |",
        "",
        "## Backtest context",
        "",
        f"- Commit: {sha}",
    ]

    if metadata:
        lines.extend(
            [
                f"- Market: {metadata['market']}",
                f"- Interval: {metadata['interval']}",
                (
                    f"- Period: {metadata['first_candle_open']} to "
                    f"{metadata['last_candle_close']}"
                ),
                f"- Bars: {metadata['bars']}",
                f"- Market data source: {metadata.get('source', 'unknown')}",
            ]
        )
    else:
        lines.append("- Dataset: deterministic regression fixture")

    lines.extend(
        [
            "- Strategy signal: EMA20 + close breakout above prior 20-bar high",
            (
                "- Benchmark: 1x Buy & Hold — deploy all initial cash at the first "
                "bar open and exit at the final bar close."
            ),
            (
                "- Benchmark uses the same configured trading fee and slippage "
                "assumptions as the strategy."
            ),
            "",
            "## Files",
            "",
            "- summary.json — strategy metrics",
            "- benchmark_summary.json — Buy & Hold metrics",
            "- comparison.json — strategy-minus-benchmark comparison",
            "- equity_curve.csv — strategy equity curve",
            "- benchmark_equity_curve.csv — Buy & Hold equity curve",
            "- trades.csv — strategy trades",
            "- events.csv — strategy execution events",
        ]
    )

    if metadata:
        lines.append("- market_metadata.json — market-data provenance")
    for market_file in market_files:
        lines.append(f"- {market_file.name} — source OHLCV used by this run")

    Path(args.output).write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
