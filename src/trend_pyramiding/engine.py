from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .indicators import atr, prior_rolling_high, rolling_structure_low
from .metrics import summarize
from .signals import resolve_entry_signal, strong_close_signal


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 100_000.0
    fee_bps: float = 5.0
    slippage_bps: float = 3.0
    risk_per_trade: float = 0.01
    max_position_pct: float = 1.0

    atr_period: int = 14
    atr_stop_mult: float = 2.0
    structure_lookback: int = 8
    structure_buffer_atr: float = 0.10

    ema_period: int = 20
    entry_breakout_lookback: int = 20
    add_breakout_lookback: int = 10
    add_step_atr: float = 0.75
    require_add_breakout: bool = False

    trail_atr_mult: float = 2.25
    trail_activation_r: float = 1.0
    break_even_r: float = 1.0

    risk_weights: tuple[float, ...] = (0.30, 0.30, 0.20, 0.20)
    allocation_weights: tuple[float, ...] = (0.30, 0.30, 0.20, 0.20)

    def validate(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be > 0")
        if not 0 < self.risk_per_trade <= 0.10:
            raise ValueError("risk_per_trade must be in (0, 0.10]")
        if not 0 < self.max_position_pct <= 1.0:
            raise ValueError("max_position_pct must be in (0, 1]")
        if len(self.risk_weights) != len(self.allocation_weights):
            raise ValueError("risk_weights and allocation_weights must have equal length")
        if not self.risk_weights:
            raise ValueError("at least one tranche is required")
        if any(x <= 0 for x in self.risk_weights + self.allocation_weights):
            raise ValueError("all tranche weights must be > 0")
        if sum(self.risk_weights) > 1.000001:
            raise ValueError("risk_weights must sum to <= 1")
        if sum(self.allocation_weights) > 1.000001:
            raise ValueError("allocation_weights must sum to <= 1")


@dataclass
class PendingOrder:
    kind: str
    stop: float
    signal_time: pd.Timestamp


@dataclass
class Position:
    trade_id: int
    entry_time: pd.Timestamp
    first_entry: float
    avg_entry: float
    qty: float
    stop: float
    initial_stop: float
    initial_risk_per_unit: float
    risk_budget: float
    entry_equity: float
    tranches: int = 1
    last_add_reference: float = 0.0
    high_watermark: float = 0.0
    entry_fees: float = 0.0
    entry_notional: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)


