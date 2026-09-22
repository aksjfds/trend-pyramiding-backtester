import http.client
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from trend_pyramiding import web
from trend_pyramiding.live import StateStore


@pytest.fixture
def controller(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    source = Path(__file__).resolve().parents[1] / "config"
    for name in ("okx.toml", "default.toml"):
        shutil.copy2(source / name, config / name)
    for prefix in ("OKX_", "OKX_DEMO_"):
        for suffix in ("API_KEY", "API_SECRET", "API_PASSPHRASE"):
            monkeypatch.setenv(prefix + suffix, prefix + suffix + "-private-test-value")
    instance = web.Controller(config / "okx.toml", tmp_path / "state")
    yield instance
    instance.close()


def test_page_does_not_start_trading_and_never_exposes_keys(controller):
    state = controller.snapshot()
    assert not state["running"]
    assert state["mode"] is None
    assert state["credentials"] == {"live": True, "demo": True}
    assert "private-test-value" not in json.dumps(state)
    assert controller.process is None


def test_explicit_live_start_and_separate_demo_commands(controller, monkeypatch):
    monkeypatch.setattr(web.subprocess, "Popen", lambda *a, **kw: pytest.fail("must not spawn"))
    with pytest.raises(ValueError, match="明确"):
        controller.start("live")
    assert "--live" in controller.command("live")
    assert "--demo" not in controller.command("live")
    assert "--demo" in controller.command("demo")
    assert "--live" not in controller.command("demo")
    assert "watch" in controller.command("demo-watch")
    assert "--live" not in controller.command("live", check=True)
    with pytest.raises(ValueError, match="有效"):
        controller.start(["live"])


def test_missing_keys_and_halt_block_start(controller, monkeypatch):
    monkeypatch.setattr(web.subprocess, "Popen", lambda *a, **kw: pytest.fail("must not spawn"))
    monkeypatch.delenv("OKX_API_SECRET")
    with pytest.raises(ValueError, match="OKX_API_SECRET"):
        controller.start("live", True)
    controller.store(True).halt(RuntimeError("test pause"))
    with pytest.raises(ValueError, match="异常暂停"):
        controller.start("demo")


def test_external_runner_blocks_duplicate_start(controller, monkeypatch):
    monkeypatch.setattr(web.subprocess, "Popen", lambda *a, **kw: pytest.fail("must not spawn"))
    with controller.store(False).lock():
        assert controller.snapshot()["external_process"] == ["live"]
        with pytest.raises(ValueError, match="其他策略进程"):
            controller.start("demo")


def test_snapshot_reports_saved_positions_and_uncertain_order(controller):
    controller.store(False).save(
        {
            "version": 1,
            "capital_ceiling": 200,
            "pending": {"client_id": "pending"},
            "markets": {
                "BTC-USDT-SWAP": {
                    "last_bar": "2026-09-20T01:00:00Z",
                    "position": {"qty": "2", "avg": 100, "stop": 90, "legs": [{}, {}]},
                }
            },
        }
    )
    result = controller.snapshot()
    assert result["pending"] is True
    assert result["markets"][0]["contracts"] == "2"
    assert result["markets"][0]["stop"] == 90
    assert result["markets"][0]["legs"] == 2
    assert result["capital_ceiling"] == 200
    assert controller.snapshot("demo")["markets"] == []


def test_account_check_is_read_only_and_preserves_timestamp_on_error(controller, monkeypatch):
    from types import SimpleNamespace

    report = {
        "authenticated": True,
        "account_equity_usdt_equivalent": 100,
        "available_usdt": 90,
        "capital_limit_usdt_approx": 20,
        "open_positions": 0,
        "pending_orders": 0,
        "instruments": ["BTC-USDT-SWAP"],
        "trade_permission": True,
        "warnings": [],
    }

    def run(command, **kwargs):
        assert "check" in command and "run" not in command and "--live" not in command
        return SimpleNamespace(returncode=0, stdout=json.dumps(report), stderr="")

    monkeypatch.setattr(web.subprocess, "run", run)
    controller.check("live")
    previous = controller.snapshot()["account"]
    assert previous["data"] == report
    monkeypatch.setattr(
        web.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=1, stderr="Error: " + os.environ["OKX_API_SECRET"]
        ),
    )
    with pytest.raises(ValueError):
        controller.check("watch")
    result = controller.snapshot()["account"]
    assert result["time"] == previous["time"] and result["data"] == report
    assert "已隐藏" in result["error"]
    assert "private-test-value" not in result["error"]
    assert not controller.checking


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail("test process did not reach expected state")


