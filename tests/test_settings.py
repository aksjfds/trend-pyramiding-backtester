from dataclasses import replace

import pytest
from test_live import MARKET, Exchange, approve_first_candidate, request_candidate_scan

from trend_pyramiding.engine import BacktestConfig
from trend_pyramiding.live import (
    LiveConfig,
    StateStore,
    SwapRunner,
    closed_candles,
    config_fingerprint,
)
from trend_pyramiding.settings import load_settings, save_settings


def test_settings_survive_restart_and_profiles_are_separate(tmp_path):
    config, strategy = LiveConfig(), BacktestConfig()
    store = StateStore(tmp_path / "okx-live.json")
    values = {
        "live": {"capital_fraction": 0.6, "leverage": 5, "bar": "4H"},
        "strategy": {"atr_stop_mult": 3, "risk_per_trade": 0.02},
    }
    save_settings(config, strategy, store, values)
    cfg, strat = load_settings(config, strategy, StateStore(store.path))
    assert (cfg.capital_fraction, cfg.leverage, cfg.bar) == (0.6, 5, "4H")
    assert strat.atr_stop_mult == 3 and strat.risk_per_trade == 0.02
    assert load_settings(config, strategy, StateStore(tmp_path / "okx-demo.json")) == (
        config,
        strategy,
    )


@pytest.mark.parametrize(
    "values",
    [
        {"live": {"capital_fraction": 1.01}},
        {"live": {"capital_fraction": 0}},
        {"live": {"capital_fraction": float("nan")}},
        {"live": {"capital_fraction": True}},
        {"live": {"leverage": 0}},
        {"live": {"leverage": 1.5}},
        {"live": {"leverage": 126}},
        {"live": {"bar": "not-a-bar"}},
        {"live": {"base_url": "https://example.com"}},
        {"strategy": {"risk_per_trade": 0.11}},
        {"strategy": {"atr_period": 101}},
        {"strategy": {"atr_stop_mult": 0}},
        {"strategy": {"risk_weights": [0.8, 0.8]}},
        {"strategy": {"require_add_breakout": "false"}},
        {"strategy": {"allocation_weights": [float("inf")]}},
    ],
)
def test_invalid_settings_rejected_without_replacing_saved_file(tmp_path, values):
    store = StateStore(tmp_path / "okx-live.json")
    valid = {"live": {"capital_fraction": 0.5}, "strategy": {}}
    save_settings(LiveConfig(), BacktestConfig(), store, valid)
    path = store.path.with_suffix(".settings.json")
    before = path.read_bytes()
    with pytest.raises((ValueError, TypeError)):
        save_settings(LiveConfig(), BacktestConfig(), store, {"live": {}, "strategy": {}, **values})
    assert path.read_bytes() == before


@pytest.mark.parametrize("busy", ["position", "pending", "halt"])
def test_cannot_save_over_open_position_pending_order_or_halt(tmp_path, busy):
    store = StateStore(tmp_path / "okx-live.json")
    store.save(
        {
            "version": 1,
            "pending": {"id": "a"} if busy == "pending" else None,
            "markets": {MARKET: {"position": {"qty": "1"} if busy == "position" else None}},
        }
    )
    if busy == "halt":
        store.halt(RuntimeError("inspect"))
    with pytest.raises(ValueError, match="不能修改"):
        save_settings(
            LiveConfig(), BacktestConfig(), store, {"live": {"leverage": 5}, "strategy": {}}
        )
    assert not store.path.with_suffix(".settings.json").exists()


def test_saved_parameters_migrate_flat_state_and_actual_leverage(tmp_path):
    store = StateStore(tmp_path / "okx-live.json")
    config, strategy, exchange = LiveConfig(instruments=(MARKET,)), BacktestConfig(), Exchange()
    bot = SwapRunner(exchange, config, strategy, store)
    bot.initialize()
    save_settings(
        config,
        strategy,
        store,
        {"live": {"capital_fraction": 0.5, "leverage": 5}, "strategy": {"atr_stop_mult": 3}},
    )
    cfg, strat = load_settings(config, strategy, store)
    restarted = SwapRunner(exchange, cfg, strat, store)
    restarted.initialize()
    assert restarted.state["capital_ceiling"] == 5000
    assert exchange.leverage == 5
    assert restarted.state["fingerprint"] == config_fingerprint(cfg, strat, "test-account")
    restarted.step()
    assert not exchange.orders
    request_candidate_scan(restarted)
    restarted.step()
    approve_first_candidate(restarted)
    restarted.step()
    assert len(exchange.orders) == 1
    restarted.step()
    assert len(exchange.orders) == 1


def test_legacy_flat_state_can_migrate_with_matching_saved_origin(tmp_path):
    store = StateStore(tmp_path / "okx-live.json")
    config, strategy, exchange = LiveConfig(instruments=(MARKET,)), BacktestConfig(), Exchange()
    bot = SwapRunner(exchange, config, strategy, store)
    bot.initialize()
    state = store.load()
    state.pop("configuration")
    store.save(state)
    save_settings(config, strategy, store, {"live": {"capital_fraction": 0.1}, "strategy": {}})
    cfg, strat = load_settings(config, strategy, store)
    bot = SwapRunner(exchange, cfg, strat, store)
    bot.initialize()
    assert bot.state["capital_ceiling"] == 1000


