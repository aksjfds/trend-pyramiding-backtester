from __future__ import annotations

import argparse
import json
import tomllib
from dataclasses import fields
from pathlib import Path

from .engine import BacktestConfig, run_backtest
from .validation import locked_parameter_walk_forward


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

    walk_forward = sub.add_parser(
        "walk-forward",
        help="run locked-parameter chronological OOS folds",
    )
    walk_forward.add_argument("--csv", required=True)
    walk_forward.add_argument("--config", default=None)
    walk_forward.add_argument("--signal-column", default=None)
    walk_forward.add_argument("--folds", type=int, default=4)
    walk_forward.add_argument("--warmup-bars", type=int, default=720)
    walk_forward.add_argument("--output-dir", default="artifacts")

    args = parser.parse_args()
    cfg = _config_from_toml(args.config)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    if args.command == "backtest":
        result = run_backtest(args.csv, cfg, signal_column=args.signal_column)
        (out / "summary.json").write_text(
            json.dumps(result.summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        result.equity_curve.to_csv(out / "equity_curve.csv", index=False)
        result.trades.to_csv(out / "trades.csv", index=False)
        result.events.to_csv(out / "events.csv", index=False)
        print(json.dumps(result.summary, indent=2))
        return

    aggregate, folds = locked_parameter_walk_forward(
        args.csv,
        cfg,
        folds=args.folds,
        warmup_bars=args.warmup_bars,
        signal_column=args.signal_column,
    )
    (out / "walk_forward.json").write_text(
        json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    folds.to_csv(out / "walk_forward_folds.csv", index=False)
    print(json.dumps(aggregate, indent=2))


if __name__ == "__main__":
    main()