def test_real_process_stop_is_graceful_blocks_restart_and_allows_later_restart(
    controller, monkeypatch
):
    # A local stub with no network or exchange operations; finish an in-flight operation on SIGTERM.
    script = """
import signal, time, os
stopping = False
def stop(*args):
    global stopping
    stopping = True
signal.signal(signal.SIGTERM, stop)
print("ready " + os.environ["OKX_API_SECRET"], flush=True)
while not stopping:
    time.sleep(.02)
time.sleep(.2)
print("finished protective work", flush=True)
"""
    monkeypatch.setattr(
        controller, "command", lambda *a, **kw: [sys.executable, "-u", "-c", script]
    )
    controller.start("watch")
    wait_until(lambda: bool(controller.snapshot()["logs"]))
    assert "private-test-value" not in json.dumps(controller.snapshot())
    with pytest.raises(ValueError, match="已在运行"):
        controller.start("demo")
    controller.stop()
    assert controller.snapshot()["stopping"]
    with pytest.raises(ValueError, match="已在运行"):
        controller.start("watch")
    wait_until(lambda: not controller.snapshot()["running"])
    wait_until(
        lambda: any("finished protective work" in e["text"] for e in controller.snapshot()["logs"])
    )
    assert controller.snapshot()["exit_code"] == 0
    controller.start("watch")
    wait_until(lambda: bool(controller.snapshot()["logs"]))
    controller.close()
    assert not controller.snapshot()["running"]
    with pytest.raises(ValueError, match="关闭"):
        controller.start("watch")


@pytest.fixture
def server(controller):
    with web.PanelServer(0, controller) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        yield server
        server.shutdown()
        thread.join()


def request(server, method, path, body=None, **headers):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    headers.setdefault("Host", f"127.0.0.1:{server.server_port}")
    if body is not None:
        headers.setdefault("Content-Type", "application/json")
        body = json.dumps(body)
    connection.request(method, path, body, headers)
    response = connection.getresponse()
    content = response.read().decode()
    result = response.status, dict(response.getheaders()), content
    connection.close()
    return result


def test_http_page_assets_status_and_no_secret_exposure(server):
    status, headers, page = request(server, "GET", "/")
    assert status == 200 and "策略控制台" in page
    assert "资金与策略设置" not in page
    assert 'id="edit-capital"' in page
    assert 'id="edit-strategy"' in page
    assert page.count(">修改</button>") == 2
    assert "高级策略参数" not in page
    assert 'id="advanced-settings"' not in page
    assert 'id="settings-form"' not in page
    assert "符合策略的币种" in page
    assert "24h USDT 成交量" in page
    assert "信号收盘价" not in page
    assert "预计张数" not in page
    assert "剩余确认时间" not in page
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Frame-Options"] == "DENY"
    assert server.token in page
    for asset in ("/panel.js", "/panel.css"):
        assert request(server, "GET", asset)[0] == 200
    status, _, body = request(server, "GET", "/api/status", **{"X-Panel-Token": server.token})
    assert status == 200 and json.loads(body)["running"] is False
    assert "private-test-value" not in body
    assert request(server, "GET", "/../../config/okx.toml")[0] == 404


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Panel-Token": "wrong"},
        {"Origin": "https://evil.invalid"},
        {"Host": "evil.invalid"},
    ],
)
def test_http_rejects_cross_origin_and_missing_session(server, headers):
    if "Origin" in headers or "Host" in headers:
        headers["X-Panel-Token"] = server.token
    assert (
        request(server, "POST", "/api/start", {"mode": "live", "confirm_live": True}, **headers)[0]
        == 403
    )
    assert server.controller.process is None


