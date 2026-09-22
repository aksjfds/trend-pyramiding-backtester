from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import replace
from pathlib import Path

from .live import LiveConfig, StateStore, SwapRunner, account_snapshot, selected_config
from .okx import Credentials, OKXClient, OKXError, TransientRead, UncertainWrite, dec, universe
from .runtime import ProcessControl, safe_console_print
from .settings import load_settings


def check_account(client, config, store):
    account = account_snapshot(client, for_trading=False)
    warnings = list(account.get("warnings", []))
    instruments, positions, pending = [], None, None
    try:
        state = store.load()
        requested = config.instruments or (tuple(state["markets"]) if state else ())
        instruments = [i.inst_id for i in universe(client, config.top_n, requested)]
    except (ValueError, RuntimeError, OSError) as exc:
        warnings.append(f"候选币种读取失败：{exc}")
        state = None
    try:
        positions = sum(
            dec(p.get("pos") or "0") != 0 for p in client.get("/api/v5/account/positions")
        )
    except (ValueError, RuntimeError, OSError) as exc:
        warnings.append(f"持仓读取失败：{exc}")
    try:
        pending = len(client.get("/api/v5/trade/orders-pending"))
    except (ValueError, RuntimeError, OSError) as exc:
        warnings.append(f"挂单读取失败：{exc}")
    return {
        "environment": "demo" if config.demo else "live",
        "authenticated": True,
        "orders_enabled": False,
        "trade_permission": account["trade_permission"],
        "account_equity_usd": account["equity_usd"],
        "account_equity_usdt_equivalent": account["equity"],
        "available_usdt": account["available_usdt"],
        "capital_limit_usdt_approx": account["equity"] * config.capital_fraction
        if account["equity"] is not None
        else None,
        "open_positions": positions,
        "pending_orders": pending,
        "saved_state": state is not None,
        "unresolved_order": bool(state and state["pending"]),
        "halted": store.halt_path.exists(),
        "instruments": instruments,
        "warnings": warnings,
    }


def watch_account(client, config, store):
    """Read-only account monitor. No trading client is created."""
    with store.lock(), ProcessControl() as control:
        while not control.stop.is_set():
            try:
                client.sync_time()
                report = check_account(client, config, store)
                # Routine monitor logs do not include account balances or credentials.
                safe_console_print(
                    json.dumps(
                        {
                            "event": "read_only_check",
                            "environment": report["environment"],
                            "authenticated": True,
                            "trade_permission": report["trade_permission"],
                            "orders_enabled": False,
                            "halted": report["halted"],
                            "instruments": report["instruments"],
                        }
                    ),
                    flush=True,
                )
                control.update("ready")
            except Exception as exc:
                safe_console_print(
                    json.dumps(
                        {
                            "event": "read_only_check_failed",
                            "error_type": type(exc).__name__,
                            "detail": str(exc),
                        }
                    ),
                    flush=True,
                )
                control.update("error")
            control.stop.wait(300)


def run_worker(client, config, strategy, store, *, once=False):
    with ProcessControl() as control:
        if store.halt_path.exists():
            safe_console_print(
                "Trading is paused by a persistent halt marker. Inspect state, clear-halt, then restart.",
                flush=True,
            )
            while not control.stop.is_set():
                control.update("halted")
                control.stop.wait(10)
            return
        with store.lock():
            # Check again after acquiring the shared-volume lock.
            if store.halt_path.exists():
                raise RuntimeError("persistent halt marker prevents startup")
            # Re-read under the trading lock so a simultaneous settings save cannot be missed.
            config, strategy = load_settings(config, strategy, store)
            config = selected_config(config, store)
            runner = SwapRunner(client, config, strategy, store)
            try:
                runner.stop_requested = control.stop.is_set
                initialized, failures = False, 0
                next_sync = time.monotonic() + 300
                while not control.stop.is_set():
                    try:
                        if not initialized:
                            runner.initialize()
                            initialized = True
                        if control.stop.is_set():
                            break
                        if time.monotonic() >= next_sync:
                            client.sync_time()
                            next_sync = time.monotonic() + 300
                        runner.step(stop_requested=control.stop.is_set)
                    except TransientRead as exc:
                        state = getattr(runner, "state", None) or {}
                        unsafe = state.get("pending") or any(
                            m.get("position", {}).get("unsafe")
                            for m in state.get("markets", {}).values()
                            if m.get("position")
                        )
                        # Never replay initialization writes or bypass order/stop verification.
                        if unsafe or (not initialized and getattr(client, "write_attempts", 0)):
                            raise RuntimeError(
                                "read failed during order/setup verification; inspect before restart"
                            ) from exc
                        if once:
                            raise
                        failures += 1
                        delay = min(60, config.poll_seconds * 2 ** min(failures - 1, 3))
                        control.update("recovering")
                        if failures == 1 or failures % 10 == 0:
                            store.event("read_retry", code=exc.code, retry_seconds=delay)
                        control.stop.wait(delay)
                        continue
                    if failures:
                        store.event("connection_recovered")
                    failures = 0
                    control.update(
                        "degraded" if getattr(runner, "market_warnings", {}) else "ready"
                    )
                    if once:
                        break
                    control.stop.wait(config.poll_seconds)
                store.event(
                    "stopped", note="exchange stops remain active; positions are not liquidated"
                )
            except TransientRead:
                # A one-shot temporary read failure does not latch an operational halt.
                raise
            except Exception as exc:
                store.halt(exc)
                store.event("halted", error_type=type(exc).__name__, detail=str(exc))
                raise


