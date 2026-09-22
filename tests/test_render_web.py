import base64
import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from trend_pyramiding import render_web
from trend_pyramiding.web import Controller

PASSWORD = "test-only-password-with-at-least-32-characters"
ORIGIN = "https://pyramid.onrender.com"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("PYRAMID_WEB_PASSWORD", PASSWORD)
    controller = Controller(Path(__file__).resolve().parents[1] / "config/okx.toml", tmp_path)
    yield render_web.Application(controller, ORIGIN, PASSWORD)
    controller.close()


def request(app, path="/", method="GET", body=b"", login=True, **changes):
    env = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "HTTP_HOST": "pyramid.onrender.com",
        "HTTP_ORIGIN": ORIGIN,
        "CONTENT_TYPE": "application/json",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
        "HTTP_X_PANEL_TOKEN": app.token,
    }
    if login:
        env["HTTP_AUTHORIZATION"] = (
            "Basic " + base64.b64encode(("admin:" + PASSWORD).encode()).decode()
        )
    env.update(changes)
    response = {}

    def start(status, headers):
        response.update(code=int(status.split()[0]), headers=dict(headers))

    response["body"] = b"".join(app(env, start)).decode()
    return SimpleNamespace(**response)


@pytest.mark.parametrize(
    "path", ["/", "/panel.js", "/panel.css", "/api/status", "/api/connectivity", "/api/start", "/api/stop", "/api/check", "/api/approve-entry", "/api/generate-candidates", "/api/manual-entry"]
)
def test_every_panel_resource_requires_authentication(app, path):
    result = request(
        app,
        path,
        method="POST" if path in ("/api/start", "/api/stop", "/api/check", "/api/approve-entry", "/api/generate-candidates", "/api/manual-entry") else "GET",
        login=False,
    )
    assert result.code == 401
    assert result.headers["WWW-Authenticate"].startswith("Basic")
    assert app.token not in result.body and PASSWORD not in result.body
    assert app.controller.process is None


@pytest.mark.parametrize(
    "auth",
    [
        "Basic invalid!",
        "Basic /w==",
        "Bearer xxx",
        "Basic " + base64.b64encode(b"admin:wrong").decode(),
    ],
)
def test_bad_password_or_malformed_auth_rejected(app, auth):
    assert request(app, HTTP_AUTHORIZATION=auth).code == 401


def test_health_needs_no_credentials_or_public_host_and_has_no_account_data(app):
    result = request(app, "/healthz", login=False, HTTP_HOST="internal.render", HTTP_ORIGIN="")
    assert result.code == 200 and json.loads(result.body) == {"ok": True}
    assert request(app, "/healthz", method="HEAD", login=False).body == ""
    app.controller.closing = True
    assert request(app, "/healthz", login=False).code == 503


def test_authenticated_page_and_status_do_not_start_or_expose_secrets(app):
    page = request(app)
    assert page.code == 200 and "Render 运行" in page.body
    assert app.token in page.body and PASSWORD not in page.body
    assert page.headers["Cache-Control"] == "no-store"
    assert request(app, "/panel.js").code == 200
    status = request(app, "/api/status")
    assert status.code == 200 and json.loads(status.body)["running"] is False
    assert PASSWORD not in status.body
    assert request(app, "/config/okx.toml").code == 404
    assert request(app, "/.env").code == 404


@pytest.mark.parametrize(
    "changes",
    [
        {"HTTP_HOST": "evil.invalid"},
        {"HTTP_ORIGIN": "https://evil.invalid"},
        {"HTTP_X_PANEL_TOKEN": ""},
        {"HTTP_X_PANEL_TOKEN": "错误"},
        {"HTTP_HOST": "evil.invalid", "HTTP_X_FORWARDED_HOST": "pyramid.onrender.com"},
    ],
)
def test_host_origin_and_csrf_protection(app, changes):
    assert (
        request(app, "/api/start", "POST", b'{"mode":"live","confirm_live":true}', **changes).code
        == 403
    )
    assert app.controller.process is None


def test_live_confirmation_body_validation_and_stop(app, monkeypatch):
    assert request(app, "/api/start", "POST", b'{"mode":"live"}').code == 400
    assert app.controller.process is None
    calls = []
    monkeypatch.setattr(
        app.controller, "start", lambda mode, confirm: calls.append((mode, confirm))
    )
    assert request(app, "/api/start", "POST", b'{"mode":"live","confirm_live":true}').code == 200
    assert calls == [("live", True)]
    assert request(app, "/api/stop", "POST", b"{}").code == 200
    for body in (b"[]", b"bad", b"{}" + b" " * 4096):
        assert request(app, "/api/start", "POST", body).code == 400
    assert request(app, "/api/stop", "POST", b"{}", CONTENT_TYPE="text/plain").code == 415