def test_http_live_requires_explicit_action_and_stop_is_idempotent(server):
    headers = {"X-Panel-Token": server.token, "Origin": server.origin}
    status, _, body = request(server, "POST", "/api/start", {"mode": "live"}, **headers)
    assert status == 400 and "明确" in body
    assert server.controller.process is None
    assert request(server, "POST", "/api/stop", {}, **headers)[0] == 200
    assert request(server, "POST", "/api/start", {"mode": ["live"]}, **headers)[0] == 400
    assert request(server, "GET", "/api/status?mode=invalid", **headers)[0] == 400


def test_corrupted_state_shows_error_without_destroying_file(controller):
    store = controller.store(False)
    store.path.write_text("not json")
    result = controller.snapshot()
    assert result["state_error"]
    assert store.path.read_text() == "not json"


def test_web_session_lock_prevents_second_controller(tmp_path):
    store = StateStore(tmp_path / "web-controller.json")
    with store.lock():
        with pytest.raises(RuntimeError, match="another runner"):
            with StateStore(store.path).lock():
                pass


def test_instrument_catalog_remains_available_for_manual_entry(server, monkeypatch):
    monkeypatch.setattr(
        server.controller,
        "catalog",
        lambda mode: [{"instrument": "BTC-USDT-SWAP", "turnover": 100}],
    )
    headers = {"X-Panel-Token": server.token}
    status, _, body = request(server, "GET", "/api/instruments", **headers)
    assert status == 200
    assert json.loads(body)["items"][0]["instrument"] == "BTC-USDT-SWAP"


def test_selection_endpoint_and_snapshot_are_removed(server, controller):
    headers = {"X-Panel-Token": server.token, "Origin": server.origin}
    status, _, _ = request(
        server,
        "POST",
        "/api/selection",
        {"mode": "watch", "instruments": ["BTC-USDT-SWAP"]},
        **headers,
    )
    assert status == 404
    assert "selection" not in controller.snapshot("watch")


def test_legacy_selection_file_is_ignored(controller):
    controller.store(False).path.with_suffix(".selection.json").write_text("invalid json")
    report = controller.snapshot()
    assert report["state_error"] is None
    assert "selection" not in report


def test_open_command_reuses_only_matching_panel(server, tmp_path):
    assert web.existing_panel(
        server.server_port, server.controller.config_path, server.controller.state_dir
    )
    assert not web.existing_panel(
        server.server_port, server.controller.config_path, tmp_path / "different-state"
    )


def test_catalog_concurrency_does_not_block_health_workers(controller):
    with controller.catalog_lock:
        with pytest.raises(ValueError, match="正在刷新"):
            controller.catalog()


def test_settings_http_save_refresh_and_separate_profile(server):
    headers = {"X-Panel-Token": server.token, "Origin": server.origin}
    values = {
        "live": {"capital_fraction": 0.35, "leverage": 5, "bar": "4H"},
        "strategy": {"atr_period": 21},
    }
    status, _, _ = request(
        server, "POST", "/api/settings", {"mode": "watch", "values": values}, **headers
    )
    assert status == 200
    state = server.controller.snapshot("watch")
    assert state["config"]["capital_fraction"] == 0.35
    assert state["config"]["leverage"] == 5 and state["config"]["bar"] == "4H"
    assert {field["key"] for field in state["parameters"]["fields"]} == {
        "capital_fraction",
        "leverage",
        "bar",
    }
    assert state["parameters"]["values"]["strategy"]["atr_period"] == 21
    reopened = web.Controller(server.controller.config_path, server.controller.state_dir)
    assert reopened.snapshot("watch")["parameters"] == state["parameters"]
    assert reopened.snapshot("demo-watch")["config"]["bar"] == "1H"
    assert request(server, "POST", "/api/settings", {"mode": "watch", "values": values})[0] == 403


def test_parameters_cannot_change_while_worker_or_external_process_runs(controller, monkeypatch):
    from types import SimpleNamespace

    payload = {"live": {"capital_fraction": 0.3}, "strategy": {}}
    controller.process = SimpleNamespace(poll=lambda: None)
    with pytest.raises(ValueError, match="停止策略"):
        controller.save_parameters("watch", payload)
    controller.process = None
    with controller.store(False).lock():
        with pytest.raises(ValueError, match="另一个"):
            controller.save_parameters("watch", payload)
    assert not controller.store(False).path.with_suffix(".settings.json").exists()


