"""Persistent, long-only OKX swap runner. Real trading is explicitly opt-in."""

from __future__ import annotations

import hashlib
import json
import os
import time
import tomllib
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

import pandas as pd

from .cli import _config_from_toml
from .engine import BacktestConfig, _initial_stop, _load_frame
from .indicators import atr, prior_rolling_high, rolling_structure_low
from .okx import (
    HOSTS,
    Instrument,
    OKXClient,
    OKXError,
    TransientRead,
    dec,
    number,
    rounded,
    universe,
)
from .runtime import safe_console_print
from .signals import breakout_long_signal

CONTROL_COMMAND_TTL_SECONDS = 30

BAR_SECONDS = {
    "15m": 900,
    "30m": 1800,
    "1H": 3600,
    "2H": 7200,
    "4H": 14400,
    "6Hutc": 21600,
    "12Hutc": 43200,
    "1Dutc": 86400,
}


class MarketUnavailable(ValueError):
    """Public market data is temporarily unsuitable for execution."""


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
        if self.bar not in BAR_SECONDS:
            raise ValueError("unsupported candle interval")
        if type(self.leverage) is not int or not 1 <= self.leverage <= 125:
            raise ValueError("leverage must be an integer in [1, 125]")
        if isinstance(self.capital_fraction, bool) or not 0 < self.capital_fraction <= 1:
            raise ValueError("capital_fraction must be in (0, 1]")
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
        validate_strategy(strategy)
        return cfg, strategy


def validate_strategy(strategy):
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
        # Keep disk use bounded; durable orders/positions live in the separate state file.
        path = self.path.with_suffix(".events.jsonl")
        if path.exists() and path.stat().st_size >= 5 * 1024 * 1024:
            for index in range(4, 0, -1):
                source = path.with_name(path.name + f".{index}")
                if source.exists():
                    os.replace(source, path.with_name(path.name + f".{index + 1}"))
            os.replace(path, path.with_name(path.name + ".1"))
        record = {"time": pd.Timestamp.now(tz="UTC").isoformat(), "event": event, **details}
        with path.open("a") as handle:
            handle.write(json.dumps(record, allow_nan=False) + "\n")
        safe_console_print(json.dumps(record, ensure_ascii=False), flush=True)


def account_snapshot(client: OKXClient, *, for_trading=True) -> dict:
    warnings = []
    try:
        account = client.get("/api/v5/account/config")[0]
    except (ValueError, RuntimeError, OSError, IndexError) as exc:
        if for_trading:
            raise
        account = {}
        warnings.append(f"账户配置读取失败：{exc}")
    balances = client.get("/api/v5/account/balance")[0]
    usdt = next((x for x in balances["details"] if x["ccy"] == "USDT"), {})
    equity_usd = float(dec(balances["totalEq"]))
    usdt_equity = float(dec(usdt.get("eq") or "0"))
    usdt_equity_usd = float(dec(usdt.get("eqUsd") or "0"))
    equity = (
        equity_usd / (usdt_equity_usd / usdt_equity)
        if usdt_equity > 0 and usdt_equity_usd > 0
        else None
    )
    available_raw = usdt.get("availBal")
    if available_raw in (None, ""):
        available_raw = usdt.get("availEq")
    available = (
        float(dec(available_raw))
        if available_raw not in (None, "")
        else (0.0 if not usdt else None)
    )
    problems = []
    if account.get("acctLv") not in {"2", "3"} or account.get("posMode") != "net_mode":
        problems.append("set OKX Futures or Multi-currency margin account to net position mode")
    if equity is None:
        problems.append("positive USDT collateral with an exchange USD valuation is required")
    if dec(usdt.get("liab") or "0") > 0 or account.get("autoLoan") in (True, "true"):
        problems.append("USDT borrowing/automatic borrowing must be disabled")
    if equity_usd <= 0:
        problems.append("account equity must be positive")
    if available is None:
        problems.append("available USDT balance is unavailable")
    if for_trading and problems:
        raise ValueError(problems[0])
    warnings.extend(problems)
    return {
        "uid": account.get("uid"),
        "equity": equity,
        "equity_usd": equity_usd,
        "available_usdt": available,
        "trade_permission": "trade" in account.get("perm", "").split(","),
        "warnings": warnings,
    }


