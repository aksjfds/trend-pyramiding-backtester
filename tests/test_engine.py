import subprocess
import sys
from pathlib import Path

import pandas as pd

from trend_pyramiding.engine import BacktestConfig, _initial_stop, run_backtest
from trend_pyramiding.indicators import completed_timeframe_trend_filter
from trend_pyramiding.validation import locked_parameter_walk_forward

ROOT = Path(__file__).resolve().parents[1]


def fixture_frame(tmp_path: Path) -> pd.DataFrame:
    path = tmp_path / "fixture.csv"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/generate_fixture.py"), "--output", str(path)],
        check=True,
    )
    return pd.read_csv(path)


def test_signal_fills_on_next_bar_open(tmp_path):
    frame = fixture_frame(tmp_path)
    signal_idx = int(frame.index[frame["signal_long"] == 1][0])
    cfg = BacktestConfig(require_add_breakout=False)
    result = run_backtest(frame, cfg, signal_column="signal_long")
    first_entry = result.events[result.events["event"] == "entry"].iloc[0]
    expected_time = pd.to_datetime(frame.loc[signal_idx + 1, "timestamp"], utc=True)
    assert pd.to_datetime(first_entry["timestamp"], utc=True) == expected_time


def test_adds_only_to_winners_and_risk_is_capped(tmp_path):
    frame = fixture_frame(tmp_path)
    cfg = BacktestConfig(require_add_breakout=False)
    result = run_backtest(frame, cfg, signal_column="signal_long")
    assert len(result.trades) >= 2
    adds = result.events[result.events["event"] == "add"]
    assert not adds.empty
    for _, group in result.events.groupby("trade_id"):
        entry = group[group["event"] == "entry"].iloc[0]
        for _, add in group[group["event"] == "add"].iterrows():
            assert add["price"] > entry["price"]
            assert add["open_risk_to_stop"] <= add["risk_budget"] * 1.000001


def test_stop_never_moves_down(tmp_path):
    frame = fixture_frame(tmp_path)
    result = run_backtest(
        frame,
        BacktestConfig(require_add_breakout=False),
        signal_column="signal_long",
    )
    for _, group in result.events.groupby("trade_id"):
        stops = group[group["event"].isin(["entry", "add", "stop_update"])]["stop"].tolist()
        assert stops == sorted(stops)


def test_v11_defaults_are_confirmation_first():
    cfg = BacktestConfig()
    assert cfg.require_add_breakout is True
    assert cfg.add_breakout_lookback == 20
    assert cfg.break_even_r == 1.5
    assert cfg.trail_activation_r == 2.0
    assert cfg.trail_atr_mult == 3.0


def test_initial_stop_uses_structure_and_rejects_excessive_distance():
    row = pd.Series({"atr": 2.0, "structure_low": 94.0, "close": 100.0})
    cfg = BacktestConfig(max_initial_stop_atr=4.0, structure_buffer_atr=0.10)
    assert _initial_stop(row, cfg) == 93.8

    too_wide = pd.Series({"atr": 2.0, "structure_low": 90.0, "close": 100.0})
    assert _initial_stop(too_wide, cfg) is None


def test_completed_daily_filter_uses_only_completed_days():
    n = 24 * 8
    close = pd.Series(100.0 + 0.1 * pd.RangeIndex(n), dtype=float)
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC"),
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": 1_000.0,
        }
    )
    trend = completed_timeframe_trend_filter(
        frame,
        timeframe="1D",
        ema_period=3,
        slope_lookback=1,
    )
    assert not trend.iloc[:95].any()
    assert bool(trend.iloc[95])


def test_locked_parameter_walk_forward_returns_chronological_folds(tmp_path):
    frame = fixture_frame(tmp_path)
    aggregate, folds = locked_parameter_walk_forward(
        frame,
        BacktestConfig(require_add_breakout=False),
        folds=3,
        warmup_bars=120,
        signal_column="signal_long",
    )
    assert aggregate["folds"] == 3
    assert len(folds) == 3
    starts = pd.to_datetime(folds["test_start"], utc=True)
    assert starts.is_monotonic_increasing
