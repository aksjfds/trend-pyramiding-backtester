import subprocess
import sys
from pathlib import Path

import pandas as pd

from trend_pyramiding.engine import BacktestConfig, run_backtest

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
    result = run_backtest(frame, BacktestConfig(), signal_column="signal_long")
    first_entry = result.events[result.events["event"] == "entry"].iloc[0]
    expected_time = pd.to_datetime(frame.loc[signal_idx + 1, "timestamp"], utc=True)
    assert pd.to_datetime(first_entry["timestamp"], utc=True) == expected_time


def test_adds_only_to_winners_and_risk_is_capped(tmp_path):
    frame = fixture_frame(tmp_path)
    cfg = BacktestConfig()
    result = run_backtest(frame, cfg, signal_column="signal_long")
    assert len(result.trades) >= 2
    adds = result.events[result.events["event"] == "add"]
    assert not adds.empty
    for trade_id, group in result.events.groupby("trade_id"):
        entry = group[group["event"] == "entry"].iloc[0]
        for _, add in group[group["event"] == "add"].iterrows():
            assert add["price"] > entry["price"]
            assert add["open_risk_to_stop"] <= add["risk_budget"] * 1.000001


def test_stop_never_moves_down(tmp_path):
    frame = fixture_frame(tmp_path)
    result = run_backtest(frame, BacktestConfig(), signal_column="signal_long")
    for _, group in result.events.groupby("trade_id"):
        stops = group[group["event"].isin(["entry", "add", "stop_update"])]["stop"].tolist()
        assert stops == sorted(stops)
