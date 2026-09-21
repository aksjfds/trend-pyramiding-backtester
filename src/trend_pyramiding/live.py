"""Persistent, long-only OKX swap runner. Real trading is explicitly opt-in."""

from __future__ import annotations

import hashlib
import json
import os
import time
import tomllib
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import pandas as pd

from .cli import _config_from_toml
from .engine import BacktestConfig, _initial_stop, _load_frame
from .indicators import atr, prior_rolling_high, rolling_structure_low
from .okx import HOSTS, Instrument, OKXClient, OKXError, dec, number, rounded, universe
from .signals import breakout_long_signal


@dataclass(frozen=True)
class LiveConfig:
    base_url: str = "https://openapi.okx.com"
    demo: bool = False
    bar: str = "1H"
    leverage: int = 2
    capital_fraction: float = 0.20
    top_n: int = 5
    instruments: tuple[str, ...] = ()
    poll_seconds: int = 10
    max_signal_age_seconds: int = 120
    max_entry_slippage_bps: float = 10.0
    max_spread_bps: float = 20.0
    strategy_config: str = "default.toml"

    def validate(self):
        if self.base_url not in HOSTS or not isinstance(self.demo, bool):
            raise ValueError("invalid host or demo flag")
        if self.bar != "1H" or self.leverage != 2:
            raise ValueError("this runner requires 1H bars and 2x isolated leverage")
        if not 0 < self.capital_fraction <= 0.20:
            raise ValueError("capital_fraction must be in (0, 0.20]")
        if (
            not isinstance(self.top_n, int)
            or not 1 <= self.top_n <= 10
            or len(set(self.instruments)) != len(self.instruments)
        ):
            raise ValueError("invalid universe")
        if len(self.instruments) > 10:
            raise ValueError("at most 10 instruments")
        if not 5 <= self.poll_seconds <= 60 or not 10 <= self.max_signal_age_seconds <= 300:
            raise ValueError("invalid polling or signal freshness settings")
        if not 0 <= self.max_entry_slippage_bps <= 50 or not 0 < self.max_spread_bps <= 50:
            raise ValueError("invalid execution price limits")

    @classmethod
    def load(cls, path: Path) -> tuple[LiveConfig, BacktestConfig]:
        raw = tomllib.loads(path.read_text())
        unknown = set(raw) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"unknown live config keys: {sorted(unknown)}")
        if "instruments" in raw:
            raw["instruments"] = tuple(raw["instruments"])
        cfg = cls(**raw)
        cfg.validate()
        strategy = _config_from_toml(path.parent / cfg.strategy_config)
        strategy.validate()
        for value in asdict(strategy).values():
            if isinstance(value, (float, int)) and not isinstance(value, bool):
                dec(value)
            elif isinstance(value, tuple):
                for weight in value:
                    dec(weight)
        for key in (
            "atr_period",
            "structure_lookback",
            "ema_period",
            "entry_breakout_lookback",
            "add_breakout_lookback",
        ):
            value = getattr(strategy, key)
            if not isinstance(value, int) or not 1 <= value <= 100:
                raise ValueError(f"live {key} must be an integer in [1, 100]")
        for key in (
            "atr_stop_mult",
            "add_step_atr",
            "trail_atr_mult",
            "trail_activation_r",
            "break_even_r",
        ):
            if getattr(strategy, key) <= 0:
                raise ValueError(f"live {key} must be positive")
        if strategy.fee_bps < 0 or strategy.structure_buffer_atr < 0:
            raise ValueError("negative fees/buffer are unsupported")
        return cfg, strategy


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    @property
    def halt_path(self):
        return self.path.with_suffix(".halted.json")

    def halt(self, error: Exception):
        marker = StateStore(self.halt_path)
        marker.save(
            {
                "version": 1,
                "error_type": type(error).__name__,
                "detail": str(error),
                "time": pd.Timestamp.now(tz="UTC").isoformat(),
            }
        )

    def clear_halt(self):
        with self.lock():
            self.halt_path.unlink(missing_ok=True)

    @contextmanager
    def lock(self):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.with_suffix(".lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("another runner is using this state file") from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def load(self):
        if not self.path.exists():
            return None
        state = json.loads(self.path.read_text())
        if state.get("version") != 1:
            raise ValueError("unsupported state version")
        return state

    def save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def event(self, event, **details):
        record = {"time": pd.Timestamp.now(tz="UTC").isoformat(), "event": event, **details}
        with self.path.with_suffix(".events.jsonl").open("a") as handle:
            handle.write(json.dumps(record, allow_nan=False) + "\n")
        print(json.dumps(record, ensure_ascii=False), flush=True)


def account_snapshot(client: OKXClient) -> dict:
    account = client.get("/api/v5/account/config")[0]
    if account.get("acctLv") not in {"2", "3"} or account.get("posMode") != "net_mode":
        raise ValueError("set OKX Futures or Multi-currency margin account to net position mode")
    balances = client.get("/api/v5/account/balance")[0]
    usdt = next((x for x in balances["details"] if x["ccy"] == "USDT"), {})
    equity_usd = float(dec(balances["totalEq"]))
    usdt_equity = float(dec(usdt.get("eq") or "0"))
    usdt_equity_usd = float(dec(usdt.get("eqUsd") or "0"))
    if usdt_equity <= 0 or usdt_equity_usd <= 0:
        raise ValueError("positive USDT collateral with an exchange USD valuation is required")
    equity = equity_usd / (usdt_equity_usd / usdt_equity)
    available = float(dec(usdt.get("availBal") or "0"))
    # Borrowed collateral is never used by this runner.
    if dec(usdt.get("liab") or "0") > 0 or account.get("autoLoan") is True:
        raise ValueError("USDT borrowing/automatic borrowing must be disabled")
    if equity <= 0:
        raise ValueError("account equity must be positive")
    return {
        "uid": account["uid"],
        "equity": equity,
        "equity_usd": equity_usd,
        "available_usdt": available,
        "trade_permission": "trade" in account.get("perm", "").split(","),
    }


def taker_fee(client: OKXClient, instrument: Instrument) -> float:
    params = {"instType": "SWAP"}
    if instrument.fee_group:
        params["groupId"] = instrument.fee_group
    else:
        params["instFamily"] = instrument.inst_id.removesuffix("-SWAP")
    row = client.get("/api/v5/account/trade-fee", params)[0]
    groups = row.get("feeGroup", [])
    if instrument.fee_group:
        groups = [g for g in groups if g.get("groupId") == instrument.fee_group]
    if len(groups) == 1 and groups[0].get("taker"):
        return abs(float(dec(groups[0]["taker"])))
    if row.get("takerU"):
        return abs(float(dec(row["takerU"])))
    raise ValueError("USDT swap taker fee could not be determined")


def closed_candles(client: OKXClient, instrument: str, strategy: BacktestConfig) -> pd.DataFrame:
    rows = client.get(
        "/api/v5/market/candles", {"instId": instrument, "bar": "1H", "limit": "300"}, private=False
    )
    closed = [r for r in rows if len(r) >= 9 and r[8] == "1"]
    if len(closed) < max(
        100, strategy.ema_period + 1, strategy.atr_period + 1, strategy.entry_breakout_lookback + 1
    ):
        raise ValueError("insufficient confirmed candle history")
    frame = pd.DataFrame(
        [r[:6] for r in closed], columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"].astype("int64"), unit="ms", utc=True)
    frame = _load_frame(frame)
    if frame.isna().any().any():
        raise ValueError("missing candle values")
    if any(not dec(v).is_finite() for v in frame[["open", "high", "low", "close"]].to_numpy().flat):
        raise ValueError("invalid candle prices")
    if not frame["timestamp"].diff().dropna().dt.total_seconds().eq(3600).all():
        raise ValueError("candle history has gaps")
    frame["atr"] = atr(frame, strategy.atr_period)
    frame["structure_low"] = rolling_structure_low(frame["low"], strategy.structure_lookback)
    frame["add_prior_high"] = prior_rolling_high(frame["high"], strategy.add_breakout_lookback)
    frame["signal"] = breakout_long_signal(
        frame, strategy.ema_period, strategy.entry_breakout_lookback
    )
    return frame


def size_contracts(
    instrument: Instrument,
    *,
    price: float,
    stop: float,
    market_budget: float,
    total_budget: float,
    available: float,
    used_margin: float,
    position: dict | None,
    strategy: BacktestConfig,
    fee_rate: float,
    leverage: int = 2,
):
    index = len(position["legs"]) if position else 0
    if index >= len(strategy.risk_weights) or not 0 < stop < price:
        return dec(0)
    qty = float(dec(position["qty"])) * float(instrument.contract_value) if position else 0.0
    risk_budget = position["risk_budget"] if position else market_budget * strategy.risk_per_trade
    old_risk = (
        max((position["avg"] - stop + fee_rate * (position["avg"] + stop)) * qty, 0)
        if position
        else 0.0
    )
    risk = min(risk_budget * strategy.risk_weights[index], max(risk_budget - old_risk, 0))
    risk_per_base = price - stop + fee_rate * (price + stop)
    market_cap = min(market_budget, position["market_budget"]) if position else market_budget
    market_cap *= strategy.max_position_pct
    # Reserve both entry and exit fees in the 20% capital envelope.
    cost_per_base = price / leverage + 2 * fee_rate * price
    remaining_market = max(market_cap - qty * cost_per_base, 0)
    margin = min(
        remaining_market,
        market_cap * strategy.allocation_weights[index],
        max(total_budget - used_margin, 0),
        max(available, 0),
    )
    base_qty = min(risk / risk_per_base, margin / cost_per_base)
    contracts = rounded(dec(base_qty) / instrument.contract_value, instrument.lot)
    remaining_contracts = instrument.max_size - (dec(position["qty"]) if position else dec(0))
    contracts = min(contracts, rounded(max(remaining_contracts, dec(0)), instrument.lot))
    return contracts if contracts >= instrument.minimum else dec(0)


class SwapRunner:
    def __init__(
        self, client: OKXClient, config: LiveConfig, strategy: BacktestConfig, store: StateStore
    ):
        self.client, self.config, self.strategy, self.store = client, config, strategy, store
        self.state = None
        self.instruments = {}
        self.fees = {}

    def save(self):
        self.store.save(self.state)

    def initialize(self):
        self.client.sync_time()
        account = account_snapshot(self.client)
        if not account["trade_permission"]:
            raise ValueError("API key requires trade permission before starting the runner")
        self.state = self.store.load()
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "live": asdict(self.config),
                    "strategy": asdict(self.strategy),
                    "uid": account["uid"],
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if self.state and self.state["fingerprint"] != fingerprint:
            raise ValueError(
                "account/config differs from saved state; do not reuse or delete active state"
            )
        requested = tuple(self.state["markets"]) if self.state else self.config.instruments
        selected = universe(self.client, self.config.top_n, requested)
        self.instruments = {i.inst_id: i for i in selected}
        positions = self.client.get("/api/v5/account/positions")
        if not self.state:
            if any(dec(p.get("pos") or "0") != 0 for p in positions):
                raise ValueError("first start requires an account without open positions")
            if self.client.get("/api/v5/trade/orders-pending"):
                raise ValueError("first start requires no pending ordinary orders")
            for kind in ("conditional", "oco", "trigger", "move_order_stop"):
                if self.client.get("/api/v5/trade/orders-algo-pending", {"ordType": kind}):
                    raise ValueError("first start requires no pending algorithmic orders")
            self.state = {
                "version": 1,
                "fingerprint": fingerprint,
                "capital_ceiling": account["equity"] * self.config.capital_fraction,
                "pending": None,
                "markets": {i.inst_id: {"last_bar": None, "position": None} for i in selected},
            }
            self.save()
        for instrument in selected:
            # Account leverage is only changed inside the user-invoked run command.
            self.client.post(
                "/api/v5/account/set-leverage",
                {"instId": instrument.inst_id, "lever": "2", "mgnMode": "isolated"},
            )
            info = self.client.get(
                "/api/v5/account/leverage-info",
                {"instId": instrument.inst_id, "mgnMode": "isolated"},
            )
            if not info or any(dec(x["lever"]) != 2 for x in info):
                raise ValueError("2x isolated leverage verification failed")
            self.fees[instrument.inst_id] = max(
                taker_fee(self.client, instrument), self.strategy.fee_bps / 10000
            )
        if self.state["pending"]:
            self.finish_order()
        self.store.event(
            "ready",
            demo=self.config.demo,
            instruments=list(self.instruments),
            capital_ceiling=self.state["capital_ceiling"],
            leverage=2,
        )

    def _order(self, market: str, payload: dict, context: dict):
        if self.state["pending"]:
            raise RuntimeError("unresolved order prevents new orders")
        client_id = "tp" + uuid.uuid4().hex[:28]
        payload = {**payload, "instId": market, "clOrdId": client_id}
        self.state["pending"] = {"market": market, "payload": payload, **context}
        self.save()  # Durable intent precedes every order submission.
        try:
            self.client.post("/api/v5/trade/order", payload)
        except OKXError as exc:
            # An explicit rejection is terminal; transport/ambiguous writes remain pending.
            self.store.event(
                "order_rejected", instrument=market, code=exc.code, client_id=client_id
            )
            self.state["pending"] = None
            self.save()
            if context["kind"] == "exit":
                raise
            return
        self.finish_order()

    def finish_order(self):
        pending = self.state["pending"]
        market, payload = pending["market"], pending["payload"]
        order = None
        for _ in range(10):
            try:
                rows = self.client.get(
                    "/api/v5/trade/order", {"instId": market, "clOrdId": payload["clOrdId"]}
                )
            except OKXError as exc:
                if exc.code == "51603":
                    raise RuntimeError(
                        "saved order not found; investigate on OKX, never auto-resend"
                    ) from None
                raise
            order = rows[0]
            if order["state"] in {"filled", "canceled", "mmp_canceled"}:
                break
            time.sleep(1)
        else:
            raise RuntimeError("order not terminal; saved intent retained for reconciliation")
        item = self.state["markets"][market]
        quantity = dec(order.get("accFillSz") or "0")
        if pending["kind"] == "exit":
            if quantity < dec(payload["sz"]):
                raise RuntimeError("emergency exit incomplete; inspect OKX position immediately")
            self.state["pending"] = None
            self.save()
            self.cleanup_flat(market)
            return
        if quantity > 0:
            average = float(dec(order["avgPx"]))
            position = item["position"]
            if position is None:
                position = {
                    "qty": "0",
                    "avg": average,
                    "first_entry": average,
                    "distance": average - pending["stop"],
                    "last_fill": average,
                    "high": average,
                    "stop": pending["stop"],
                    "risk_budget": pending["risk_budget"],
                    "market_budget": pending["market_budget"],
                    "legs": [],
                    "unsafe": True,
                }
                item["position"] = position
            old_qty = dec(position["qty"])
            position["avg"] = float(
                (dec(position["avg"]) * old_qty + dec(average) * quantity) / (old_qty + quantity)
            )
            position["qty"] = number(old_qty + quantity)
            position["last_fill"] = average
            position["unsafe"] = True
            position["legs"].append(
                {"qty": number(quantity), "order_id": order["ordId"], "stop_id": pending["stop_id"]}
            )
        self.state["pending"] = None
        self.save()
        self.store.event(
            "order_complete",
            instrument=market,
            state=order["state"],
            contracts=number(quantity),
            client_id=payload["clOrdId"],
        )
        if quantity > 0:
            if order["state"] != "filled" or quantity != dec(payload["sz"]):
                self.flatten(market, "unexpected_partial_fill")
            else:
                self.verify_protection(market)

    def actual_position(self, market):
        rows = self.client.get("/api/v5/account/positions", {"instId": market})
        rows = [p for p in rows if dec(p.get("pos") or "0") != 0]
        if any(
            p.get("mgnMode") != "isolated"
            or p.get("posSide") != "net"
            or dec(p["pos"]) < 0
            or dec(p["lever"]) != 2
            for p in rows
        ):
            raise RuntimeError("unexpected position mode, direction or leverage; inspect OKX")
        return sum((dec(p["pos"]) for p in rows), dec(0))

    def stop_details(self, stop_id):
        return self.client.get("/api/v5/trade/order-algo", {"algoClOrdId": stop_id})[0]

    def verify_protection(self, market):
        position = self.state["markets"][market]["position"]
        for _ in range(5):
            protected = True
            for leg in position["legs"]:
                try:
                    algo = self.stop_details(leg["stop_id"])
                    protected &= (
                        algo["state"] == "live"
                        and algo["side"] == "sell"
                        and algo["instId"] == market
                        and dec(algo["sz"]) == dec(leg["qty"])
                        and dec(algo["slTriggerPx"]) >= dec(position["stop"])
                        and algo["slOrdPx"] == "-1"
                    )
                except OKXError as exc:
                    if exc.code not in {"51603", "51600"}:
                        raise
                    protected = False
            if protected:
                position["unsafe"] = False
                self.save()
                return
            if self.actual_position(market) == 0:
                self.cleanup_flat(market)
                return
            time.sleep(1)
        self.flatten(market, "protective_stop_not_confirmed")

    def cleanup_flat(self, market):
        if self.actual_position(market) != 0:
            raise RuntimeError("cannot clear state while exchange position remains open")
        position = self.state["markets"][market]["position"]
        if position:
            for leg in position["legs"]:
                try:
                    algo = self.stop_details(leg["stop_id"])
                except OKXError as exc:
                    if exc.code in {"51603", "51600"}:
                        continue
                    raise
                if algo["state"] == "live":
                    self.client.post(
                        "/api/v5/trade/cancel-algos", [{"instId": market, "algoId": algo["algoId"]}]
                    )
            # Do not act on a signal that predates this exit.
            close_boundary = int(self.client.now() // 3600) * 3600 - 3600
            self.state["markets"][market]["last_bar"] = pd.Timestamp(
                close_boundary, unit="s", tz="UTC"
            ).isoformat()
        self.state["markets"][market]["position"] = None
        self.save()
        self.store.event("position_closed", instrument=market)

    def flatten(self, market, reason):
        quantity = self.actual_position(market)
        self.store.event("emergency_exit", instrument=market, reason=reason)
        if quantity == 0:
            self.cleanup_flat(market)
        else:
            self._order(
                market,
                {
                    "tdMode": "isolated",
                    "side": "sell",
                    "posSide": "net",
                    "ordType": "market",
                    "sz": number(quantity),
                    "reduceOnly": True,
                },
                {"kind": "exit"},
            )
        raise RuntimeError(f"runner stopped after emergency exit for {market}: {reason}")

    def reconcile(self, market):
        position = self.state["markets"][market]["position"]
        actual = self.actual_position(market)
        if position is None:
            if actual:
                raise RuntimeError("untracked exchange position; no new orders allowed")
            return
        if actual == 0:
            self.cleanup_flat(market)
            return
        if actual != dec(position["qty"]):
            raise RuntimeError(
                "position quantity differs from state; inspect fills before restarting"
            )
        self.verify_protection(market)

    def trail(self, market, row):
        position = self.state["markets"][market]["position"]
        if position is None:
            return
        position["high"] = max(position["high"], float(row["high"]))
        stop = position["stop"]
        gain = position["high"] - position["first_entry"]
        if gain >= self.strategy.break_even_r * position["distance"]:
            stop = max(stop, position["first_entry"])
        if gain >= self.strategy.trail_activation_r * position["distance"]:
            stop = max(
                stop,
                float(row["structure_low"] - self.strategy.structure_buffer_atr * row["atr"]),
                float(row["close"] - self.strategy.trail_atr_mult * row["atr"]),
            )
        stop = float(
            rounded(min(stop, float(row["close"]) * (1 - 1e-9)), self.instruments[market].tick)
        )
        self.save()
        if stop <= position["stop"]:
            return
        for leg in position["legs"]:
            algo = self.stop_details(leg["stop_id"])
            if algo["state"] != "live":
                raise RuntimeError("stop is executing; pause and reconcile before further orders")
            if dec(algo["slTriggerPx"]) < dec(stop):
                self.client.post(
                    "/api/v5/trade/amend-algos",
                    {
                        "instId": market,
                        "algoId": algo["algoId"],
                        "newSlTriggerPx": number(stop),
                        "newSlOrdPx": "-1",
                        "cxlOnFail": False,
                    },
                )
                confirmed = self.stop_details(leg["stop_id"])
                if confirmed["state"] != "live" or dec(confirmed["slTriggerPx"]) < dec(stop):
                    raise RuntimeError(
                        "stop amendment unconfirmed; original stop retained, runner halted"
                    )
        position["stop"] = stop
        self.save()
        self.store.event("stop_raised", instrument=market, stop=stop)

    def quote(self, market):
        ticker = self.client.get("/api/v5/market/ticker", {"instId": market}, private=False)[0]
        if not 0 <= self.client.now() - float(ticker["ts"]) / 1000 <= 15:
            raise ValueError("stale market quote")
        bid, ask = float(dec(ticker["bidPx"])), float(dec(ticker["askPx"]))
        if bid <= 0 or ask < bid or (ask - bid) / bid * 10000 > self.config.max_spread_bps:
            raise ValueError("market spread exceeds execution limit")
        return bid, ask

    def step(self, stop_requested=lambda: False):
        if self.state["pending"]:
            self.finish_order()
        if self.client.get("/api/v5/trade/orders-pending"):
            raise RuntimeError("unexpected pending ordinary orders; reconcile before trading")
        for market in self.instruments:
            self.reconcile(market)
        account = account_snapshot(self.client)
        budget = min(
            self.state["capital_ceiling"], account["equity"] * self.config.capital_fraction
        )
        used_margin = 0.0
        for market, instrument in self.instruments.items():
            position = self.state["markets"][market]["position"]
            if position:
                _, ask = self.quote(market)
                notional = float(dec(position["qty"]) * instrument.contract_value) * max(
                    ask, position["avg"]
                )
                used_margin += notional * (1 / self.config.leverage + 2 * self.fees[market])
        for market, instrument in self.instruments.items():
            if stop_requested():
                return
            frame = closed_candles(self.client, market, self.strategy)
            row = frame.iloc[-1]
            bar = row["timestamp"].isoformat()
            item = self.state["markets"][market]
            if item["last_bar"] is not None and pd.Timestamp(bar) <= pd.Timestamp(item["last_bar"]):
                continue
            age = self.client.now() - row["timestamp"].timestamp() - 3600
            if age < 0 or age > 3700:
                raise ValueError("latest confirmed candle is stale or in the future")
            if item["position"] and item["last_bar"]:
                missed = frame[frame["timestamp"] > pd.Timestamp(item["last_bar"])]
                if len(missed):
                    item["position"]["high"] = max(
                        item["position"]["high"], float(missed["high"].max())
                    )
            # Persist consumption before any action: restart never repeats this bar.
            item["last_bar"] = bar
            self.save()
            self.trail(market, row)
            position = item["position"]
            if age > self.config.max_signal_age_seconds:
                self.store.event("skip_stale_entry", instrument=market, age_seconds=round(age))
                continue
            if position:
                signal = (
                    len(position["legs"]) < len(self.strategy.risk_weights)
                    and row["close"] > position["avg"]
                    and row["close"]
                    >= position["last_fill"] + self.strategy.add_step_atr * row["atr"]
                    and (
                        not self.strategy.require_add_breakout
                        or row["close"] > row["add_prior_high"]
                    )
                )
                stop = position["stop"]
            else:
                signal = bool(row["signal"])
                stop = _initial_stop(row, self.strategy)
            if not signal or stop is None:
                continue
            stop = float(rounded(stop, instrument.tick))
            bid, ask = self.quote(market)
            # Skip a signal if price already crossed its stop or retraced its add threshold.
            if bid <= stop:
                continue
            if position and (
                ask <= position["avg"]
                or ask < position["last_fill"] + self.strategy.add_step_atr * float(row["atr"])
            ):
                continue
            limit = rounded(ask * (1 + self.config.max_entry_slippage_bps / 10000), instrument.tick)
            if float(limit) < ask:
                continue
            market_budget = budget / len(self.instruments)
            quantity = size_contracts(
                instrument,
                price=float(limit),
                stop=stop,
                market_budget=market_budget,
                total_budget=budget,
                available=account["available_usdt"],
                used_margin=used_margin,
                position=position,
                strategy=self.strategy,
                fee_rate=self.fees[market],
                leverage=self.config.leverage,
            )
            if quantity == 0:
                self.store.event("skip_below_minimum_or_budget", instrument=market)
                continue
            # Recheck exchange position immediately before sending an add.
            self.reconcile(market)
            if item["position"] is not position:
                continue
            if stop_requested():
                return
            stop_id = "ts" + uuid.uuid4().hex[:28]
            risk_budget = (
                position["risk_budget"]
                if position
                else market_budget * self.strategy.risk_per_trade
            )
            self._order(
                market,
                {
                    "tdMode": "isolated",
                    "posSide": "net",
                    "side": "buy",
                    "ordType": "fok",
                    "sz": number(quantity),
                    "px": number(limit),
                    "attachAlgoOrds": [
                        {
                            "attachAlgoClOrdId": stop_id,
                            "slTriggerPx": number(stop),
                            "slOrdPx": "-1",
                            "slTriggerPxType": "last",
                        }
                    ],
                },
                {
                    "kind": "entry",
                    "stop": stop,
                    "stop_id": stop_id,
                    "market_budget": market_budget,
                    "risk_budget": risk_budget,
                },
            )
            # Reserve conservatively for this iteration, even if FOK was canceled.
            cost = float(quantity * instrument.contract_value * limit) * (
                0.5 + 2 * self.fees[market]
            )
            used_margin += cost
            account["available_usdt"] = max(account["available_usdt"] - cost, 0)
