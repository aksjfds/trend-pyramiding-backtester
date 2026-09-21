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


def test_coin_selection_survives_restart_and_is_used_by_runner_config(controller, monkeypatch):
    from trend_pyramiding.live import selected_config

    monkeypatch.setattr(controller, "catalog", lambda mode: [{"instrument": "HYPE-USDT-SWAP"}])
    controller.save_selection("watch", ["HYPE-USDT-SWAP"])
    reopened = web.Controller(controller.config_path, controller.state_dir)
    assert reopened.snapshot()["selection"]["instruments"] == ["HYPE-USDT-SWAP"]
    assert selected_config(reopened.config, reopened.store(False)).instruments == (
        "HYPE-USDT-SWAP",
    )
    assert reopened.snapshot("demo")["selection"]["instruments"] == []
    reopened.save_selection("watch", [])
    assert selected_config(reopened.config, reopened.store(False)).instruments == ()


def test_coin_selection_rejects_unknown_duplicate_or_unsafe_changes(controller, monkeypatch):
    monkeypatch.setattr(controller, "catalog", lambda mode: [{"instrument": "BTC-USDT-SWAP"}])
    for selection in (None, "BTC", [1], ["FAKE-USDT-SWAP"], ["BTC-USDT-SWAP"] * 2):
        with pytest.raises(ValueError):
            controller.save_selection("watch", selection)
    controller.store(False).save(
        {"version": 1, "pending": None, "markets": {"BTC-USDT-SWAP": {"position": {"qty": "1"}}}}
    )
    with pytest.raises(ValueError, match="持仓"):
        controller.save_selection("watch", [])
    controller.store(False).save({"version": 1, "pending": {"order": "unknown"}, "markets": {}})
    with pytest.raises(ValueError, match="待核对"):
        controller.save_selection("watch", [])


def test_selection_http_end_to_end(server, monkeypatch):
    monkeypatch.setattr(
        server.controller,
        "catalog",
        lambda mode: [{"instrument": "BTC-USDT-SWAP", "turnover": 100}],
    )
    headers = {"X-Panel-Token": server.token}
    status, _, body = request(server, "GET", "/api/instruments", **headers)
    assert status == 200 and json.loads(body)["items"][0]["instrument"] == "BTC-USDT-SWAP"
    assert (
        request(
            server,
            "POST",
            "/api/selection",
            {"mode": "watch", "instruments": ["BTC-USDT-SWAP"]},
            **headers,
        )[0]
        == 200
    )
    assert server.controller.snapshot()["selection"]["instruments"] == ["BTC-USDT-SWAP"]
    assert server.controller.process is None


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


def test_invalid_selection_remains_visible_and_prevents_selection(controller):
    controller.store(False).path.with_suffix(".selection.json").write_text("invalid json")
    report = controller.snapshot()
    assert report["state_error"]
    assert report["selection"]["locked"]
    assert report["running"] is False


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
