"""Authenticated, single-process Render entry point for the strategy controller."""

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import signal
import sys
from http import HTTPStatus
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .live import StateStore
from .web import Controller


def public_origin(value):
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.path not in ("", "/")
        or url.query
        or url.fragment
        or url.port not in (None, 443)
    ):
        raise ValueError("PYRAMID_PUBLIC_URL / RENDER_EXTERNAL_URL 必须是 HTTPS 网站根地址")
    host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
    return f"https://{host}"


def persistent_state(env):
    mount = Path(env.get("PYRAMID_DISK_PATH", "/var/data")).resolve()
    state = Path(env.get("PYRAMID_STATE_DIR", "/var/data/pyramid")).resolve()
    if mount == Path("/") or not mount.is_mount():
        raise ValueError("持久化磁盘未挂载，请在 Render 添加挂载到 /var/data 的 Disk")
    if not state.is_relative_to(mount):
        raise ValueError("PYRAMID_STATE_DIR 必须位于 PYRAMID_DISK_PATH 持久化磁盘内")
    return state


class Application:
    """WSGI adapter; Basic authentication is protected by Render's HTTPS edge."""

    def __init__(self, controller, origin, password):
        if len(password) < 24 or password != password.strip():
            raise ValueError("PYRAMID_WEB_PASSWORD 必须设置为至少 24 字符且首尾无空白的密码")
        self.controller = controller
        self.origin = public_origin(origin)
        self.host = urlsplit(self.origin).netloc
        self.credentials = ("admin:" + password).encode("utf-8")
        self.token = secrets.token_urlsafe(32)

    def __call__(self, env, start_response):
        def respond(code, body, kind="application/json; charset=utf-8", extra=()):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers = [
                ("Content-Type", kind),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
                ("X-Frame-Options", "DENY"),
                ("Referrer-Policy", "no-referrer"),
                ("Strict-Transport-Security", "max-age=31536000"),
                (
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self'; "
                    "style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
                ),
                *extra,
            ]
            start_response(f"{code} {HTTPStatus(code).phrase}", headers)
            return [b"" if env["REQUEST_METHOD"] == "HEAD" else body]

        method, path = env["REQUEST_METHOD"], env.get("PATH_INFO", "/")
        # This endpoint reports the web service only. A stopped strategy is healthy.
        if path == "/healthz" and method in ("GET", "HEAD"):
            code = 503 if self.controller.closing else 200
            return respond(code, {"ok": code == 200})
        if env.get("HTTP_HOST") != self.host:
            return respond(403, {"error": "请使用配置的网站地址访问"})
        if env.get("HTTP_ORIGIN", self.origin) != self.origin:
            return respond(403, {"error": "不允许跨站访问"})
        authorization = env.get("HTTP_AUTHORIZATION", "")
        try:
            scheme, encoded = authorization.split(" ", 1)
            provided = (
                base64.b64decode(encoded, validate=True) if scheme.lower() == "basic" else b""
            )
        except (ValueError, binascii.Error):
            provided = b""
        if not secrets.compare_digest(provided, self.credentials):
            return respond(
                401,
                {"error": "请使用 admin 和网页访问密码登录"},
                extra=[
                    ("WWW-Authenticate", 'Basic realm="Pyramid", charset="UTF-8"'),
                ],
            )
        if path.startswith("/api/") and not secrets.compare_digest(
            env.get("HTTP_X_PANEL_TOKEN", "").encode("utf-8"), self.token.encode()
        ):
            return respond(403, {"error": "页面会话已失效，请刷新页面"})
        try:
            if method in ("GET", "HEAD") and path in ("/", "/panel.js", "/panel.css"):
                name, kind = {
                    "/": ("panel.html", "text/html"),
                    "/panel.js": ("panel.js", "text/javascript"),
                    "/panel.css": ("panel.css", "text/css"),
                }[path]
                content = files("trend_pyramiding").joinpath(name).read_text()
                content = content.replace("__PANEL_TOKEN__", self.token)
                content = content.replace("本地运行", "Render 运行")
                return respond(200, content.encode(), kind + "; charset=utf-8")
            if method == "GET" and path == "/api/status":
                mode = parse_qs(env.get("QUERY_STRING", "")).get("mode", ["watch"])[0]
                return respond(200, self.controller.snapshot(mode))
            if method == "GET" and path == "/api/instruments":
                mode = parse_qs(env.get("QUERY_STRING", "")).get("mode", ["watch"])[0]
                return respond(200, {"items": self.controller.catalog(mode)})
            if method == "POST" and path in (
                "/api/start",
                "/api/stop",
                "/api/check",
                "/api/selection",
                "/api/settings",
            ):
                if env.get("CONTENT_TYPE") != "application/json":
                    return respond(415, {"error": "需要 JSON 请求"})
                length = int(env.get("CONTENT_LENGTH", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("请求长度无效")
                data = json.loads(env["wsgi.input"].read(length))
                if not isinstance(data, dict):
                    raise ValueError("请求格式无效")
                if path == "/api/start":
                    self.controller.start(
                        data.get("mode", "watch"), data.get("confirm_live", False)
                    )
                elif path == "/api/settings":
                    self.controller.save_parameters(data.get("mode", "watch"), data.get("values"))
                elif path == "/api/selection":
                    self.controller.save_selection(
                        data.get("mode", "watch"), data.get("instruments")
                    )
                elif path == "/api/stop":
                    self.controller.stop()
                else:
                    self.controller.check(data.get("mode", "watch"))
                return respond(200, {"ok": True})
            return respond(404, {"error": "未找到页面或操作"})
        except (ValueError, OSError) as exc:
            return respond(400, {"error": self.controller.redact(str(exc))})
        except Exception:
            # Do not send tracebacks, credentials or account internals to clients/logs.
            return respond(500, {"error": "网页服务处理失败，请检查持久化磁盘和服务状态"})


def serve(controller, application, port):
    from waitress import create_server

    server = create_server(
        application,
        host="0.0.0.0",
        port=port,
        threads=4,
        max_request_body_size=4096,
        max_request_header_size=16384,
        connection_limit=100,
        channel_timeout=30,
        expose_tracebacks=False,
        clear_untrusted_proxy_headers=True,
    )

    def shutdown(signum, frame):
        # Ignore repeated termination signals while an in-flight order finishes.
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, signal.SIG_IGN)
        with controller.lock:
            controller.closing = True
        controller.stop()
        raise SystemExit(0)

    previous = {sig: signal.signal(sig, shutdown) for sig in (signal.SIGINT, signal.SIGTERM)}
    print(f"网页已启动：{application.origin}（尚未启动策略）", flush=True)
    try:
        server.run()
    finally:
        controller.close()
        server.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    try:
        port = int(os.environ.get("PORT", "10000"))
        if not 1 <= port <= 65535:
            raise ValueError("PORT 必须在 1–65535 之间")
        state_dir = persistent_state(os.environ)
        origin = os.environ.get("PYRAMID_PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")
        controller = Controller(
            Path(os.environ.get("PYRAMID_CONFIG", "config/okx.toml")), state_dir
        )
        application = Application(controller, origin, os.environ.get("PYRAMID_WEB_PASSWORD", ""))
        with StateStore(state_dir / "web-controller.json").lock():
            serve(controller, application, port)
    except (OSError, ValueError, RuntimeError) as exc:
        # Configuration errors here contain variable names/paths, never secret values.
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