def _load_frame(data: pd.DataFrame | str | Path) -> pd.DataFrame:
    if isinstance(data, (str, Path)):
        frame = pd.read_csv(data)
    else:
        frame = data.copy()

    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"missing columns: {', '.join(missing)}")

    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    frame = frame.reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        frame[col] = pd.to_numeric(frame[col], errors="raise")
    if (frame[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("OHLC prices must be > 0")
    if (frame["high"] < frame[["open", "close", "low"]].max(axis=1)).any():
        raise ValueError("high is inconsistent with OHLC")
    if (frame["low"] > frame[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError("low is inconsistent with OHLC")
    return frame


def _buy_fill(raw_price: float, slippage_bps: float) -> float:
    return raw_price * (1.0 + slippage_bps / 10_000.0)


def _sell_fill(raw_price: float, slippage_bps: float) -> float:
    return raw_price * (1.0 - slippage_bps / 10_000.0)


def _initial_stop(row: pd.Series, cfg: BacktestConfig) -> float | None:
    if not np.isfinite(row["atr"]) or not np.isfinite(row["structure_low"]):
        return None
    structure_stop = row["structure_low"] - cfg.structure_buffer_atr * row["atr"]
    atr_stop = row["close"] - cfg.atr_stop_mult * row["atr"]
    stop = max(float(structure_stop), float(atr_stop))
    if not np.isfinite(stop) or stop <= 0 or stop >= row["close"]:
        return None
    return stop


def _open_risk(position: Position) -> float:
    return max((position.avg_entry - position.stop) * position.qty, 0.0)


def _size_tranche(
    *,
    fill_price: float,
    stop: float,
    cash: float,
    position: Position | None,
    tranche_index: int,
    cfg: BacktestConfig,
    trade_equity: float,
    risk_budget: float,
    recycle_risk: bool = False,
) -> float:
    risk_per_unit = fill_price - stop
    if risk_per_unit <= 0:
        return 0.0

    existing_risk = _open_risk(position) if position else 0.0
    remaining_risk = max(risk_budget - existing_risk, 0.0)
    tranche_risk_cap = risk_budget * cfg.risk_weights[tranche_index]
    risk_cap = min(remaining_risk, tranche_risk_cap)
    if recycle_risk:
        # Reuse released risk within the cumulative tranche schedule, while bounding
        # each new tranche by the largest original tranche allowance.
        cumulative_cap = risk_budget * sum(cfg.risk_weights[: tranche_index + 1])
        risk_cap = min(
            remaining_risk,
            max(cumulative_cap - existing_risk, 0.0),
            risk_budget * max(cfg.risk_weights),
        )
    qty_by_risk = risk_cap / risk_per_unit

    max_notional = trade_equity * cfg.max_position_pct
    current_notional = (position.qty * fill_price) if position else 0.0
    remaining_notional = max(max_notional - current_notional, 0.0)
    tranche_notional_cap = max_notional * cfg.allocation_weights[tranche_index]
    notional_cap = min(remaining_notional, tranche_notional_cap)
    qty_by_notional = notional_cap / fill_price

    fee_rate = cfg.fee_bps / 10_000.0
    qty_by_cash = cash / (fill_price * (1.0 + fee_rate))
    qty = min(qty_by_risk, qty_by_notional, qty_by_cash)
    return max(float(qty), 0.0)


def run_backtest(
    data: pd.DataFrame | str | Path,
    cfg: BacktestConfig | None = None,
    *,
    signal_column: str | None = None,
    strategy: str = "classic",
) -> BacktestResult:
    if strategy not in {"classic", "confirmed-pyramid"}:
        raise ValueError("strategy must be classic or confirmed-pyramid")
    confirmed = strategy == "confirmed-pyramid"
    cfg = cfg or BacktestConfig()
    cfg.validate()
    frame = _load_frame(data)

    frame["atr"] = atr(frame, cfg.atr_period)
    frame["structure_low"] = rolling_structure_low(frame["low"], cfg.structure_lookback)
    frame["add_prior_high"] = prior_rolling_high(frame["high"], cfg.add_breakout_lookback)
    frame["entry_signal"] = resolve_entry_signal(
        frame,
        signal_column,
        cfg.ema_period,
        cfg.entry_breakout_lookback,
    )

    if confirmed:
        frame["entry_signal"] &= strong_close_signal(frame)

    cash = cfg.initial_cash
    position: Position | None = None
    pending: PendingOrder | None = None
    next_trade_id = 1
    fee_rate = cfg.fee_bps / 10_000.0
    events: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []

    def mark_equity(close: float) -> float:
        return cash + (position.qty * close if position else 0.0)

    def record_event(kind: str, ts: pd.Timestamp, price: float, qty: float) -> None:
        if position is None:
            return
        events.append(
            {
                "trade_id": position.trade_id,
                "timestamp": ts,
                "event": kind,
                "price": float(price),
                "qty": float(qty),
                "position_qty": float(position.qty),
                "avg_entry": float(position.avg_entry),
                "stop": float(position.stop),
                "open_risk_to_stop": float(_open_risk(position)),
                "risk_budget": float(position.risk_budget),
                "tranches": int(position.tranches),
            }
        )

    def exit_position(ts: pd.Timestamp, raw_price: float, reason: str) -> None:
        nonlocal cash, position, pending
        if position is None:
            return
        sell_price = _sell_fill(float(raw_price), cfg.slippage_bps)
        exit_notional = position.qty * sell_price
        exit_fee = exit_notional * fee_rate
        cash += exit_notional - exit_fee
        net_pnl = position.qty * (sell_price - position.avg_entry) - position.entry_fees - exit_fee
        trades.append(
            {
                "trade_id": position.trade_id,
                "entry_time": position.entry_time,
                "exit_time": ts,
                "first_entry": position.first_entry,
                "avg_entry": position.avg_entry,
                "exit_price": sell_price,
                "qty": position.qty,
                "tranches": position.tranches,
                "initial_stop": position.initial_stop,
                "net_pnl": net_pnl,
                "r_multiple": net_pnl / position.risk_budget if position.risk_budget else 0.0,
                "exit_reason": reason,
            }
        )
        record_event("exit", ts, sell_price, position.qty)
        position = None
        pending = None

    for i, row in frame.iterrows():
        ts = row["timestamp"]
        o = float(row["open"])
        h = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])

        # Existing stop is active from the start of the bar. Gap exits take priority over adds.
        if position is not None and o <= position.stop:
            exit_position(ts, o, "gap_stop")

        # Orders are generated only after a completed prior bar and fill at this bar's open.
        if pending is not None:
            order = pending
            pending = None
            if order.kind == "entry" and position is None:
                fill = _buy_fill(o, cfg.slippage_bps)
                if fill > order.stop:
                    trade_equity = cash
                    risk_budget = trade_equity * cfg.risk_per_trade
                    qty = _size_tranche(
                        fill_price=fill,
                        stop=order.stop,
                        cash=cash,
                        position=None,
                        tranche_index=0,
                        cfg=cfg,
                        trade_equity=trade_equity,
                        risk_budget=risk_budget,
                        recycle_risk=confirmed,
                    )
                    if qty > 0:
                        notional = qty * fill
                        fee = notional * fee_rate
                        cash -= notional + fee
                        position = Position(
                            trade_id=next_trade_id,
                            entry_time=ts,
                            first_entry=fill,
                            avg_entry=fill,
                            qty=qty,
                            stop=order.stop,
                            initial_stop=order.stop,
                            initial_risk_per_unit=fill - order.stop,
                            risk_budget=risk_budget,
                            entry_equity=trade_equity,
                            last_add_reference=fill,
                            high_watermark=fill,
                            entry_fees=fee,
                            entry_notional=notional,
                        )
                        next_trade_id += 1
                        record_event("entry", ts, fill, qty)
            elif order.kind == "add" and position is not None:
                tranche_index = position.tranches
                if tranche_index < len(cfg.risk_weights):
                    fill = _buy_fill(o, cfg.slippage_bps)
                    if fill > position.avg_entry and fill > position.stop:
                        qty = _size_tranche(
                            fill_price=fill,
                            stop=position.stop,
                            cash=cash,
                            position=position,
                            tranche_index=tranche_index,
                            cfg=cfg,
                            trade_equity=position.entry_equity,
                            risk_budget=position.risk_budget,
                            recycle_risk=confirmed,
                        )
                        if qty > 0:
                            notional = qty * fill
                            fee = notional * fee_rate
                            old_qty = position.qty
                            position.avg_entry = (
                                position.avg_entry * old_qty + fill * qty
                            ) / (old_qty + qty)
                            position.qty += qty
                            position.entry_fees += fee
                            position.entry_notional += notional
                            position.tranches += 1
                            position.last_add_reference = fill
                            cash -= notional + fee
                            record_event("add", ts, fill, qty)

        # Same-bar stop is honored after an open fill.
        if position is not None and low <= position.stop:
            exit_position(ts, position.stop, "stop")

        if position is not None:
            position.high_watermark = max(position.high_watermark, h)
            current_r = position.initial_risk_per_unit
            stop_candidate = position.stop

            if current_r > 0 and (
                position.high_watermark >= position.first_entry + cfg.break_even_r * current_r
            ):
                stop_candidate = max(stop_candidate, position.first_entry)

            if current_r > 0 and (
                position.high_watermark >= position.first_entry + cfg.trail_activation_r * current_r
            ) and np.isfinite(row["atr"]) and np.isfinite(row["structure_low"]):
                structure_stop = float(row["structure_low"] - cfg.structure_buffer_atr * row["atr"])
                atr_trail = float(close - cfg.trail_atr_mult * row["atr"])
                stop_candidate = max(stop_candidate, structure_stop, atr_trail)

            # A stop only tightens. If an end-of-bar calculation lands above the close,
            # keep it infinitesimally below close so the next bar's gap logic is explicit.
            old_stop = position.stop
            position.stop = max(position.stop, min(stop_candidate, close * (1.0 - 1e-9)))
            if position.stop > old_stop + 1e-12:
                record_event("stop_update", ts, close, 0.0)

            breakout_ok = (
                not cfg.require_add_breakout
                or (np.isfinite(row["add_prior_high"]) and close > row["add_prior_high"])
            )
            if (
                position.tranches < len(cfg.risk_weights)
                and np.isfinite(row["atr"])
                and close > position.avg_entry
                and (not confirmed or position.stop >= position.avg_entry)
                and close >= position.last_add_reference + cfg.add_step_atr * row["atr"]
                and breakout_ok
            ):
                pending = PendingOrder(kind="add", stop=position.stop, signal_time=ts)

        elif bool(row["entry_signal"]):
            stop = _initial_stop(row, cfg)
            if stop is not None:
                pending = PendingOrder(kind="entry", stop=stop, signal_time=ts)

        equity_rows.append(
            {
                "timestamp": ts,
                "equity": mark_equity(close),
                "cash": cash,
                "position_qty": position.qty if position else 0.0,
                "stop": position.stop if position else np.nan,
            }
        )

    if position is not None:
        last = frame.iloc[-1]
        exit_position(last["timestamp"], float(last["close"]), "end_of_data")
        equity_rows[-1]["equity"] = cash
        equity_rows[-1]["cash"] = cash
        equity_rows[-1]["position_qty"] = 0.0
        equity_rows[-1]["stop"] = np.nan

    equity_curve = pd.DataFrame(equity_rows)
    trades_df = pd.DataFrame(trades)
    events_df = pd.DataFrame(events)
    summary = summarize(equity_curve, trades_df, cfg.initial_cash)
    return BacktestResult(summary, equity_curve, trades_df, events_df)


@dataclass
class BacktestResult:
    summary: dict[str, float | int]
    equity_curve: pd.DataFrame
    trades: pd.DataFrame
    events: pd.DataFrame