def test_okx_connectivity_uses_public_time(controller, monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["base_url"] == controller.config.base_url
            assert kwargs["timeout"] == 2

        def get(self, path, *, private=True):
            assert path == "/api/v5/public/time"
            assert private is False
            calls.append(path)
            return [{"ts": str(int(time.time() * 1000))}]

    monkeypatch.setattr(web, "OKXClient", FakeClient)
    result = controller.okx_connectivity()

    assert result["connected"] is True
    assert result["latency_ms"] >= 0
    assert result["endpoint"] == controller.config.base_url
    assert result["error"] is None
    assert calls == ["/api/v5/public/time"]


def test_okx_connectivity_converts_unexpected_errors_to_status(controller, monkeypatch):
    class BrokenClient:
        def __init__(self, **kwargs):
            pass

        def get(self, *args, **kwargs):
            raise Exception("network exploded")

    monkeypatch.setattr(web, "OKXClient", BrokenClient)
    result = controller.okx_connectivity()

    assert result["connected"] is False
    assert result["latency_ms"] is None
    assert result["error"] == "network exploded"


def test_connectivity_http_endpoint(server, monkeypatch):
    report = {
        "connected": True,
        "checked_at": time.time(),
        "latency_ms": 42,
        "clock_skew_ms": 3,
        "endpoint": "https://openapi.okx.com",
        "error": None,
    }
    monkeypatch.setattr(server.controller, "okx_connectivity", lambda: report)
    headers = {"X-Panel-Token": server.token}
    status, _, body = request(server, "GET", "/api/connectivity", **headers)
    assert status == 200
    assert json.loads(body) == report
    assert request(server, "GET", "/api/connectivity")[0] == 403


def test_worker_stays_in_foreground_terminal_session(controller, monkeypatch):
    import io

    seen = {}

    class FakeProcess:
        pid = 12345

        def __init__(self):
            self.stdout = io.StringIO("")

        def poll(self):
            return None

        def wait(self):
            return 0

        def send_signal(self, signal_number):
            seen["signal"] = signal_number

    def popen(*args, **kwargs):
        seen.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(web.subprocess, "Popen", popen)
    controller.start("watch")

    assert "start_new_session" not in seen
    assert seen["stdout"] is web.subprocess.PIPE
    assert seen["stderr"] is web.subprocess.STDOUT


def mark_worker_ready(controller, pid=12345, age_seconds=0):
    controller.heartbeat.write_text(
        json.dumps(
            {
                "pid": pid,
                "updated_at": time.time() - age_seconds,
                "phase": "ready",
            }
        )
    )


def save_flat_web_state(controller, *instruments):
    controller.store(False).save(
        {
            "version": 1,
            "pending": None,
            "markets": {
                instrument: {"last_bar": None, "position": None}
                for instrument in instruments
            },
        }
    )


def test_manual_entry_approval_queues_only_current_live_candidate(controller):
    from types import SimpleNamespace

    controller.process = SimpleNamespace(
        pid=12345,
        poll=lambda: None,
        send_signal=lambda *_: None,
        wait=lambda: 0,
    )
    controller.mode = "live"
    mark_worker_ready(controller)
    save_flat_web_state(controller, "HYPE-USDT-SWAP")
    candidate = {
        "id": "candidate-1",
        "instrument": "HYPE-USDT-SWAP",
        "volume_usdt_24h": "123456789",
    }
    controller.candidate_store(False).save({"version": 1, "candidates": [candidate]})

    snapshot = controller.snapshot("live")
    assert snapshot["entry_candidates"] == [candidate]
    assert snapshot["entry_approval_enabled"] is True
    assert snapshot["candidate_scan_enabled"] is True
    assert snapshot["candidate_scan_pending"] is False

    controller.approve_entry("live", "candidate-1")
    approvals = controller.approval_store(False).load()["approvals"]
    assert len(approvals) == 1
    assert approvals[0]["id"] == "candidate-1"
    assert approvals[0]["expires_at"] > approvals[0]["requested_at"]
    assert controller.snapshot("live")["entry_approval_enabled"] is False

    controller.approval_store(False).save({"version": 1, "approvals": []})
    with pytest.raises(ValueError, match="不存在"):
        controller.approve_entry("live", "missing")


def test_candidate_api_preserves_worker_volume_order(controller):
    candidates = [
        {"id": "eth", "instrument": "ETH-USDT-SWAP", "volume_usdt_24h": "2500000"},
        {"id": "btc", "instrument": "BTC-USDT-SWAP", "volume_usdt_24h": "1000000"},
    ]
    controller.candidate_store(False).save({"version": 1, "candidates": candidates})
    assert controller.entry_candidates(False) == candidates


def test_manual_entry_approval_rejected_in_readonly_mode(controller):
    from types import SimpleNamespace

    controller.process = SimpleNamespace(
        poll=lambda: None,
        send_signal=lambda *_: None,
        wait=lambda: 0,
    )
    controller.mode = "watch"
    candidate = {
        "id": "candidate-1",
        "instrument": "HYPE-USDT-SWAP",
        "volume_usdt_24h": "123456789",
    }
    controller.candidate_store(False).save({"version": 1, "candidates": [candidate]})
    with pytest.raises(ValueError, match="只读"):
        controller.approve_entry("watch", "candidate-1")


def test_manual_entry_approval_http_endpoint(server, monkeypatch):
    calls = []
    monkeypatch.setattr(
        server.controller,
        "approve_entry",
        lambda mode, candidate_id: calls.append((mode, candidate_id)),
    )
    headers = {"X-Panel-Token": server.token, "Origin": server.origin}
    status, _, body = request(
        server,
        "POST",
        "/api/approve-entry",
        {"mode": "live", "candidate_id": "candidate-1"},
        **headers,
    )
    assert status == 200 and json.loads(body)["ok"] is True
    assert calls == [("live", "candidate-1")]


def test_manual_candidate_scan_request_is_queued_only_in_trading_mode(controller):
    from types import SimpleNamespace

    controller.process = SimpleNamespace(
        pid=12345,
        poll=lambda: None,
        send_signal=lambda *_: None,
        wait=lambda: 0,
    )
    controller.mode = "live"
    mark_worker_ready(controller)
    save_flat_web_state(controller, "HYPE-USDT-SWAP")

    controller.request_candidate_scan("live")
    queued = controller.candidate_scan_store(False).load()
    assert queued["request"]["id"]
    assert queued["request"]["requested_at"] > 0
    assert queued["request"]["expires_at"] > queued["request"]["requested_at"]
    assert controller.snapshot("live")["candidate_scan_pending"] is True

    with pytest.raises(ValueError, match="已提交"):
        controller.request_candidate_scan("live")

    controller.candidate_scan_store(False).save(
        {
            "version": 1,
            "request": None,
            "active": {
                "id": "active-scan",
                "requested_at": time.time(),
                "expires_at": time.time() + 30,
            },
        }
    )
    assert controller.snapshot("live")["candidate_scan_pending"] is True
    with pytest.raises(ValueError, match="已提交"):
        controller.request_candidate_scan("live")

    controller.mode = "watch"
    controller.candidate_scan_store(False).save({"version": 1, "request": None})
    with pytest.raises(ValueError, match="只读"):
        controller.request_candidate_scan("watch")


def test_generate_candidates_http_endpoint(server, monkeypatch):
    calls = []
    monkeypatch.setattr(
        server.controller,
        "request_candidate_scan",
        lambda mode: calls.append(mode),
    )
    headers = {"X-Panel-Token": server.token, "Origin": server.origin}
    status, _, body = request(
        server,
        "POST",
        "/api/generate-candidates",
        {"mode": "live"},
        **headers,
    )
    assert status == 200 and json.loads(body)["ok"] is True
    assert calls == ["live"]


def test_specified_instrument_manual_entry_is_queued_for_managed_flat_coin(controller):
    from types import SimpleNamespace

    controller.process = SimpleNamespace(
        pid=12345,
        poll=lambda: None,
        send_signal=lambda *_: None,
        wait=lambda: 0,
    )
    controller.mode = "live"
    mark_worker_ready(controller)
    controller.store(False).save(
        {
            "version": 1,
            "pending": None,
            "markets": {
                "HYPE-USDT-SWAP": {"last_bar": None, "position": None},
                "XAU-USDT-SWAP": {"last_bar": None, "position": {"qty": "1"}},
            },
        }
    )

    state = controller.snapshot("live")
    assert state["manual_entry_enabled"] is True
    assert state["manual_entry_instruments"] == ["HYPE-USDT-SWAP"]

    controller.request_manual_entry("live", "HYPE-USDT-SWAP", 0.20)
    queued = controller.manual_entry_store(False).load()["request"]
    assert queued["instrument"] == "HYPE-USDT-SWAP"
    assert queued["capital_fraction"] == pytest.approx(0.20)
    assert queued["id"]
    assert queued["expires_at"] > queued["requested_at"]
    assert controller.snapshot("live")["manual_entry_pending"] is True

    controller.manual_entry_store(False).save({"version": 1, "request": None})
    controller.request_manual_entry("live", "ETH-USDT-SWAP", 0.35)
    queued = controller.manual_entry_store(False).load()["request"]
    assert queued["instrument"] == "ETH-USDT-SWAP"
    assert queued["capital_fraction"] == pytest.approx(0.35)


def test_specified_instrument_manual_entry_rejects_existing_position(controller):
    from types import SimpleNamespace

    controller.process = SimpleNamespace(
        pid=12345,
        poll=lambda: None,
        send_signal=lambda *_: None,
        wait=lambda: 0,
    )
    controller.mode = "live"
    mark_worker_ready(controller)
    controller.store(False).save(
        {
            "version": 1,
            "pending": None,
            "markets": {
                "HYPE-USDT-SWAP": {"last_bar": None, "position": {"qty": "1"}},
            },
        }
    )
    with pytest.raises(ValueError, match="已经有策略持仓"):
        controller.request_manual_entry("live", "HYPE-USDT-SWAP", 0.20)


def test_manual_entry_http_endpoint(server, monkeypatch):
    calls = []
    monkeypatch.setattr(
        server.controller,
        "request_manual_entry",
        lambda mode, instrument, capital_fraction: calls.append(
            (mode, instrument, capital_fraction)
        ),
    )
    headers = {"X-Panel-Token": server.token, "Origin": server.origin}
    status, _, body = request(
        server,
        "POST",
        "/api/manual-entry",
        {
            "mode": "live",
            "instrument": "HYPE-USDT-SWAP",
            "capital_fraction": 0.25,
        },
        **headers,
    )
    assert status == 200 and json.loads(body)["ok"] is True
    assert calls == [("live", "HYPE-USDT-SWAP", 0.25)]


def test_stale_heartbeat_disables_and_rejects_manual_trading_controls(controller):
    from types import SimpleNamespace

    controller.process = SimpleNamespace(
        pid=12345,
        poll=lambda: None,
        send_signal=lambda *_: None,
        wait=lambda: 0,
    )
    controller.mode = "live"
    save_flat_web_state(controller, "HYPE-USDT-SWAP")
    mark_worker_ready(controller, age_seconds=120)

    snapshot = controller.snapshot("live")
    assert snapshot["candidate_scan_enabled"] is False
    assert snapshot["entry_approval_enabled"] is False
    assert snapshot["manual_entry_enabled"] is False
    with pytest.raises(ValueError, match="心跳"):
        controller.request_manual_entry("live", "HYPE-USDT-SWAP", 0.20)


def test_manual_actions_are_mutually_exclusive(controller):
    from types import SimpleNamespace

    controller.process = SimpleNamespace(
        pid=12345,
        poll=lambda: None,
        send_signal=lambda *_: None,
        wait=lambda: 0,
    )
    controller.mode = "live"
    mark_worker_ready(controller)
    save_flat_web_state(controller, "HYPE-USDT-SWAP")
    candidate = {
        "id": "candidate-1",
        "instrument": "HYPE-USDT-SWAP",
        "volume_usdt_24h": "123456789",
    }
    controller.candidate_store(False).save({"version": 1, "candidates": [candidate]})

    controller.request_candidate_scan("live")
    with pytest.raises(ValueError, match="扫描"):
        controller.approve_entry("live", "candidate-1")

    controller.candidate_scan_store(False).save({"version": 1, "request": None})
    controller.approve_entry("live", "candidate-1")
    with pytest.raises(ValueError, match="候选开仓"):
        controller.request_manual_entry("live", "HYPE-USDT-SWAP", 0.20)