def test_errors_redact_access_password_and_unexpected_internals(app, monkeypatch):
    def check(mode):
        raise ValueError(PASSWORD)

    monkeypatch.setattr(app.controller, "check", check)
    result = request(app, "/api/check", "POST", b"{}")
    assert result.code == 400 and PASSWORD not in result.body and "已隐藏" in result.body

    def broken(mode):
        raise RuntimeError("private internals " + PASSWORD)

    monkeypatch.setattr(app.controller, "check", broken)
    result = request(app, "/api/check", "POST", b"{}")
    assert result.code == 500 and PASSWORD not in result.body


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "http://pyramid.onrender.com",
        "https://user:pass@example.com",
        "https://example.com/path",
        "https://example.com?x=1",
        "https://example.com#x",
        "https://example.com:80",
    ],
)
def test_invalid_public_origin_rejected(origin):
    with pytest.raises(ValueError):
        render_web.public_origin(origin)


def test_weak_password_fails_before_listening(app):
    with pytest.raises(ValueError, match="PYRAMID_WEB_PASSWORD"):
        render_web.Application(app.controller, ORIGIN, "short")


def test_mount_required_and_state_cannot_escape_disk(tmp_path, monkeypatch):
    env = {"PYRAMID_DISK_PATH": str(tmp_path), "PYRAMID_STATE_DIR": str(tmp_path / "state")}
    with pytest.raises(ValueError, match="未挂载"):
        render_web.persistent_state(env)
    monkeypatch.setattr(Path, "is_mount", lambda self: self == tmp_path)
    assert render_web.persistent_state(env) == tmp_path / "state"
    for state in (tmp_path.parent, tmp_path / ".." / "escape"):
        with pytest.raises(ValueError, match="必须位于"):
            render_web.persistent_state(dict(env, PYRAMID_STATE_DIR=str(state)))
    (tmp_path / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        render_web.persistent_state(dict(env, PYRAMID_STATE_DIR=str(tmp_path / "link" / "state")))


def test_render_entrypoint_missing_mount_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("PYRAMID_DISK_PATH", str(tmp_path))
    monkeypatch.setattr(render_web, "serve", lambda *args: pytest.fail("must not listen"))
    with pytest.raises(SystemExit) as error:
        render_web.main()
    assert error.value.code == 1


def test_production_server_sigterm_finishes_owned_worker(tmp_path):
    # Real Waitress + a local child; no credentials or exchange network requests.
    pytest.importorskip("waitress")
    ready, finished = tmp_path / "ready", tmp_path / "finished"
    child = """
import signal, time
from pathlib import Path
stopping = False
def stop(*args):
    global stopping
    stopping = True
signal.signal(signal.SIGTERM, stop)
Path(%r).touch()
while not stopping:
    time.sleep(.01)
time.sleep(.1)
Path(%r).touch()
""" % (str(ready), str(finished))
    script = """
import subprocess, sys
from pathlib import Path
from trend_pyramiding.render_web import Application, serve
from trend_pyramiding.web import Controller
from trend_pyramiding.live import StateStore
controller = Controller(Path(sys.argv[1]), Path(sys.argv[2]))
with StateStore(controller.state_dir / 'web-controller.json').lock():
    controller.process = subprocess.Popen([sys.executable, '-c', sys.argv[3]])
    serve(controller, Application(controller, 'https://pyramid.onrender.com', sys.argv[4]), 0)
"""
    config = Path(__file__).resolve().parents[1] / "config/okx.toml"
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(config), str(tmp_path), child, PASSWORD],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Output is emitted only after signal handlers and the HTTP listener exist.
        assert "尚未启动策略" in proc.stdout.readline()
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        os.kill(proc.pid, signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=15)
        assert proc.returncode == 0, stdout + stderr
        assert finished.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def test_public_origin_normalizes_default_tls_port_and_case():
    assert render_web.public_origin("https://PYRAMID.onrender.com:443/") == ORIGIN