def test_settings_cannot_migrate_different_account(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "okx-live.json")
    config, strategy, exchange = LiveConfig(instruments=(MARKET,)), BacktestConfig(), Exchange()
    bot = SwapRunner(exchange, config, strategy, store)
    bot.initialize()
    save_settings(config, strategy, store, {"live": {"leverage": 5}, "strategy": {}})
    cfg, strat = load_settings(config, strategy, store)
    original = exchange.get

    def get(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path.endswith("account/config"):
            result[0]["uid"] = "other-account"
        return result

    monkeypatch.setattr(exchange, "get", get)
    before = len(exchange.posts)
    with pytest.raises(ValueError, match="account/config"):
        SwapRunner(exchange, cfg, strat, store).initialize()
    assert len(exchange.posts) == before


@pytest.mark.parametrize("bar,seconds", [("15m", 900), ("4H", 14400), ("1Dutc", 86400)])
def test_selected_candle_interval_used_for_fetch_and_spacing(bar, seconds):
    exchange = Exchange()
    original = exchange.get
    seen = []

    def get(path, params=None, **kwargs):
        seen.append(params["bar"])
        rows = original(path, params, **kwargs)
        for row in rows:
            row[0] = str(1767225600000 + (int(row[0]) - 1767225600000) * seconds // 3600)
        return rows

    exchange.get = get
    frame = closed_candles(exchange, MARKET, BacktestConfig(), bar)
    assert seen == [bar]
    assert frame.timestamp.diff().dropna().dt.total_seconds().eq(seconds).all()


def test_daily_exit_does_not_reuse_a_signal_from_before_exit(tmp_path):
    import pandas as pd

    exchange = Exchange()
    exchange.clock = pd.Timestamp("2026-01-06T12:30:00Z").timestamp()
    cfg = replace(LiveConfig(instruments=(MARKET,)), bar="1Dutc")
    bot = SwapRunner(exchange, cfg, BacktestConfig(), StateStore(tmp_path / "live.json"))
    bot.initialize()
    bot.state["markets"][MARKET]["position"] = {"legs": []}
    bot.cleanup_flat(MARKET)
    assert bot.state["markets"][MARKET]["last_bar"] == "2026-01-05T00:00:00+00:00"


@pytest.mark.parametrize("bar,seconds", [("15m", 900), ("4H", 14400), ("1Dutc", 86400)])
def test_worker_trades_once_on_selected_interval(tmp_path, bar, seconds):
    import pandas as pd

    exchange = Exchange()
    original = exchange.get

    def get(path, params=None, **kwargs):
        rows = original(path, params, **kwargs)
        if path.endswith("market/candles"):
            assert params["bar"] == bar
            boundary = int(exchange.clock // seconds) * seconds
            for i, row in enumerate(rows):
                row[0] = str((boundary - i * seconds) * 1000)
        return rows

    exchange.get = get
    bot = SwapRunner(
        exchange,
        LiveConfig(instruments=(MARKET,), bar=bar, leverage=5),
        BacktestConfig(),
        StateStore(tmp_path / "live.json"),
    )
    bot.initialize()
    bot.step()
    assert not exchange.orders
    request_candidate_scan(bot)
    bot.step()
    approve_first_candidate(bot)
    bot.step()
    assert len(exchange.orders) == 1
    bot.step()
    assert len(exchange.orders) == 1
    latest = pd.Timestamp(bot.state["markets"][MARKET]["last_bar"]).timestamp()
    assert exchange.clock - latest - seconds == 30


def test_multiple_saves_before_restart_keep_original_state_baseline(tmp_path):
    store = StateStore(tmp_path / "okx-live.json")
    config, strategy, exchange = LiveConfig(instruments=(MARKET,)), BacktestConfig(), Exchange()
    bot = SwapRunner(exchange, config, strategy, store)
    bot.initialize()
    first, first_strategy = save_settings(
        config, strategy, store, {"live": {"capital_fraction": 0.4}, "strategy": {}}
    )
    save_settings(
        first,
        first_strategy,
        store,
        {"live": {"capital_fraction": 0.6, "leverage": 5}, "strategy": {}},
    )
    cfg, strat = load_settings(config, strategy, store)
    restarted = SwapRunner(exchange, cfg, strat, store)
    restarted.initialize()
    assert restarted.state["capital_ceiling"] == pytest.approx(6000)


def test_exchange_positions_block_settings_migration_before_any_writes(tmp_path):
    store = StateStore(tmp_path / "okx-live.json")
    config, strategy, exchange = LiveConfig(instruments=(MARKET,)), BacktestConfig(), Exchange()
    SwapRunner(exchange, config, strategy, store).initialize()
    save_settings(config, strategy, store, {"live": {"leverage": 5}, "strategy": {}})
    cfg, strat = load_settings(config, strategy, store)
    exchange.position = 1
    before = len(exchange.posts)
    original_state = store.load()
    with pytest.raises(ValueError, match="without open positions"):
        SwapRunner(exchange, cfg, strat, store).initialize()
    assert len(exchange.posts) == before
    assert store.load() == original_state
