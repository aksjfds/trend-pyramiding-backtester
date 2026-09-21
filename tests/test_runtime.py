import json
import signal
import threading

import pytest

from trend_pyramiding import okx_cli, runtime
from trend_pyramiding.live import LiveConfig, StateStore
from trend_pyramiding.okx import UncertainWrite


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("PYRAMID_HEARTBEAT_FILE", str(tmp_path / "heartbeat.json"))


def test_sigterm_requests_stop_without_interrupting_work_and_restores_handler():
    previous = signal.getsignal(signal.SIGTERM)
    with runtime.ProcessControl() as control:
        signal.raise_signal(signal.SIGTERM)
        assert control.stop.is_set()
        control.update("ready")  # The in-flight operation can finish normally.
        assert json.loads(runtime.heartbeat_path().read_text())["phase"] == "ready"
    assert signal.getsignal(signal.SIGTERM) == previous
    assert json.loads(runtime.heartbeat_path().read_text())["phase"] == "stopped"


def test_failed_worker_latches_halt_without_erasing_uncertain_order(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state/okx-live.json")
    original = {"version": 1, "pending": {"client_id": "test-pending"}}
    store.save(original)

    class FailedRunner:
        def __init__(self, *args):
            pass

        def initialize(self):
            raise UncertainWrite("order needs reconciliation")

    monkeypatch.setattr(okx_cli, "SwapRunner", FailedRunner)
    with pytest.raises(UncertainWrite):
        okx_cli.run_worker(None, LiveConfig(), None, store)
    assert json.loads(store.halt_path.read_text())["error_type"] == "UncertainWrite"
    assert store.load() == original
    store.clear_halt()
    assert not store.halt_path.exists()
    assert store.load() == original


def test_restart_with_halt_never_initializes_trader(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state/okx-live.json")
    store.halt(RuntimeError("operator review required"))

    class StopOnWait(threading.Event):
        def wait(self, timeout=None):
            self.set()
            return True

    control = runtime.ProcessControl()
    control.stop = StopOnWait()
    monkeypatch.setattr(okx_cli, "ProcessControl", lambda: control)
    monkeypatch.setattr(
        okx_cli, "SwapRunner", lambda *args: pytest.fail("halted worker must not initialize")
    )
    okx_cli.run_worker(None, LiveConfig(), None, store)
    assert store.halt_path.exists()


def test_once_worker_exits_after_one_cycle(tmp_path, monkeypatch):
    calls = []

    class Runner:
        def __init__(self, *args):
            pass

        def initialize(self):
            calls.append("initialize")

        def step(self, *, stop_requested):
            assert not stop_requested()
            calls.append("step")

    monkeypatch.setattr(okx_cli, "SwapRunner", Runner)
    store = StateStore(tmp_path / "state/okx-live.json")
    okx_cli.run_worker(None, LiveConfig(), None, store, once=True)
    assert calls == ["initialize", "step"]
    assert not store.halt_path.exists()


def test_watch_uses_only_read_only_checks_and_no_balance_logs(tmp_path, monkeypatch, capsys):
    class StopOnWait(threading.Event):
        def wait(self, timeout=None):
            self.set()
            return True

    class Client:
        def sync_time(self):
            pass

    control = runtime.ProcessControl()
    control.stop = StopOnWait()
    monkeypatch.setattr(okx_cli, "ProcessControl", lambda: control)
    monkeypatch.setattr(
        okx_cli, "SwapRunner", lambda *args: pytest.fail("read-only watch cannot trade")
    )
    monkeypatch.setattr(
        okx_cli,
        "check_account",
        lambda *args: {
            "environment": "live",
            "trade_permission": True,
            "halted": False,
            "instruments": ["BTC-USDT-SWAP"],
            "account_equity_usd": 123456.0,
        },
    )
    okx_cli.watch_account(Client(), LiveConfig(), StateStore(tmp_path / "state.json"))
    output = capsys.readouterr().out
    assert '"orders_enabled": false' in output
    assert "123456" not in output
