import json
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from trend_pyramiding.engine import BacktestConfig
from trend_pyramiding.live import (
    LiveConfig,
    StateStore,
    SwapRunner,
    account_snapshot,
    closed_candles,
    size_contracts,
    taker_fee,
)
from trend_pyramiding.okx import Instrument, OKXError, UncertainWrite, dec

MARKET = "BTC-USDT-SWAP"
INSTRUMENT = Instrument(MARKET, dec("0.01"), dec("0.01"), dec("0.01"), dec("0.1"), dec("100000"))


class Exchange:
    """Deterministic exchange simulator: no network and no real orders."""

    def __init__(self):
        self.posts = []
        self.orders = {}
        self.algos = {}
        self.position = dec(0)
        self.lose_ack = False
        self.missing_stop = False
        self.cancel_entry = False
        self.mode = "net_mode"
        self.clock = pd.Timestamp("2026-01-06T00:00:30Z").timestamp()

    def now(self):
        return self.clock

    def sync_time(self):
        pass

    def get(self, path, params=None, **kwargs):
        if path.endswith("account/config"):
            return [
                {
                    "uid": "test-account",
                    "acctLv": "2",
                    "posMode": self.mode,
                    "autoLoan": False,
                    "perm": "read_only,trade",
                }
            ]
        if path.endswith("account/balance"):
            return [
                {
                    "totalEq": "10000",
                    "details": [
                        {"ccy": "USDT", "availBal": "10000", "eq": "10000", "eqUsd": "10000"}
                    ],
                }
            ]
        if path.endswith("account/positions"):
            return [
                {
                    "instId": MARKET,
                    "pos": str(self.position),
                    "mgnMode": "isolated",
                    "posSide": "net",
                    "lever": "2",
                }
            ]
        if path.endswith("leverage-info"):
            return [{"lever": "2"}]
        if path.endswith("trade-fee"):
            return [{"feeGroup": [{"groupId": "1", "taker": "-0.0005"}]}]
        if path.endswith("orders-pending") or path.endswith("orders-algo-pending"):
            return []
        if path.endswith("public/instruments"):
            return [
                {
                    "instId": MARKET,
                    "instType": "SWAP",
                    "ctType": "linear",
                    "settleCcy": "USDT",
                    "state": "live",
                    "ctValCcy": "BTC",
                    "ctVal": "0.01",
                    "ctMult": "1",
                    "lotSz": "0.01",
                    "minSz": "0.01",
                    "tickSz": "0.1",
                    "maxLmtSz": "100000",
                    "maxMktSz": "100000",
                    "maxStopSz": "100000",
                    "instCategory": "1",
                    "ruleType": "normal",
                }
            ]
        if path.endswith("market/ticker"):
            return [{"bidPx": "123.8", "askPx": "123.9", "ts": str(int(self.clock * 1000))}]
        if path.endswith("market/candles"):
            bars = []
            for i, ts in enumerate(pd.date_range("2026-01-01", periods=121, freq="h", tz="UTC")):
                close = 100 + i * 0.2
                bars.append(
                    [
                        str(ts.value // 1000000),
                        str(close - 0.1),
                        str(close + 0.05),
                        str(close - 0.2),
                        str(close),
                        "1000",
                        "0",
                        "0",
                        "1" if i < 120 else "0",
                    ]
                )
            return list(reversed(bars))
        if path.endswith("trade/order"):
            key = params["clOrdId"]
            if key not in self.orders:
                raise OKXError("51603", path)
            return [self.orders[key]]
        if path.endswith("trade/order-algo"):
            if params["algoClOrdId"] not in self.algos:
                raise OKXError("51603", path)
            return [self.algos[params["algoClOrdId"]].copy()]
        raise AssertionError(path)

    def post(self, path, body):
        self.posts.append((path, body))
        if path.endswith("set-leverage"):
            return [{"lever": "2"}]
        if path.endswith("amend-algos"):
            algo = next(a for a in self.algos.values() if a["algoId"] == body["algoId"])
            algo["slTriggerPx"] = body["newSlTriggerPx"]
            return [{"sCode": "0"}]
        if path.endswith("cancel-algos"):
            for req in body:
                next(a for a in self.algos.values() if a["algoId"] == req["algoId"])["state"] = (
                    "canceled"
                )
            return [{"sCode": "0"}]
        assert path.endswith("trade/order")
        canceled = self.cancel_entry and body["side"] == "buy"
        quantity = dec(0) if canceled else dec(body["sz"])
        order = {
            "ordId": str(len(self.orders) + 1),
            "accFillSz": str(quantity),
            "state": "canceled" if canceled else "filled",
            "avgPx": body.get("px", "123"),
        }
        self.orders[body["clOrdId"]] = order
        if body["side"] == "buy":
            self.position += quantity
            if quantity and not self.missing_stop:
                attached = body["attachAlgoOrds"][0]
                key = attached["attachAlgoClOrdId"]
                self.algos[key] = {
                    "algoId": "a" + order["ordId"],
                    "instId": MARKET,
                    "state": "live",
                    "side": "sell",
                    "sz": body["sz"],
                    "slTriggerPx": attached["slTriggerPx"],
                    "slOrdPx": "-1",
                }
        else:
            assert body["reduceOnly"] is True
            self.position = max(self.position - quantity, dec(0))
        if self.lose_ack:
            self.lose_ack = False
            raise UncertainWrite("lost acknowledgement")
        return [{"ordId": order["ordId"], "sCode": "0"}]


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setattr("trend_pyramiding.live.time.sleep", lambda _: None)
    exchange = Exchange()
    bot = SwapRunner(
        exchange,
        LiveConfig(instruments=(MARKET,)),
        BacktestConfig(),
        StateStore(tmp_path / "state/live.json"),
    )
    bot.initialize()
    return bot


def submit(bot):
    bot._order(
        MARKET,
        {
            "tdMode": "isolated",
            "posSide": "net",
            "side": "buy",
            "ordType": "fok",
            "sz": "10",
            "px": "123.9",
            "attachAlgoOrds": [
                {
                    "attachAlgoClOrdId": "stop1",
                    "slTriggerPx": "120",
                    "slOrdPx": "-1",
                    "slTriggerPxType": "last",
                }
            ],
        },
        {
            "kind": "entry",
            "stop": 120.0,
            "stop_id": "stop1",
            "market_budget": 2000.0,
            "risk_budget": 20.0,
        },
    )


def test_lifecycle_entry_native_stop_trail_and_exit_reconciliation(runner):
    submit(runner)
    position = runner.state["markets"][MARKET]["position"]
    assert position["qty"] == "10"
    assert not position["unsafe"]
    runner.trail(MARKET, pd.Series({"high": 140, "close": 139, "structure_low": 132, "atr": 2}))
    assert position["stop"] > 120
    assert dec(runner.client.algos["stop1"]["slTriggerPx"]) == dec(position["stop"])
    runner.client.position = dec(0)
    runner.client.algos["stop1"]["state"] = "effective"
    runner.reconcile(MARKET)
    assert runner.state["markets"][MARKET]["position"] is None


def test_unknown_ack_is_persisted_then_recovered_without_duplicate(runner):
    runner.client.lose_ack = True
    with pytest.raises(UncertainWrite):
        submit(runner)
    saved = runner.store.load()
    assert saved["pending"]["payload"]["clOrdId"] in runner.client.orders
    runner.state = saved  # Simulate process restart.
    runner.finish_order()
    assert len(runner.client.orders) == 1
    assert runner.state["pending"] is None
    assert runner.state["markets"][MARKET]["position"]["qty"] == "10"


def test_unknown_order_not_found_never_resubmits(runner):
    runner.client.lose_ack = True
    with pytest.raises(UncertainWrite):
        submit(runner)
    runner.client.orders.clear()
    count = len(runner.client.posts)
    with pytest.raises(RuntimeError, match="never auto-resend"):
        runner.finish_order()
    assert len(runner.client.posts) == count
    assert runner.store.load()["pending"] is not None


def test_native_stop_failure_attempts_reduce_only_exit_and_halts(runner):
    runner.client.missing_stop = True
    with pytest.raises(RuntimeError, match="emergency exit"):
        submit(runner)
    assert runner.client.position == 0
    exit_payload = [p for path, p in runner.client.posts if path.endswith("trade/order")][-1]
    assert exit_payload["reduceOnly"] is True and exit_payload["side"] == "sell"


def test_fok_cancel_does_not_create_a_position(runner):
    runner.client.cancel_entry = True
    submit(runner)
    assert runner.state["markets"][MARKET]["position"] is None
    assert runner.state["pending"] is None


def test_untracked_or_modified_position_blocks_trading(runner):
    runner.client.position = dec(1)
    with pytest.raises(RuntimeError, match="untracked"):
        runner.reconcile(MARKET)
    runner.client.position = dec(0)
    submit(runner)
    runner.client.position = dec(9)
    with pytest.raises(RuntimeError, match="quantity differs"):
        runner.reconcile(MARKET)


def test_size_caps_combined_capital_cash_risk_and_rounds_down():
    strategy = BacktestConfig()
    common = dict(
        price=100,
        stop=90,
        market_budget=40,
        total_budget=200,
        available=1000,
        used_margin=0,
        position=None,
        strategy=strategy,
        fee_rate=0.001,
    )
    quantity = size_contracts(INSTRUMENT, **common)
    base = float(quantity * INSTRUMENT.contract_value)
    assert base * (10 + 0.19) <= 40 * 0.01 * 0.3
    assert base * 100 * (0.5 + 0.002) <= 40 * 0.3
    assert quantity % INSTRUMENT.lot == 0
    assert size_contracts(INSTRUMENT, **{**common, "used_margin": 200}) == 0
    assert size_contracts(INSTRUMENT, **{**common, "available": 0}) == 0
    assert size_contracts(INSTRUMENT, **{**common, "stop": 101}) == 0


def test_near_global_budget_cannot_oversubscribe_across_markets():
    quantity = size_contracts(
        INSTRUMENT,
        price=100,
        stop=99.9,
        market_budget=1000,
        total_budget=200,
        available=10000,
        used_margin=199.8,
        position=None,
        strategy=BacktestConfig(),
        fee_rate=0.001,
    )
    assert float(quantity * INSTRUMENT.contract_value) * 100 * 0.502 <= 0.2 + 1e-12


def test_below_minimum_does_not_round_up():
    quantity = size_contracts(
        INSTRUMENT,
        price=100000,
        stop=90000,
        market_budget=1,
        total_budget=1,
        available=1,
        used_margin=0,
        position=None,
        strategy=BacktestConfig(),
        fee_rate=0.001,
    )
    assert quantity == 0


def test_candles_use_confirmed_only_and_reject_gaps():
    exchange = Exchange()
    frame = closed_candles(exchange, MARKET, BacktestConfig())
    assert len(frame) == 120
    assert frame.iloc[-1]["timestamp"] == pd.Timestamp("2026-01-05T23:00:00Z")
    original = exchange.get
    exchange.get = lambda path, params=None, **kwargs: (
        original(path, params, **kwargs)[:-2] + original(path, params, **kwargs)[-1:]
    )
    with pytest.raises(ValueError, match="gaps"):
        closed_candles(exchange, MARKET, BacktestConfig())


def test_step_consumes_bar_once_and_attaches_stop(runner):
    runner.step()
    entries = [p for path, p in runner.client.posts if path.endswith("trade/order")]
    assert len(entries) == 1
    assert entries[0]["ordType"] == "fok"
    assert entries[0]["attachAlgoOrds"][0]["slOrdPx"] == "-1"
    runner.step()
    assert len(runner.client.orders) == 1


def test_midbar_start_skips_stale_signal(runner):
    runner.client.clock += 600
    runner.step()
    assert not runner.client.orders
    assert runner.state["markets"][MARKET]["last_bar"] is not None


def test_hedge_mode_is_rejected_without_changing_account_mode():
    exchange = Exchange()
    exchange.mode = "long_short_mode"
    with pytest.raises(ValueError, match="net position mode"):
        account_snapshot(exchange)
    assert not exchange.posts


def test_first_start_refuses_existing_positions(tmp_path):
    exchange = Exchange()
    exchange.position = Decimal("2")
    bot = SwapRunner(
        exchange,
        LiveConfig(instruments=(MARKET,)),
        BacktestConfig(),
        StateStore(tmp_path / "live.json"),
    )
    with pytest.raises(ValueError, match="without open positions"):
        bot.initialize()
    assert not exchange.posts


def test_state_lock_prevents_duplicate_processes_and_saved_state_survives(tmp_path):
    store = StateStore(tmp_path / "state/live.json")
    with store.lock():
        with pytest.raises(RuntimeError, match="another runner"):
            with StateStore(store.path).lock():
                pass
        store.save({"version": 1, "pending": {"client_id": "tp1"}})
    assert store.load()["pending"]["client_id"] == "tp1"
    assert json.loads(store.path.read_text())["version"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"leverage": 3},
        {"capital_fraction": 0.21},
        {"capital_fraction": float("nan")},
        {"bar": "5m"},
    ],
)
def test_config_refuses_exceeding_user_limits(changes):
    with pytest.raises(ValueError):
        LiveConfig(**changes).validate()