def test_real_waitress_serves_auth_health_and_control_requests(app):
    import http.client
    import threading

    waitress = pytest.importorskip("waitress")
    server = waitress.create_server(app, host="127.0.0.1", port=0)
    stopping = threading.Event()

    def run_server():
        while not stopping.is_set():
            server.asyncore.loop(timeout=0.05, map=server._map, count=1)

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", int(server.effective_port), timeout=5)
    try:
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        assert response.status == 200 and json.loads(response.read()) == {"ok": True}
        connection.request("GET", "/", headers={"Host": "pyramid.onrender.com"})
        response = connection.getresponse()
        assert response.status == 401
        response.read()
        headers = {
            "Host": "pyramid.onrender.com",
            "Authorization": "Basic " + base64.b64encode(("admin:" + PASSWORD).encode()).decode(),
            "X-Panel-Token": app.token,
            "Content-Type": "application/json",
            "Origin": ORIGIN,
        }
        connection.request("GET", "/api/status", headers=headers)
        response = connection.getresponse()
        assert response.status == 200 and not json.loads(response.read())["running"]
        connection.request("POST", "/api/stop", body="{}", headers=headers)
        response = connection.getresponse()
        assert response.status == 200 and json.loads(response.read())["ok"]
    finally:
        connection.close()
        stopping.set()
        thread.join(timeout=5)
        server.task_dispatcher.shutdown()
        server.close()
        assert not thread.is_alive()


def test_render_instrument_catalog_requires_auth_and_selection_is_removed(app, monkeypatch):
    monkeypatch.setattr(
        app.controller,
        "catalog",
        lambda mode: [{"instrument": "HYPE-USDT-SWAP", "turnover": 100}],
    )
    assert request(app, "/api/instruments", login=False).code == 401
    assert request(app, "/api/instruments").code == 200
    assert (
        request(
            app,
            "/api/selection",
            "POST",
            b'{"mode":"watch","instruments":["HYPE-USDT-SWAP"]}',
        ).code
        == 404
    )


def test_cloud_settings_use_persistent_state_and_require_auth(app):
    values = {
        "live": {"capital_fraction": 0.4, "leverage": 3, "bar": "2H"},
        "strategy": {"add_step_atr": 1.5},
    }
    body = json.dumps({"mode": "watch", "values": values}).encode()
    assert request(app, "/api/settings", "POST", body, login=False).code == 401
    assert request(app, "/api/settings", "POST", body, HTTP_X_PANEL_TOKEN="").code == 403
    assert request(app, "/api/settings", "POST", body).code == 200
    assert app.controller.store(False).path.with_suffix(".settings.json").is_file()
    state = json.loads(request(app, "/api/status").body)
    assert state["config"]["capital_fraction"] == 0.4
    assert state["config"]["leverage"] == 3 and state["config"]["bar"] == "2H"
    assert state["parameters"]["values"]["strategy"]["add_step_atr"] == 1.5


def test_render_connectivity_endpoint_is_authenticated(app, monkeypatch):
    report = {
        "connected": True,
        "checked_at": time.time(),
        "latency_ms": 55,
        "clock_skew_ms": -2,
        "endpoint": "https://openapi.okx.com",
        "error": None,
    }
    monkeypatch.setattr(app.controller, "okx_connectivity", lambda: report)
    assert request(app, "/api/connectivity", login=False).code == 401
    response = request(app, "/api/connectivity")
    assert response.code == 200
    assert json.loads(response.body) == report


def test_render_manual_entry_approval_endpoint(app, monkeypatch):
    calls = []
    monkeypatch.setattr(
        app.controller,
        "approve_entry",
        lambda mode, candidate_id: calls.append((mode, candidate_id)),
    )
    body = json.dumps({"mode": "live", "candidate_id": "candidate-1"}).encode()
    response = request(app, "/api/approve-entry", "POST", body)
    assert response.code == 200
    assert json.loads(response.body)["ok"] is True
    assert calls == [("live", "candidate-1")]


def test_render_generate_candidates_endpoint(app, monkeypatch):
    calls = []
    monkeypatch.setattr(
        app.controller,
        "request_candidate_scan",
        lambda mode: calls.append(mode),
    )
    body = json.dumps({"mode": "live"}).encode()
    response = request(app, "/api/generate-candidates", "POST", body)
    assert response.code == 200
    assert json.loads(response.body)["ok"] is True
    assert calls == ["live"]


def test_render_manual_entry_endpoint(app, monkeypatch):
    calls = []
    monkeypatch.setattr(
        app.controller,
        "request_manual_entry",
        lambda mode, instrument: calls.append((mode, instrument)),
    )
    body = json.dumps({"mode": "live", "instrument": "HYPE-USDT-SWAP"}).encode()
    response = request(app, "/api/manual-entry", "POST", body)
    assert response.code == 200
    assert json.loads(response.body)["ok"] is True
    assert calls == [("live", "HYPE-USDT-SWAP")]
