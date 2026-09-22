"""Validated, per-account settings on the same persistent disk as trading state."""

from dataclasses import asdict, replace

from .engine import BacktestConfig
from .live import BAR_SECONDS, LiveConfig, StateStore, config_fingerprint, validate_strategy
from .okx import dec

LIVE_FIELDS = {"capital_fraction", "leverage", "bar"}
STRATEGY_FIELDS = {
    "risk_per_trade",
    "max_position_pct",
    "atr_period",
    "atr_stop_mult",
    "structure_lookback",
    "structure_buffer_atr",
    "ema_period",
    "entry_breakout_lookback",
    "add_breakout_lookback",
    "add_step_atr",
    "require_add_breakout",
    "trail_atr_mult",
    "trail_activation_r",
    "break_even_r",
    "risk_weights",
    "allocation_weights",
}


def settings_store(store):
    return StateStore(store.path.with_suffix(".settings.json"))


def apply_settings(config, strategy, payload):
    if not isinstance(payload, dict) or set(payload) != {"live", "strategy"}:
        raise ValueError("参数格式无效")
    for name, allowed, original in (
        ("live", LIVE_FIELDS, LiveConfig()),
        ("strategy", STRATEGY_FIELDS, BacktestConfig()),
    ):
        values = payload[name]
        if not isinstance(values, dict) or set(values) - allowed:
            raise ValueError("存在不支持的参数")
        for key, value in values.items():
            default = getattr(original, key)
            if isinstance(default, bool):
                if type(value) is not bool:
                    raise ValueError(f"{key} 必须为开关值")
            elif isinstance(default, int):
                if type(value) is not int:
                    raise ValueError(f"{key} 必须为整数")
            elif isinstance(default, (tuple, list)):
                if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 10:
                    raise ValueError(f"{key} 需要 1–10 个分档比例")
                for weight in value:
                    if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                        raise ValueError(f"{key} 比例必须是数字")
                    dec(weight)
            elif isinstance(default, str):
                if not isinstance(value, str):
                    raise ValueError(f"{key} 必须为文本选项")
            else:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"{key} 必须为数字")
                dec(value)
    live = replace(config, **payload["live"])
    values = dict(payload["strategy"])
    for key in ("risk_weights", "allocation_weights"):
        if key in values:
            values[key] = tuple(values[key])
    strategy = replace(strategy, **values)
    live.validate()
    validate_strategy(strategy)
    return live, strategy


def load_settings(config, strategy, store):
    saved = settings_store(store).load()
    if not saved:
        return config, strategy
    values = {
        "live": dict(saved["values"].get("live", {})),
        "strategy": dict(saved["values"].get("strategy", {})),
    }
    # Backward compatibility only: top_n used to control the removed preselected
    # trading universe and is ignored from now on.
    values["live"].pop("top_n", None)
    return apply_settings(config, strategy, values)


def settings_values(config, strategy):
    return {
        "live": {key: getattr(config, key) for key in sorted(LIVE_FIELDS)},
        "strategy": {key: getattr(strategy, key) for key in sorted(STRATEGY_FIELDS)},
    }


def migration_source(store, state, uid, config):
    saved = settings_store(store).load()
    if not saved:
        return None  # Editing TOML alone never bypasses the active-state guard.
    origin = saved.get("migration")
    if not origin or origin.get("fingerprint") != state["fingerprint"]:
        return None
    live = LiveConfig(**origin["configuration"]["live"])
    strategy = BacktestConfig(**origin["configuration"]["strategy"])
    if live.base_url != config.base_url or live.demo != config.demo:
        return None
    if config_fingerprint(live, strategy, uid) != state["fingerprint"]:
        return None
    return live


def save_settings(config, strategy, store, payload):
    live, updated = apply_settings(config, strategy, payload)
    state = store.load()
    if store.halt_path.exists() or (
        state
        and (state.get("pending") or any(m.get("position") for m in state["markets"].values()))
    ):
        raise ValueError("仍有持仓、待核对订单或暂停记录，暂时不能修改参数")
    previous = settings_store(store).load() or {}
    migration = None
    if state:
        migration = previous.get("migration")
        if not migration or migration.get("fingerprint") != state["fingerprint"]:
            migration = {
                "fingerprint": state["fingerprint"],
                "configuration": state.get("configuration")
                or {
                    "live": asdict(config),
                    "strategy": asdict(strategy),
                },
            }
    settings_store(store).save(
        {
            "version": 1,
            "values": settings_values(live, updated),
            "migration": migration,
        }
    )
    return live, updated


def schema():
    """Return only the small set of settings editable from the overview cards."""
    return [
        {
            "section": "live",
            "key": "capital_fraction",
            "label": "资金使用上限（%）",
            "type": "number",
            "min": 0.01,
            "max": 100,
            "step": 0.01,
            "scale": 100,
        },
        {
            "section": "live",
            "key": "leverage",
            "label": "逐仓杠杆（倍）",
            "type": "number",
            "min": 1,
            "max": 125,
            "step": 1,
            "scale": 1,
        },
        {
            "section": "live",
            "key": "bar",
            "label": "K 线周期",
            "type": "select",
            "choices": list(BAR_SECONDS),
        },
    ]

