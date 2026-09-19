from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def signed(value: float, decimals: int = 2) -> str:
    return f"{value:+.{decimals}f}"


def market_section(name: str, root: Path) -> list[str]:
    summary = load_json(root / name / "summary.json")
    benchmark = load_json(root / name / "benchmark_summary.json")
    comparison = load_json(root / name / "comparison.json")
    metadata = load_json(root / name / "market_metadata.json")

    excess = float(comparison["excess_return_pct"])
    verdict = "Outperformed" if excess >= 0 else "Underperformed"

    return [
        f"## {name}",
        "",
        f"**{verdict} Buy & Hold by {signed(excess)} percentage points.**",
        "",
        "| Metric | Strategy | Buy & Hold | Difference |",
        "| --- | ---: | ---: | ---: |",
        (
            f"| Total return | **{signed(float(summary['total_return_pct']))}%** | "
            f"{signed(float(benchmark['total_return_pct']))}% | "
            f"**{signed(excess)} pp** |"
        ),
        (
            f"| Final equity | {float(summary['final_equity']):,.2f} | "
            f"{float(benchmark['final_equity']):,.2f} | "
            f"{signed(float(comparison['final_equity_difference']), 2)} |"
        ),
        (
            f"| Max drawdown | {float(summary['max_drawdown_pct']):.2f}% | "
            f"{float(benchmark['max_drawdown_pct']):.2f}% | "
            f"{signed(float(comparison['drawdown_advantage_pct']))} pp |"
        ),
        (
            f"| Sharpe | {float(summary['sharpe']):.3f} | "
            f"{float(benchmark['sharpe']):.3f} | "
            f"{signed(float(comparison['sharpe_delta']), 3)} |"
        ),
        "",
        "| Strategy diagnostic | Value |",
        "| --- | ---: |",
        f"| Trades | {int(summary['trades'])} |",
        f"| Win rate | {float(summary['win_rate_pct']):.2f}% |",
        f"| Profit factor | {float(summary['profit_factor']):.3f} |",
        f"| Average R | {float(summary['average_r']):.3f} |",
        "",
        f"- Market: {metadata['market']}",
        f"- Interval: {metadata['interval']}",
        (
            f"- Period: {metadata['first_candle_open']} to "
            f"{metadata['last_candle_close']}"
        ),
        f"- Bars: {metadata['bars']}",
        "- Source: Hyperliquid candleSnapshot API",
        "",
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="artifacts")
    parser.add_argument("--output", default="artifacts/release_notes.md")
    args = parser.parse_args()

    root = Path(args.root)
    sha = os.environ.get("GITHUB_SHA", "local")[:7]
    run_number = os.environ.get("GITHUB_RUN_NUMBER", "local")
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")

    lines = [
        f"# Real-market backtest #{run_number} · attempt {run_attempt}",
        "",
        f"- Commit: {sha}",
        "- Markets: HYPE and XAU (xyz:GOLD)",
        "- Strategy: current pyramiding / ATR / trailing-stop implementation",
        "- Benchmark: 1x Buy & Hold with the same fee/slippage assumptions",
        "- No synthetic/regression market data is used in this release.",
        "",
    ]

    lines.extend(market_section("HYPE", root))
    lines.extend(market_section("XAU", root))

    lines.extend(
        [
            "## Files",
            "",
            "Each market includes strategy summary/equity/trades/events, "
            "Buy & Hold summary/equity, comparison JSON, source OHLCV, and metadata.",
        ]
    )

    Path(args.output).write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
