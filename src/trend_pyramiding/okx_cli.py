from __future__ import annotations

import argparse
import getpass
import json
import os
from dataclasses import replace
from pathlib import Path

from .live import LiveConfig, StateStore, SwapRunner, account_snapshot
from .okx import Credentials, OKXClient, OKXError, UncertainWrite, dec, universe
from .runtime import ProcessControl, credential_config, require_persistent_state


def save_credentials(path: Path, demo: bool):
    if path.exists():
        raise ValueError(f"credentials already exist at {path}; edit them locally if needed")
    values = {
        key: getpass.getpass(label).strip()
        for key, label in (
            ("key", "OKX API Key (hidden): "),
            ("secret", "OKX Secret (hidden): "),
            ("passphrase", "OKX Passphrase (hidden): "),
        )
    }
    if not all(values.values()):
        raise ValueError("all credential fields are required")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"demo": demo, **values}, handle)
    print(f"Credentials saved privately to {path}; values are never printed.")


def check_account(client, config, store):
    account = account_snapshot(client)
    state = store.load()
    requested = tuple(state["markets"]) if state else config.instruments
    instruments = universe(client, config.top_n, requested)
    positions = [
        p for p in client.get("/api/v5/account/positions") if dec(p.get("pos") or "0") != 0
    ]
    pending = client.get("/api/v5/trade/orders-pending")
    return {
        "environment": "demo" if config.demo else "live",
        "authenticated": True,
        "orders_enabled": False,
        "trade_permission": account["trade_permission"],
        "account_equity_usd": account["equity_usd"],
        "account_equity_usdt_equivalent": account["equity"],
        "available_usdt": account["available_usdt"],
        "capital_limit_usdt_approx": account["equity"] * config.capital_fraction,
        "open_positions": len(positions),
        "pending_orders": len(pending),
        "saved_state": state is not None,
        "unresolved_order": bool(state and state["pending"]),
        "halted": store.halt_path.exists(),
        "instruments": [i.inst_id for i in instruments],
    }


def watch_account(client, config, store):
    """Read-only, long-running deployment smoke check. No trading client is created."""
    with ProcessControl() as control:
        while not control.stop.is_set():
            try:
                client.sync_time()
                report = check_account(client, config, store)
                # Routine hosted logs do not include account balances or credentials.
                print(
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
                print(
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
            print(
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
            runner = SwapRunner(client, config, strategy, store)
            try:
                runner.initialize()
                while not control.stop.is_set():
                    runner.step(stop_requested=control.stop.is_set)
                    control.update("ready")
                    if once:
                        break
                    control.stop.wait(config.poll_seconds)
                store.event(
                    "stopped", note="exchange stops remain active; positions are not liquidated"
                )
            except Exception as exc:
                store.halt(exc)
                store.event("halted", error_type=type(exc).__name__, detail=str(exc))
                raise


def main():
    parser = argparse.ArgumentParser(prog="pyramid-okx", description="OKX long-only swap runner")
    parser.add_argument(
        "command", choices=["scan", "check", "watch", "credentials", "status", "clear-halt", "run"]
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
        credentials_path = root / "secrets" / f"okx-{environment}.json"
        store = StateStore((args.state_dir or root / "state") / f"okx-{environment}.json")
        if args.command == "credentials":
            save_credentials(credentials_path, config.demo)
            return
        if args.command == "status":
            state = store.load()
            if state is None:
                print("No saved trading state; runner has not been initialized.")
                if store.halt_path.exists():
                    print("Persistent halt marker exists; trading is paused.")
            else:
                print(
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
            print("Halt marker cleared. Trading state is preserved; restart the worker manually.")
            return
        if args.command == "run" and not config.demo and not args.live:
            raise ValueError("real orders require run --live; check is read-only")
        if args.command == "run":
            require_persistent_state(store.path.parent)
        credentials = None
        if args.command != "scan":
            with credential_config(args.config.resolve().parent / "okx.credentials.toml") as path:
                credentials = Credentials.load(credentials_path, config.demo, path)
        client = OKXClient(
            base_url=config.base_url,
            demo=config.demo,
            credentials=credentials,
            write_enabled=args.command == "run",
        )
        if args.command == "watch":
            watch_account(client, config, store)
            return
        client.sync_time()
        if args.command == "scan":
            instruments = universe(client, config.top_n, config.instruments)
            print(
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
            print(json.dumps(check_account(client, config, store), indent=2))
            return
        run_worker(client, config, strategy, store, once=args.once)
    except (ValueError, RuntimeError, OKXError, UncertainWrite, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