def test_fee_uses_usdt_contract_rate_and_inst_family():
    class Fees:
        def get(self, path, params):
            assert params == {"instType": "SWAP", "instFamily": "BTC-USDT"}
            return [{"taker": "-0.0002", "takerU": "-0.0006"}]

    assert taker_fee(Fees(), INSTRUMENT) == 0.0006


def test_equity_converts_usd_to_usdt_using_exchange_valuation():
    exchange = Exchange()
    original = exchange.get

    def get(path, params=None, **kwargs):
        if path.endswith("account/balance"):
            return [
                {
                    "totalEq": "10000",
                    "details": [{"ccy": "USDT", "availBal": "1000", "eq": "1000", "eqUsd": "1020"}],
                }
            ]
        return original(path, params, **kwargs)

    exchange.get = get
    assert account_snapshot(exchange)["equity"] == pytest.approx(10000 / 1.02)


def test_add_respects_existing_risk_including_fees():
    position = {
        "qty": "100",
        "avg": 100.0,
        "risk_budget": 1.0,
        "market_budget": 1000.0,
        "legs": [{}],
    }
    # Existing 1 base coin already consumes 1.199 of risk including estimated costs.
    assert (
        size_contracts(
            INSTRUMENT,
            price=101,
            stop=99,
            market_budget=1000,
            total_budget=2000,
            available=10000,
            used_margin=50,
            position=position,
            strategy=BacktestConfig(),
            fee_rate=0.001,
        )
        == 0
    )