def main():
    parser = argparse.ArgumentParser(prog="pyramid-okx", description="OKX long-only swap runner")
    parser.add_argument(
        "command", choices=["scan", "check", "watch", "status", "clear-halt", "run"]
    )
    parser.add_argument(
        "--config", type=Path, default=Path(os.environ.get("PYRAMID_CONFIG", "config/okx.toml"))
    )
    parser.add_argument("--state-dir", type=Path, default=os.environ.get("PYRAMID_STATE_DIR"))
    parser.add_argument(
        "--demo", action="store_true", help="use separate demo credentials and state"
    )
    parser.add_argument(
        "--live", action="store_true", help="explicitly enable real trading for run"
    )
    parser.add_argument("--once", action="store_true", help="run one trading iteration, then exit")
    args = parser.parse_args()
    try:
        config, strategy = LiveConfig.load(args.config)
        if args.demo:
            config = replace(config, demo=True)
        if args.live and (config.demo or args.command != "run"):
            raise ValueError("--live is only valid with the production run command")
        root = args.config.resolve().parent.parent
        environment = "demo" if config.demo else "live"
        store = StateStore((args.state_dir or root / "state") / f"okx-{environment}.json")
        config, strategy = load_settings(config, strategy, store)
        config = selected_config(config, store)
        if args.command == "status":
            state = store.load()
            if state is None:
                safe_console_print("No saved trading state; runner has not been initialized.")
                if store.halt_path.exists():
                    safe_console_print("Persistent halt marker exists; trading is paused.")
            else:
                safe_console_print(
                    json.dumps(
                        {
                            "environment": environment,
                            "capital_ceiling": state["capital_ceiling"],
                            "unresolved_order": state["pending"] is not None,
                            "halted": store.halt_path.exists(),
                            "markets": {
                                key: {
                                    "last_bar": item["last_bar"],
                                    "contracts": item["position"]["qty"]
                                    if item["position"]
                                    else "0",
                                    "stop": item["position"]["stop"] if item["position"] else None,
                                }
                                for key, item in state["markets"].items()
                            },
                        },
                        indent=2,
                    )
                )
            return
        if args.command == "clear-halt":
            store.clear_halt()
            safe_console_print("Halt marker cleared. Trading state is preserved; restart the worker manually.")
            return
        if args.command == "run" and not config.demo and not args.live:
            raise ValueError("real orders require run --live; check is read-only")
        credentials = None
        if args.command != "scan":
            credentials = Credentials.load(config.demo)
        client = OKXClient(
            base_url=config.base_url,
            demo=config.demo,
            credentials=credentials,
            write_enabled=args.command == "run",
        )
        if args.command == "watch":
            watch_account(client, config, store)
            return
        if args.command != "run":
            client.sync_time()
        if args.command == "scan":
            instruments = universe(client, config.top_n, config.instruments)
            safe_console_print(
                json.dumps(
                    {
                        "environment": environment,
                        "bar": config.bar,
                        "selection": "24h base volume × latest price (approximate USDT turnover)",
                        "instruments": [i.inst_id for i in instruments],
                        "leverage": config.leverage,
                        "capital_fraction": config.capital_fraction,
                        "orders_enabled": False,
                    },
                    indent=2,
                )
            )
            return
        if args.command == "check":
            safe_console_print(json.dumps(check_account(client, config, store), indent=2))
            return
        run_worker(client, config, strategy, store, once=args.once)
    except (ValueError, RuntimeError, OKXError, TransientRead, UncertainWrite, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