def selected_config(config, store):
    selection = StateStore(store.path.with_suffix(".selection.json")).load()
    if selection is None:
        return config
    instruments = selection.get("instruments")
    if not isinstance(instruments, list) or any(not isinstance(x, str) for x in instruments):
        raise ValueError("币种选择文件格式错误")
    config = replace(config, instruments=tuple(instruments))
    config.validate()
    return config


def config_fingerprint(config, strategy, uid):
    return hashlib.sha256(
        json.dumps(
            {"live": asdict(config), "strategy": asdict(strategy), "uid": uid}, sort_keys=True
        ).encode()
    ).hexdigest()


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


def closed_candles(
    client: OKXClient, instrument: str, strategy: BacktestConfig, bar="1H"
) -> pd.DataFrame:
    rows = client.get(
        "/api/v5/market/candles", {"instId": instrument, "bar": bar, "limit": "300"}, private=False
    )
    closed = [r for r in rows if len(r) >= 9 and r[8] == "1"]
    if len(closed) < max(
        100,
        strategy.ema_period + 1,
        strategy.atr_period + 1,
        strategy.entry_breakout_lookback + 1,
        strategy.structure_lookback + 1,
        strategy.add_breakout_lookback + 1,
    ):
        raise MarketUnavailable("insufficient confirmed candle history")
    frame = pd.DataFrame(
        [r[:6] for r in closed], columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"].astype("int64"), unit="ms", utc=True)
    frame = _load_frame(frame)
    if frame.isna().any().any():
        raise MarketUnavailable("missing candle values")
    if any(not dec(v).is_finite() for v in frame[["open", "high", "low", "close"]].to_numpy().flat):
        raise MarketUnavailable("invalid candle prices")
    if not frame["timestamp"].diff().dropna().dt.total_seconds().eq(BAR_SECONDS[bar]).all():
        raise MarketUnavailable("candle history has gaps")
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
    # Reserve both entry and exit fees within the configured capital envelope.
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
        self.market_warnings = {}
        self.stop_requested = lambda: False
        self.entry_candidates = {}
        self.candidate_store = StateStore(self.store.path.with_suffix(".candidates.json"))
        self.approval_store = StateStore(self.store.path.with_suffix(".entry-approvals.json"))
        self.scan_store = StateStore(self.store.path.with_suffix(".candidate-scan.json"))
        self.manual_entry_store = StateStore(self.store.path.with_suffix(".manual-entry.json"))

    def save(self):
        self.store.save(self.state)

    def initialize(self):
        self.client.sync_time()
        account = account_snapshot(self.client)
        if not account["trade_permission"]:
            raise ValueError("API key requires trade permission before starting the runner")
        self.state = self.store.load()
        fingerprint = config_fingerprint(self.config, self.strategy, account["uid"])
        selection_changed = False
        previous_capital_fraction = None
        if self.state and self.state["fingerprint"] != fingerprint:
            # Universe changes or explicitly saved parameters require a matching prior account and flat state.
            old_choices = [(), tuple(self.state["markets"])]
            if self.state.get("configured_instruments") is not None:
                old_choices.append(tuple(self.state["configured_instruments"]))
            selection_changed = any(
                config_fingerprint(
                    replace(self.config, instruments=choice), self.strategy, account["uid"]
                )
                == self.state["fingerprint"]
                for choice in old_choices
            )
            if not selection_changed:
                from .settings import migration_source

                previous = migration_source(self.store, self.state, account["uid"], self.config)
                if previous:
                    selection_changed = True
                    previous_capital_fraction = previous.capital_fraction
            if (
                not selection_changed
                or self.state["pending"]
                or any(m["position"] for m in self.state["markets"].values())
            ):
                raise ValueError(
                    "account/config differs from saved state; do not reuse or delete active state"
                )
        requested = (
            tuple(self.state["markets"])
            if self.state and not selection_changed
            else self.config.instruments
        )
        selected = universe(self.client, self.config.top_n, requested)
        self.instruments = {i.inst_id: i for i in selected}
        positions = self.client.get("/api/v5/account/positions")
        if not self.state or selection_changed:
            if any(dec(p.get("pos") or "0") != 0 for p in positions):
                raise ValueError("first start requires an account without open positions")
            if self.client.get("/api/v5/trade/orders-pending"):
                raise ValueError("first start requires no pending ordinary orders")
            for kind in ("conditional", "oco", "trigger", "move_order_stop"):
                if self.client.get("/api/v5/trade/orders-algo-pending", {"ordType": kind}):
                    raise ValueError("first start requires no pending algorithmic orders")
            ceiling = (
                self.state["capital_ceiling"]
                if self.state
                else account["equity"] * self.config.capital_fraction
            )
            if self.state and previous_capital_fraction is not None:
                ceiling *= self.config.capital_fraction / previous_capital_fraction
            self.state = {
                "version": 1,
                "configured_instruments": list(self.config.instruments),
                "fingerprint": fingerprint,
                "configuration": {"live": asdict(self.config), "strategy": asdict(self.strategy)},
                "capital_ceiling": ceiling,
                "pending": None,
                "markets": {i.inst_id: {"last_bar": None, "position": None} for i in selected},
            }
            self.save()
        else:
            if self.state["pending"]:
                self.finish_order()
            positions = self.client.get("/api/v5/account/positions")
            self._verify_global_exchange_state(positions)

        for instrument in selected:
            if self.stop_requested():
                return
            # Account leverage is only changed inside the user-invoked run command.
            self.client.post(
                "/api/v5/account/set-leverage",
                {
                    "instId": instrument.inst_id,
                    "lever": str(self.config.leverage),
                    "mgnMode": "isolated",
                },
            )
            info = self.client.get(
                "/api/v5/account/leverage-info",
                {"instId": instrument.inst_id, "mgnMode": "isolated"},
            )
            if not info or any(dec(x["lever"]) != self.config.leverage for x in info):
                raise ValueError("configured isolated leverage verification failed")
            self.fees[instrument.inst_id] = max(
                taker_fee(self.client, instrument), self.strategy.fee_bps / 10000
            )
        try:
            saved_candidates = self.candidate_store.load() or {"version": 1, "candidates": []}
            candidates = saved_candidates.get("candidates", [])
            if not isinstance(candidates, list):
                raise ValueError("invalid entry candidate state")
            now = self.client.now()
            self.entry_candidates = {
                item["instrument"]: item
                for item in candidates
                if (
                    isinstance(item, dict)
                    and item.get("instrument") in self.instruments
                    and float(item.get("expires_at", 0)) > now
                    and self.state["markets"][item["instrument"]]["position"] is None
                )
            }
        except (ValueError, OSError, KeyError, TypeError):
            self.entry_candidates = {}
        self._save_candidates()
        # Click commands are ephemeral: never replay them after a worker restart.
        self.approval_store.save({"version": 1, "approvals": []})
        self.scan_store.save({"version": 1, "request": None})
        self.manual_entry_store.save({"version": 1, "request": None})
        self.store.event(
            "ready",
            demo=self.config.demo,
            instruments=list(self.instruments),
            capital_ceiling=self.state["capital_ceiling"],
            leverage=self.config.leverage,
        )

    def _verify_global_exchange_state(self, positions):
        managed = self.state.get("markets", {})
        for row in positions:
            quantity = dec(row.get("pos") or "0")
            if quantity == 0:
                continue
            market = row.get("instId")
            if market not in managed:
                raise RuntimeError(
                    f"untracked exchange position outside managed instruments: {market}"
                )
            if managed[market].get("position") is None:
                raise RuntimeError(
                    f"exchange position exists without strategy state: {market}"
                )

        if self.client.get("/api/v5/trade/orders-pending"):
            raise RuntimeError("unexpected pending ordinary orders; reconcile before trading")

        tracked_stops = set()
        pending = self.state.get("pending") or {}
        if pending.get("stop_id"):
            tracked_stops.add(str(pending["stop_id"]))
        for market_state in managed.values():
            position = market_state.get("position") or {}
            for leg in position.get("legs", []):
                if leg.get("stop_id"):
                    tracked_stops.add(str(leg["stop_id"]))

        for kind in ("conditional", "oco", "trigger", "move_order_stop"):
            for row in self.client.get(
                "/api/v5/trade/orders-algo-pending", {"ordType": kind}
            ):
                client_id = str(
                    row.get("algoClOrdId")
                    or row.get("attachAlgoClOrdId")
                    or ""
                )
                if not client_id or client_id not in tracked_stops:
                    raise RuntimeError(
                        "unexpected pending algorithmic order; inspect OKX before trading"
                    )

    def _save_candidates(self):
        self.candidate_store.save(
            {"version": 1, "candidates": list(self.entry_candidates.values())}
        )

    def _candidate_id(self, market: str, bar: str) -> str:
        return hashlib.sha256(f"{market}|{bar}".encode()).hexdigest()[:24]

    def _remove_candidate(self, market: str):
        if self.entry_candidates.pop(market, None) is not None:
            self._save_candidates()

    def _expire_candidates(self):
        now = self.client.now()
        changed = False
        for market, candidate in list(self.entry_candidates.items()):
            if (
                candidate["expires_at"] <= now
                or self.state["markets"].get(market, {}).get("position") is not None
            ):
                self.entry_candidates.pop(market, None)
                changed = True
        if changed:
            self._save_candidates()

    def _publish_candidate(
        self,
        market: str,
        *,
        bar: str,
        close: float,
        stop: float,
        contracts,
        expires_at: float,
    ):
        candidate_id = self._candidate_id(market, bar)
        existing = self.entry_candidates.get(market)
        candidate = {
            "id": candidate_id,
            "instrument": market,
            "bar": bar,
            "signal_close": float(close),
            "stop": float(stop),
            "indicative_contracts": number(contracts),
            "detected_at": self.client.now(),
            "expires_at": float(expires_at),
        }
        self.entry_candidates[market] = candidate
        self._save_candidates()
        if not existing or existing.get("id") != candidate_id:
            self.store.event(
                "entry_candidate",
                instrument=market,
                candidate_id=candidate_id,
                bar=bar,
                close=float(close),
                stop=float(stop),
                indicative_contracts=number(contracts),
            )

    def _take_scan_request(self):
        try:
            with self.scan_store.lock():
                data = self.scan_store.load() or {"version": 1, "request": None}
                request = data.get("request")
                self.scan_store.save({"version": 1, "request": None})
        except RuntimeError:
            return None
        if not isinstance(request, dict) or not request.get("id"):
            return None
        if float(request.get("expires_at", 0)) <= self.client.now():
            self.store.event(
                "candidate_scan_expired",
                request_id=str(request["id"]),
            )
            return None
        return request

    def _process_candidate_scan(self, account, budget, used_margin, stop_requested):
        request = self._take_scan_request()
        if request is None:
            return

        self.entry_candidates = {}
        self._save_candidates()
        request_id = str(request["id"])
        checked = 0
        found = 0
        self.store.event(
            "candidate_scan_started",
            request_id=request_id,
            instruments=list(self.instruments),
        )

        for market, instrument in self.instruments.items():
            if stop_requested():
                return
            item = self.state["markets"][market]
            if item["position"] is not None:
                continue
            try:
                frame = closed_candles(self.client, market, self.strategy, self.config.bar)
                self.quote(market)
            except (MarketUnavailable, TransientRead) as exc:
                self.market_warning(market, exc)
                continue

            row = frame.iloc[-1]
            bar = row["timestamp"].isoformat()
            age = self.client.now() - row["timestamp"].timestamp() - BAR_SECONDS[self.config.bar]
            if age < 0 or age > BAR_SECONDS[self.config.bar] + 100:
                self.market_warning(market, "latest confirmed candle is stale or in the future")
                continue
            if self.market_warnings.pop(market, None):
                self.store.event("market_recovered", instrument=market)

            checked += 1
            stop = _initial_stop(row, self.strategy)
            if not bool(row["signal"]) or stop is None:
                continue
            stop = float(rounded(stop, instrument.tick))
            try:
                bid, ask = self.quote(market)
            except (MarketUnavailable, TransientRead) as exc:
                self.market_warning(market, exc)
                continue
            if bid <= stop:
                continue

            limit = rounded(
                ask * (1 + self.config.max_entry_slippage_bps / 10000),
                instrument.tick,
            )
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
                position=None,
                strategy=self.strategy,
                fee_rate=self.fees[market],
                leverage=self.config.leverage,
            )
            if quantity == 0:
                continue

            next_bar_close = (
                row["timestamp"].timestamp() + 2 * BAR_SECONDS[self.config.bar]
            )
            self._publish_candidate(
                market,
                bar=bar,
                close=float(row["close"]),
                stop=stop,
                contracts=quantity,
                expires_at=min(
                    self.client.now() + self.config.max_signal_age_seconds,
                    next_bar_close,
                ),
            )
            found += 1

        self.store.event(
            "candidate_scan_completed",
            request_id=request_id,
            checked_markets=checked,
            candidates=found,
        )

    def _take_manual_entry_request(self):
        try:
            with self.manual_entry_store.lock():
                data = self.manual_entry_store.load() or {"version": 1, "request": None}
                request = data.get("request")
                self.manual_entry_store.save({"version": 1, "request": None})
        except RuntimeError:
            return None
        if not isinstance(request, dict) or not request.get("id") or not request.get("instrument"):
            return None
        if float(request.get("expires_at", 0)) <= self.client.now():
            self.store.event(
                "manual_entry_rejected",
                request_id=str(request["id"]),
                instrument=str(request["instrument"]),
                reason="command_expired",
            )
            return None
        return request

    def _process_manual_entry_request(
        self, account, budget, used_margin, stop_requested
    ):
        request = self._take_manual_entry_request()
        if request is None:
            return used_margin

        request_id = str(request["id"])
        market = str(request["instrument"])
        item = self.state["markets"].get(market)
        instrument = self.instruments.get(market)
        if item is None or instrument is None:
            self.store.event(
                "manual_entry_rejected",
                request_id=request_id,
                instrument=market,
                reason="instrument_not_managed",
            )
            return used_margin
        if item["position"] is not None:
            self.store.event(
                "manual_entry_rejected",
                request_id=request_id,
                instrument=market,
                reason="position_already_exists",
            )
            return used_margin
        if stop_requested():
            return used_margin

        try:
            frame = closed_candles(self.client, market, self.strategy, self.config.bar)
            row = frame.iloc[-1]
            age = (
                self.client.now()
                - row["timestamp"].timestamp()
                - BAR_SECONDS[self.config.bar]
            )
            if age < 0 or age > BAR_SECONDS[self.config.bar] + 100:
                raise ValueError("latest confirmed candle is stale or in the future")
            stop = _initial_stop(row, self.strategy)
            if stop is None:
                raise ValueError("strategy_stop_unavailable")
            stop = float(rounded(stop, instrument.tick))
            bid, ask = self.quote(market)
        except (MarketUnavailable, TransientRead, ValueError) as exc:
            self.store.event(
                "manual_entry_rejected",
                request_id=request_id,
                instrument=market,
                reason=str(exc),
            )
            return used_margin

        if bid <= stop:
            self.store.event(
                "manual_entry_rejected",
                request_id=request_id,
                instrument=market,
                reason="price_crossed_stop",
            )
            return used_margin

        limit = rounded(
            ask * (1 + self.config.max_entry_slippage_bps / 10000),
            instrument.tick,
        )
        if float(limit) < ask:
            self.store.event(
                "manual_entry_rejected",
                request_id=request_id,
                instrument=market,
                reason="invalid_limit_price",
            )
            return used_margin

        market_budget = budget / len(self.instruments)
        quantity = size_contracts(
            instrument,
            price=float(limit),
            stop=stop,
            market_budget=market_budget,
            total_budget=budget,
            available=account["available_usdt"],
            used_margin=used_margin,
            position=None,
            strategy=self.strategy,
            fee_rate=self.fees[market],
            leverage=self.config.leverage,
        )
        if quantity == 0:
            self.store.event(
                "manual_entry_rejected",
                request_id=request_id,
                instrument=market,
                reason="below_minimum_or_budget",
            )
            return used_margin

        self.reconcile(market)
        if item["position"] is not None or stop_requested():
            return used_margin

        stop_id = "ts" + uuid.uuid4().hex[:28]
        risk_budget = market_budget * self.strategy.risk_per_trade
        self.store.event(
            "manual_entry_received",
            request_id=request_id,
            instrument=market,
            contracts=number(quantity),
            bar=row["timestamp"].isoformat(),
            stop=stop,
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
        if item["position"] is not None:
            item["last_bar"] = row["timestamp"].isoformat()
            self.save()
            self._remove_candidate(market)

        cost = float(quantity * instrument.contract_value * limit) * (
            1 / self.config.leverage + 2 * self.fees[market]
        )
        used_margin += cost
        account["available_usdt"] = max(account["available_usdt"] - cost, 0)
        return used_margin

    def _approval_requests(self):
        try:
            with self.approval_store.lock():
                data = self.approval_store.load() or {"version": 1, "approvals": []}
                approvals = data.get("approvals", [])
                if not isinstance(approvals, list):
                    raise ValueError("invalid entry approval queue")
                return [
                    item
                    for item in approvals
                    if isinstance(item, dict) and item.get("id")
                ]
        except RuntimeError:
            return []

    def _ack_approval(self, candidate_id: str):
        try:
            with self.approval_store.lock():
                data = self.approval_store.load() or {"version": 1, "approvals": []}
                approvals = [
                    item
                    for item in data.get("approvals", [])
                    if not isinstance(item, dict) or str(item.get("id")) != candidate_id
                ]
                self.approval_store.save({"version": 1, "approvals": approvals})
        except RuntimeError:
            return

    def _process_entry_approvals(self, account, budget, used_margin, stop_requested):
        self._expire_candidates()
        approvals = self._approval_requests()
        if not approvals:
            return used_margin

        request = approvals[0]
        candidate_id = str(request["id"])
        for extra in approvals[1:]:
            extra_id = str(extra.get("id"))
            self.store.event(
                "entry_approval_rejected",
                candidate_id=extra_id,
                reason="concurrent_approval_discarded",
            )
            self._ack_approval(extra_id)

        if stop_requested():
            return used_margin
        if float(request.get("expires_at", 0)) <= self.client.now():
            self.store.event(
                "entry_approval_rejected",
                candidate_id=candidate_id,
                reason="command_expired",
            )
            self._ack_approval(candidate_id)
            return used_margin

        by_id = {candidate["id"]: candidate for candidate in self.entry_candidates.values()}
        candidate = by_id.get(candidate_id)
        if candidate is None:
            self._ack_approval(candidate_id)
            return used_margin

        market = candidate["instrument"]
        item = self.state["markets"].get(market)
        instrument = self.instruments.get(market)
        if item is None or instrument is None or item["position"] is not None:
            self._ack_approval(candidate_id)
            self._remove_candidate(market)
            return used_margin
        if candidate["expires_at"] <= self.client.now():
            self.store.event(
                "entry_approval_rejected",
                instrument=market,
                candidate_id=candidate_id,
                reason="signal_expired",
            )
            self._ack_approval(candidate_id)
            self._remove_candidate(market)
            return used_margin

        stop = float(candidate["stop"])
        try:
            bid, ask = self.quote(market)
        except (MarketUnavailable, TransientRead) as exc:
            self.store.event(
                "entry_approval_rejected",
                instrument=market,
                candidate_id=candidate_id,
                reason=str(exc),
            )
            self._ack_approval(candidate_id)
            return used_margin
        if bid <= stop:
            self.store.event(
                "entry_approval_rejected",
                instrument=market,
                candidate_id=candidate_id,
                reason="price_crossed_stop",
            )
            self._ack_approval(candidate_id)
            self._remove_candidate(market)
            return used_margin

        limit = rounded(
            ask * (1 + self.config.max_entry_slippage_bps / 10000),
            instrument.tick,
        )
        if float(limit) < ask:
            self._ack_approval(candidate_id)
            return used_margin
        market_budget = budget / len(self.instruments)
        quantity = size_contracts(
            instrument,
            price=float(limit),
            stop=stop,
            market_budget=market_budget,
            total_budget=budget,
            available=account["available_usdt"],
            used_margin=used_margin,
            position=None,
            strategy=self.strategy,
            fee_rate=self.fees[market],
            leverage=self.config.leverage,
        )
        if quantity == 0:
            self.store.event(
                "entry_approval_rejected",
                instrument=market,
                candidate_id=candidate_id,
                reason="below_minimum_or_budget",
            )
            self._ack_approval(candidate_id)
            self._remove_candidate(market)
            return used_margin

        self.reconcile(market)
        if item["position"] is not None:
            self._ack_approval(candidate_id)
            self._remove_candidate(market)
            return used_margin
        if stop_requested():
            return used_margin

        stop_id = "ts" + uuid.uuid4().hex[:28]
        risk_budget = market_budget * self.strategy.risk_per_trade
        self.store.event(
            "entry_approval_received",
            instrument=market,
            candidate_id=candidate_id,
            contracts=number(quantity),
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
        self._ack_approval(candidate_id)
        if item["position"] is not None:
            item["last_bar"] = candidate["bar"]
            self.save()
            self._remove_candidate(market)

        cost = float(quantity * instrument.contract_value * limit) * (
            1 / self.config.leverage + 2 * self.fees[market]
        )
        used_margin += cost
        account["available_usdt"] = max(account["available_usdt"] - cost, 0)
        return used_margin

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
            or dec(p["lever"]) != self.config.leverage
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
            seconds = BAR_SECONDS[self.config.bar]
            close_boundary = int(self.client.now() // seconds) * seconds - seconds
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
        rows = self.client.get("/api/v5/market/ticker", {"instId": market}, private=False)
        if not rows or not rows[0].get("bidPx") or not rows[0].get("askPx"):
            raise MarketUnavailable("market has no executable quote")
        ticker = rows[0]
        if not 0 <= self.client.now() - float(ticker["ts"]) / 1000 <= 15:
            raise MarketUnavailable("stale market quote")
        bid, ask = float(dec(ticker["bidPx"])), float(dec(ticker["askPx"]))
        if bid <= 0 or ask < bid or (ask - bid) / bid * 10000 > self.config.max_spread_bps:
            raise MarketUnavailable("market spread exceeds execution limit")
        return bid, ask

    def market_warning(self, market, error):
        detail = str(error)
        if self.market_warnings.get(market) != detail:
            self.store.event("market_deferred", instrument=market, detail=detail)
        self.market_warnings[market] = detail

    def step(self, stop_requested=lambda: False):
        if self.state["pending"]:
            self.finish_order()
        if self.client.get("/api/v5/trade/orders-pending"):
            raise RuntimeError("unexpected pending ordinary orders; reconcile before trading")
        for market in self.instruments:
            if stop_requested():
                return
            self.reconcile(market)

        account = account_snapshot(self.client)
        budget = min(
            self.state["capital_ceiling"], account["equity"] * self.config.capital_fraction
        )
        used_margin = 0.0
        blocked_markets = set()
        for market, instrument in self.instruments.items():
            if stop_requested():
                return
            position = self.state["markets"][market]["position"]
            if position:
                try:
                    _, ask = self.quote(market)
                except (MarketUnavailable, TransientRead) as exc:
                    self.market_warning(market, exc)
                    blocked_markets.add(market)
                    used_margin = max(used_margin, budget)
                    continue
                notional = float(dec(position["qty"]) * instrument.contract_value) * max(
                    ask, position["avg"]
                )
                used_margin += notional * (1 / self.config.leverage + 2 * self.fees[market])

        used_margin = self._process_entry_approvals(
            account, budget, used_margin, stop_requested
        )
        used_margin = self._process_manual_entry_request(
            account, budget, used_margin, stop_requested
        )
        self._process_candidate_scan(account, budget, used_margin, stop_requested)

        # New candles never create first-entry candidates automatically. The normal
        # loop below only manages positions that already exist.
        for market, instrument in self.instruments.items():
            if stop_requested():
                return
            item = self.state["markets"][market]
            position = item["position"]
            if position is None:
                continue
            try:
                frame = closed_candles(self.client, market, self.strategy, self.config.bar)
            except (MarketUnavailable, TransientRead) as exc:
                self.market_warning(market, exc)
                continue

            row = frame.iloc[-1]
            bar = row["timestamp"].isoformat()
            age = self.client.now() - row["timestamp"].timestamp() - BAR_SECONDS[self.config.bar]
            if age < 0 or age > BAR_SECONDS[self.config.bar] + 100:
                self.market_warning(market, "latest confirmed candle is stale or in the future")
                continue
            if market not in blocked_markets and self.market_warnings.pop(market, None):
                self.store.event("market_recovered", instrument=market)
            if item["last_bar"] is not None and pd.Timestamp(bar) <= pd.Timestamp(item["last_bar"]):
                continue
            if item["last_bar"]:
                missed = frame[frame["timestamp"] > pd.Timestamp(item["last_bar"])]
                if len(missed):
                    position["high"] = max(position["high"], float(missed["high"].max()))

            item["last_bar"] = bar
            self.save()
            self.trail(market, row)
            position = item["position"]
            if position is None:
                continue
            if age > self.config.max_signal_age_seconds:
                self.store.event("skip_stale_entry", instrument=market, age_seconds=round(age))
                continue

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
            if not signal:
                continue
            stop = float(rounded(position["stop"], instrument.tick))
            try:
                bid, ask = self.quote(market)
            except (MarketUnavailable, TransientRead) as exc:
                self.market_warning(market, exc)
                continue
            if bid <= stop:
                continue
            if (
                ask <= position["avg"]
                or ask < position["last_fill"] + self.strategy.add_step_atr * float(row["atr"])
            ):
                continue

            limit = rounded(
                ask * (1 + self.config.max_entry_slippage_bps / 10000),
                instrument.tick,
            )
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

            self.reconcile(market)
            if item["position"] is not position:
                continue
            if stop_requested():
                return
            stop_id = "ts" + uuid.uuid4().hex[:28]
            risk_budget = position["risk_budget"]
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
            cost = float(quantity * instrument.contract_value * limit) * (
                1 / self.config.leverage + 2 * self.fees[market]
            )
            used_margin += cost
            account["available_usdt"] = max(account["available_usdt"] - cost, 0)