def test_shipped_config_loads_with_boolean_strategy_fields():
    config, strategy = LiveConfig.load(Path(__file__).resolve().parents[1] / "config/okx.toml")
    assert config.capital_fraction == 0.20
    assert config.leverage == 2
    assert strategy.require_add_breakout is False


def test_failed_stop_amendment_does_not_cancel_original_protection(runner):
    submit(runner)
    original = runner.client.post

    def post(path, body):
        if path.endswith("amend-algos"):
            raise OKXError("51000", path)
        return original(path, body)

    runner.client.post = post
    with pytest.raises(OKXError):
        runner.trail(MARKET, pd.Series({"high": 140, "close": 139, "structure_low": 132, "atr": 2}))
    assert runner.client.algos["stop1"]["state"] == "live"
    assert runner.state["markets"][MARKET]["position"]["stop"] == 120


def test_configuration_change_rejected_before_writes(runner):
    previous_writes = len(runner.client.posts)
    other = SwapRunner(
        runner.client,
        LiveConfig(instruments=(MARKET,), capital_fraction=0.1),
        BacktestConfig(),
        runner.store,
    )
    with pytest.raises(ValueError, match="account/config differs"):
        other.initialize()
    assert len(runner.client.posts) == previous_writes


def test_shutdown_before_entry_does_not_send_new_order(runner, monkeypatch):
    from trend_pyramiding import live

    stopped = False
    original = live.closed_candles

    def candles(*args):
        nonlocal stopped
        frame = original(*args)
        stopped = True
        return frame

    monkeypatch.setattr(live, "closed_candles", candles)
    runner.step(stop_requested=lambda: stopped)
    assert not runner.client.orders
