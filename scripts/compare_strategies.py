"""Reproducible fixed-parameter comparison of classic and confirmed-pyramid policies."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pandas as pd

from trend_pyramiding.cli import _config_from_toml
from trend_pyramiding.engine import _load_frame, run_backtest
from trend_pyramiding.metrics import summarize
from trend_pyramiding.signals import breakout_long_signal


def validated_frame(path: Path) -> pd.DataFrame:
    raw = pd.read_csv(path)
    timestamps = pd.to_datetime(raw["timestamp"], utc=True)
    if timestamps.duplicated().any() or not timestamps.is_monotonic_increasing:
        raise ValueError("source timestamps must be unique and sorted")
    if not timestamps.diff().dropna().dt.total_seconds().eq(3600).all():
        raise ValueError("source must contain continuous hourly bars")
    frame = _load_frame(raw)
    if (
        not len(frame)
        or not np.isfinite(frame[["open", "high", "low", "close", "volume"]]).all().all()
    ):
        raise ValueError("source contains missing/non-finite OHLCV")
    return frame


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--config", default="config/default.toml", type=Path)
    parser.add_argument("--split", default=None, help="UTC start of the later comparison segment")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    frame = validated_frame(args.csv)
    cfg = _config_from_toml(args.config)
    cfg.validate()
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "source_file": args.csv.name,
        "source_sha256": hashlib.sha256(args.csv.read_bytes()).hexdigest(),
        "bars": len(frame),
        "start": frame.timestamp.iloc[0].isoformat(),
        "end_open": frame.timestamp.iloc[-1].isoformat(),
        "parameters": asdict(cfg),
        "split": args.split,
        "policies": ["classic", "confirmed-pyramid"],
        "note": "Retrospective comparisons, not independently untouched out-of-sample results.",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    ranges = {"full": (frame.timestamp.iloc[0], None)}
    if args.split:
        split = pd.Timestamp(args.split)
        if split.tzinfo is None:
            raise ValueError("split must specify a timezone")
        if not frame.timestamp.iloc[0] < split <= frame.timestamp.iloc[-1]:
            raise ValueError("split must be inside the source range")
        ranges.update(early=(frame.timestamp.iloc[0], split), late=(split, None))
    records = []
    signals = breakout_long_signal(frame, cfg.ema_period, cfg.entry_breakout_lookback)
    for segment, (start, end) in ranges.items():
        # Retain earlier candles for indicator warm-up, but disallow earlier entries.
        data = frame[frame.timestamp < end].copy() if end else frame.copy()
        data["comparison_entry"] = signals.iloc[: len(data)] & (data.timestamp >= start)
        for cost_multiplier in (1, 2):
            stressed = replace(
                cfg,
                fee_bps=cfg.fee_bps * cost_multiplier,
                slippage_bps=cfg.slippage_bps * cost_multiplier,
            )
            for policy in manifest["policies"]:
                result = run_backtest(
                    data, stressed, signal_column="comparison_entry", strategy=policy
                )
                equity = result.equity_curve[result.equity_curve.timestamp >= start].copy()
                summary = summarize(equity, result.trades, cfg.initial_cash)
                records.append(
                    {
                        "segment": segment,
                        "cost_multiplier": cost_multiplier,
                        "policy": policy,
                        **summary,
                    }
                )
                destination = out / segment / f"cost-{cost_multiplier}" / policy
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
                equity.to_csv(destination / "equity_curve.csv", index=False)
                result.trades.to_csv(destination / "trades.csv", index=False)
                result.events.to_csv(destination / "events.csv", index=False)
                if segment == "full" and cost_multiplier == 1:
                    series = equity.set_index("timestamp").equity
                    monthly = series.resample("ME").last()
                    previous = monthly.shift(1).fillna(cfg.initial_cash)
                    pd.DataFrame(
                        {"equity": monthly, "return_pct": (monthly / previous - 1) * 100}
                    ).to_csv(destination / "monthly_returns.csv")
    (out / "comparison.json").write_text(json.dumps(records, indent=2) + "\n")
    pd.DataFrame(records).to_csv(out / "comparison.csv", index=False)
    print(json.dumps(records, indent=2))


if __name__ == "__main__":
    main()
