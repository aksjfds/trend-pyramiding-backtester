from dataclasses import replace

import pandas as pd
import pytest

from trend_pyramiding.engine import BacktestConfig, Position, _size_tranche, run_backtest
from trend_pyramiding.signals import strong_close_signal


def market_frame():
    # Deterministic trend/pullback cycles, independent of the research market.
    close = [100 + i * 0.3 + (i % 25) * 0.1 for i in range(240)]
    opens = [close[0], *close[:-1]]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-01-01", periods=240, freq="h", tz="UTC"),
            "open": opens,
            "high": [max(o, c) + 0.4 for o, c in zip(opens, close)],
            "low": [min(o, c) - 0.7 for o, c in zip(opens, close)],
            "close": close,
            "volume": 1000,
        }
    )


def test_close_confirmation_boundary_and_causality():
    frame = pd.DataFrame({"high": [110, 110, 110], "low": [90, 90, 90], "close": [99, 100, 110]})
    assert strong_close_signal(frame).tolist() == [False, True, True]
    frame.loc[2, "high"] = 10000
    assert strong_close_signal(frame).iloc[:2].tolist() == [False, True]


def test_bounded_recycling_preserves_first_tranche_and_caps_reuse():
    cfg = replace(BacktestConfig(), fee_bps=0)
    position = Position(
        1, pd.Timestamp("2025-01-01", tz="UTC"), 80, 80, 1, 90, 70, 10, 1000, 100000, tranches=2
    )
    args = dict(
        fill_price=100,
        stop=90,
        cash=100000,
        position=None,
        tranche_index=0,
        cfg=cfg,
        trade_equity=100000,
        risk_budget=1000,
    )
    assert _size_tranche(**args, recycle_risk=False) == 30
    assert _size_tranche(**args, recycle_risk=True) == 30
    args.update(position=position, tranche_index=2)
    assert _size_tranche(**args, recycle_risk=False) == 20
    # Released risk can raise the third tranche to the largest original allowance (300).
    assert _size_tranche(**args, recycle_risk=True) == 30
    args.update(cash=50)
    assert _size_tranche(**args, recycle_risk=True) == 0.5


def test_candidate_waits_for_protection_and_never_exceeds_total_risk():
    result = run_backtest(market_frame(), strategy="confirmed-pyramid")
    assert not result.events[result.events.event == "add"].empty
    for _, trade in result.events.groupby("trade_id"):
        previous_average = None
        for row in trade.itertuples():
            if row.event == "add":
                assert row.stop >= previous_average
                assert row.open_risk_to_stop <= row.risk_budget + 1e-8
            previous_average = row.avg_entry
        stops = trade[trade.event.isin(["entry", "add", "stop_update"])].stop.tolist()
        assert stops == sorted(stops)


def test_candidate_fills_confirmed_signals_on_next_open_and_is_prefix_stable():
    frame = market_frame()
    frame["external_signal"] = False
    frame.loc[30, "external_signal"] = True
    result = run_backtest(frame, signal_column="external_signal", strategy="confirmed-pyramid")
    entry = result.events[result.events.event == "entry"].iloc[0]
    assert entry.timestamp == frame.timestamp.iloc[31]
    prefix = run_backtest(
        frame.iloc[:100], signal_column="external_signal", strategy="confirmed-pyramid"
    )
    # End-of-data liquidation only affects the last bar, which is deliberately excluded.
    pd.testing.assert_frame_equal(prefix.equity_curve.iloc[:-1], result.equity_curve.iloc[:99])


def test_policy_is_explicit_and_classic_remains_default():
    frame = market_frame()
    assert run_backtest(frame).summary == run_backtest(frame, strategy="classic").summary
    with pytest.raises(ValueError, match="strategy"):
        run_backtest(frame, strategy="HYPE-special")


def test_cli_records_selected_policy_and_same_numeric_parameters(tmp_path):
    import json
    import subprocess
    import sys

    path = tmp_path / "market.csv"
    market_frame().to_csv(path, index=False)
    output = tmp_path / "output"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "trend_pyramiding.cli",
            "backtest",
            "--csv",
            str(path),
            "--strategy",
            "confirmed-pyramid",
            "--output-dir",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    manifest = json.loads((output / "strategy_manifest.json").read_text())
    assert manifest["strategy"] == "confirmed-pyramid"
    assert manifest["config"]["risk_per_trade"] == 0.01
    assert manifest["config"]["risk_weights"] == [0.3, 0.3, 0.2, 0.2]
    summary = json.loads((output / "summary.json").read_text())
    assert summary["total_return_pct"] == pytest.approx(
        run_backtest(path, strategy="confirmed-pyramid").summary["total_return_pct"]
    )
