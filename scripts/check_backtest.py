from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", required=True)
    parser.add_argument("--baseline", required=True)
    args = parser.parse_args()

    summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))

    for key in ["final_equity", "total_return_pct", "max_drawdown_pct", "trades"]:
        if key not in summary:
            raise SystemExit(f"missing summary field: {key}")
        value = summary[key]
        if isinstance(value, float) and not math.isfinite(value):
            raise SystemExit(f"non-finite summary field: {key}={value}")

    if summary["trades"] < baseline["min_trades"]:
        raise SystemExit(f"trade count regressed: {summary['trades']} < {baseline['min_trades']}")
    if summary["total_return_pct"] < baseline["min_total_return_pct"]:
        raise SystemExit(
            f"return regressed: {summary['total_return_pct']:.4f}% < "
            f"{baseline['min_total_return_pct']:.4f}%"
        )
    if summary["max_drawdown_pct"] < -abs(baseline["max_drawdown_pct_abs"]):
        raise SystemExit(
            f"drawdown regressed: {summary['max_drawdown_pct']:.4f}% < "
            f"-{baseline['max_drawdown_pct_abs']:.4f}%"
        )

    print("backtest regression checks passed")


if __name__ == "__main__":
    main()
