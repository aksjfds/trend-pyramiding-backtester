from __future__ import annotations

import argparse
import json
import tomllib
from dataclasses import fields
from pathlib import Path

from .engine import BacktestConfig, run_backtest


def _config_from_toml(path: str | Path | None) -> BacktestConfig:
    if path is None:
        return BacktestConfig()
    with open(path, "rb") as handle:
        raw = tomllib.load(handle)
    allowed = {item.name for item in fields(BacktestConfig)}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"unknown config keys: {', '.join(unknown)}")
    for key in ("risk_weights", "allocation_weights"):
        if key in raw:
            raw[key] = tuple(float(x) for x in raw[key])
    return BacktestConfig(**raw)


def main() -> None:
    parser = argparse.ArgumentParser(prog="pyramid-backtest")
    sub = parser.add_subparsers(dest="command", required=True)

    backtest = sub.add_parser("backtest", help="run a deterministic OHLCV backtest")
    backtest.add_argument("--csv", required=True)
    backtest.add_argument("--config", default=None)
    backtest.add_argument("--signal-column", default=None)
    backtest.add_argument("--output-dir", default="artifacts")

    args = parser.parse_args()
    if args.command == "backtest":
        cfg = _config_from_toml(args.config)
        result = run_backtest(args.csv, cfg, signal_column=args.signal_column)
        out = Path(args.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "summary.json").write_text(
            json.dumps(result.summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        result.equity_curve.to_csv(out / "equity_curve.csv", index=False)
        result.trades.to_csv(out / "trades.csv", index=False)
        result.events.to_csv(out / "events.csv", index=False)
        print(json.dumps(result.summary, indent=2))


if __name__ == "__main__":
    main()
