"""Loopback-only control panel. Exchange access stays in separate CLI processes."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path

from .live import LiveConfig, StateStore, selected_config
from .local_credentials import load_local_credentials
from .okx import Credentials, Instrument, OKXClient, dec
from .settings import load_settings, save_settings, schema, settings_values

MODES = {
    "watch": (False, False),
    "demo-watch": (True, False),
    "demo": (True, True),
    "live": (False, True),
}


class Controller:
    def __init__(self, config: Path, state_dir: Path):
        self.config_path = config.resolve()
        self.config, self.strategy = LiveConfig.load(self.config_path)
        self.state_dir = state_dir.resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.process = None
        self.mode = None
        self.stopping = False
        self.closing = False
        self.started_at = None
        self.logs = deque(maxlen=300)
        self.accounts = {}
        self.checking = False
        self.catalogs = {}
        self.catalog_lock = threading.Lock()
        self.heartbeat = self.state_dir / "web-heartbeat.json"

    def redact(self, text):
        password = os.environ.get("PYRAMID_WEB_PASSWORD")
        if password:
            text = text.replace(password, "[已隐藏]")
        for prefix in ("OKX_", "OKX_DEMO_"):
            for suffix in ("API_KEY", "API_SECRET", "API_PASSPHRASE"):
                value = os.environ.get(prefix + suffix)
                if value:
                    text = text.replace(value, "[已隐藏]")
        return text

    def profile(self, mode):
        if not isinstance(mode, str) or mode not in MODES:
            raise ValueError("请选择有效的运行方式")
        demo, trading = MODES[mode]
        if mode == "live" and self.config.demo:
            raise ValueError("当前配置启用了 demo，请先在 config/okx.toml 中关闭后重启网页")
        return demo or self.config.demo, trading

    def store(self, demo):
        return StateStore(self.state_dir / f"okx-{'demo' if demo else 'live'}.json")

    def catalog(self, mode="watch"):
        demo, _ = self.profile(mode)
        if not self.catalog_lock.acquire(blocking=False):
            raise ValueError("币种列表正在刷新，请稍后重试")
        try:
            cached = self.catalogs.get(demo)
            if cached and time.time() - cached["time"] < 300:
                return cached["items"]
            client = OKXClient(base_url=self.config.base_url, demo=demo, timeout=8)
            eligible = {}
            for row in client.get(
                "/api/v5/public/instruments", {"instType": "SWAP"}, private=False
            ):
                try:
                    instrument = Instrument.parse(row)
                    eligible[instrument.inst_id] = instrument.category
                except (ValueError, KeyError):
                    continue
            items = []
            for row in client.get("/api/v5/market/tickers", {"instType": "SWAP"}, private=False):
                if row["instId"] in eligible:
                    turnover = float(dec(row.get("volCcy24h") or "0") * dec(row.get("last") or "0"))
                    if turnover > 0:
                        items.append(
                            {
                                "instrument": row["instId"],
                                "turnover": turnover,
                                "category": eligible[row["instId"]],
                            }
                        )
            items.sort(key=lambda item: item["turnover"], reverse=True)
            if not items:
                raise ValueError("暂无可用 USDT 永续合约，请稍后重试")
            self.catalogs[demo] = {"time": time.time(), "items": items}
            return items
        finally:
            self.catalog_lock.release()

    def save_selection(self, mode, instruments):
        demo, _ = self.profile(mode)
        if (
            not isinstance(instruments, list)
            or len(instruments) > 10
            or any(not isinstance(x, str) for x in instruments)
            or len(set(instruments)) != len(instruments)
        ):
            raise ValueError("请选择最多 10 个不同的永续合约")
        if instruments:
            available = {item["instrument"] for item in self.catalog(mode)}
            if not set(instruments) <= available:
                raise ValueError("所选币种不支持交易或已下架，请刷新币种列表")
        with self.lock:
            if self.closing or self.checking or (self.process and self.process.poll() is None):
                raise ValueError("请等待账户读取完成并停止运行后再更改币种")
            store = self.store(demo)
            with store.lock():
                state = store.load()
                if store.halt_path.exists() or (
                    state
                    and (
                        state.get("pending")
                        or any(x.get("position") for x in state["markets"].values())
                    )
                ):
                    raise ValueError("当前仍有持仓、待核对订单或暂停记录，不能更换币种")
                StateStore(store.path.with_suffix(".selection.json")).save(
                    {"version": 1, "instruments": instruments}
                )
                cached = self.accounts.get("demo" if demo else "live")
                if cached and cached.get("data"):
                    cached["data"]["instruments"] = list(instruments)

    def effective_settings(self, demo):
        config, strategy = load_settings(
            replace(self.config, demo=demo), self.strategy, self.store(demo)
        )
        return selected_config(config, self.store(demo)), strategy

    def save_parameters(self, mode, values):
        demo, _ = self.profile(mode)
        with self.lock:
            if self.closing or self.checking or (self.process and self.process.poll() is None):
                raise ValueError("请等待账户读取完成并停止策略后再修改参数")
            if self.busy_profiles():
                raise ValueError("另一个策略进程正在运行，请先停止")
            store = self.store(demo)
            with store.lock():
                config, strategy = self.effective_settings(demo)
                config, strategy = save_settings(config, strategy, store, values)
                cached = self.accounts.get("demo" if demo else "live")
                if cached and cached.get("data"):
                    equity = cached["data"].get("account_equity_usdt_equivalent")
                    cached["data"]["capital_limit_usdt_approx"] = (
                        equity * config.capital_fraction if equity is not None else None
                    )

    def command(self, mode, *, check=False):
        demo, trading = self.profile(mode)
        command = [
            sys.executable,
            "-u",
            "-m",
            "trend_pyramiding.okx_cli",
            "check" if check else "run" if trading else "watch",
            "--config",
            str(self.config_path),
            "--state-dir",
            str(self.state_dir),
        ]
        if demo:
            command.append("--demo")
        elif trading and not check:
            command.append("--live")
        return command

    def busy_profiles(self):
        busy = []
        for demo in (False, True):
            try:
                with self.store(demo).lock():
                    pass
            except RuntimeError:
                busy.append("demo" if demo else "live")
        return busy

    def start(self, mode, confirm_live=False):
        with self.lock:
            if self.closing:
                raise ValueError("网页服务正在关闭")
            if self.process and self.process.poll() is None:
                raise ValueError("策略已在运行或停止中，请等待退出后再启动")
            demo, trading = self.profile(mode)
            if mode == "live" and confirm_live is not True:
                raise ValueError("请明确点击启动实盘交易")
            self.effective_settings(demo)  # Validate saved parameters before spawning a worker.
            Credentials.load(demo)
            if self.busy_profiles():
                raise ValueError("状态目录正被其他策略进程使用，请先在原终端停止该进程")
            if trading and self.store(demo).halt_path.exists():
                raise ValueError("策略因异常暂停，请核查日志和订单后使用 clear-halt，再启动")
            env = dict(
                os.environ,
                PYRAMID_HEARTBEAT_FILE=str(self.heartbeat),
                PYRAMID_PARENT_PID=str(os.getpid()),
            )
            self.heartbeat.unlink(missing_ok=True)
            process = subprocess.Popen(
                self.command(mode),
                cwd=self.config_path.parent.parent,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
            self.process = process
            self.mode = mode
            self.stopping = False
            self.started_at = time.time()
            self.logs.clear()
            threading.Thread(target=self._read_logs, args=(process,), daemon=True).start()

    def _read_logs(self, process):
        try:
            for line in process.stdout:
                entry = {"time": time.time(), "text": self.redact(line.rstrip())[:4000]}
                with self.lock:
                    if self.process is process:
                        self.logs.append(entry)
        finally:
            process.stdout.close()
            process.wait()

    def stop(self):
        with self.lock:
            if self.process and self.process.poll() is None and not self.stopping:
                self.stopping = True
                try:
                    self.process.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def close(self):
        with self.lock:
            self.closing = True
        self.stop()
        if self.process:
            # Do not force-kill an in-flight order or its protective stop verification.
            self.process.wait()

    def check(self, mode):
        demo, _ = self.profile(mode)
        with self.lock:
            if self.checking or self.closing:
                raise ValueError("账户检查正在进行或网页正在关闭")
            self.checking = True
        key = "demo" if demo else "live"
        try:
            Credentials.load(demo)
            result = subprocess.run(
                self.command(mode, check=True),
                cwd=self.config_path.parent.parent,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode:
                raise ValueError(self.redact(result.stderr.strip() or "账户检查失败")[:2000])
            data = json.loads(result.stdout)
            # Explicit fields only; never return raw process environment or credentials.
            report = {
                k: data[k]
                for k in (
                    "authenticated",
                    "account_equity_usdt_equivalent",
                    "available_usdt",
                    "capital_limit_usdt_approx",
                    "open_positions",
                    "pending_orders",
                    "instruments",
                    "trade_permission",
                )
            }
            report["warnings"] = data.get("warnings", [])
            with self.lock:
                self.accounts[key] = {"time": time.time(), "data": report, "error": None}
        except (ValueError, OSError, subprocess.TimeoutExpired, KeyError) as exc:
            error = (
                "账户检查超时，请稍后重试"
                if isinstance(exc, subprocess.TimeoutExpired)
                else self.redact(str(exc))
            )
            with self.lock:
                previous = self.accounts.get(key, {})
                self.accounts[key] = {**previous, "error": error, "error_time": time.time()}
            raise ValueError(error) from None
        finally:
            with self.lock:
                self.checking = False

    def snapshot(self, selected="watch"):
        with self.lock:
            demo, _ = self.profile(selected)
            running = self.process is not None and self.process.poll() is None
            exit_code = self.process.poll() if self.process else None
            if running:
                demo, _ = self.profile(self.mode)
            profile = "demo" if demo else "live"
            store = self.store(demo)
            error = None
            rows = []
            state = None
            try:
                state = store.load()
                for name, market in (state or {}).get("markets", {}).items():
                    pos = market.get("position") or {}
                    rows.append(
                        {
                            "instrument": name,
                            "contracts": pos.get("qty", "0"),
                            "average": pos.get("avg"),
                            "stop": pos.get("stop"),
                            "legs": len(pos.get("legs", [])),
                            "last_bar": market.get("last_bar"),
                        }
                    )
            except (ValueError, OSError, KeyError, TypeError, AttributeError):
                error = "无法读取策略状态，请检查状态文件；不要删除原始订单记录"
            halt = None
            if store.halt_path.exists():
                try:
                    marker = json.loads(store.halt_path.read_text())
                    halt = {
                        "time": marker.get("time"),
                        "detail": self.redact(str(marker.get("detail", "策略异常"))),
                    }
                except (ValueError, OSError, AttributeError):
                    halt = {"detail": "存在异常暂停标记，请检查状态目录"}
            heartbeat = None
            if running:
                try:
                    beat = json.loads(self.heartbeat.read_text())
                    if beat.get("pid") == self.process.pid:
                        heartbeat = {
                            "phase": beat.get("phase"),
                            "updated_at": beat.get("updated_at"),
                        }
                except (OSError, ValueError, AttributeError):
                    pass
            selected = []
            try:
                selected = list(selected_config(self.config, store).instruments)
            except (ValueError, OSError, KeyError, TypeError, AttributeError):
                error = "无法读取币种选择，请检查 selection.json；不要删除交易状态"
            config, strategy = replace(self.config, demo=demo), self.strategy
            try:
                config, strategy = self.effective_settings(demo)
            except (ValueError, OSError, KeyError, TypeError, AttributeError):
                error = "无法读取已保存参数，请检查 settings.json"
            credential_status = {}
            for is_demo in (False, True):
                try:
                    Credentials.load(is_demo)
                    credential_status["demo" if is_demo else "live"] = True
                except ValueError:
                    credential_status["demo" if is_demo else "live"] = False
            return {
                "running": running,
                "stopping": running and self.stopping,
                "mode": self.mode,
                "profile": profile,
                "exit_code": exit_code,
                "started_at": self.started_at,
                "heartbeat": heartbeat,
                "external_process": self.busy_profiles() if not running else [],
                "credentials": credential_status,
                "checking": self.checking,
                "config": {
                    "bar": config.bar,
                    "leverage": config.leverage,
                    "capital_fraction": config.capital_fraction,
                    "top_n": config.top_n,
                },
                "parameters": {"values": settings_values(config, strategy), "fields": schema()},
                "selection": {
                    "instruments": selected,
                    "locked": bool(
                        running
                        or halt
                        or error
                        or (
                            state
                            and (
                                state.get("pending")
                                or any(m.get("position") for m in state.get("markets", {}).values())
                            )
                        )
                    ),
                },
                "account": self.accounts.get(profile),
                "markets": rows,
                "capital_ceiling": (state or {}).get("capital_ceiling"),
                "pending": bool((state or {}).get("pending")),
                "halt": halt,
                "state_error": error,
                "logs": list(self.logs),
            }


def panel_identity(config, state_dir):
    return hashlib.sha256(f"{config.resolve()}|{state_dir.resolve()}".encode()).hexdigest()


def existing_panel(port, config, state_dir):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        return response.status == 200 and response.getheader(
            "X-Pyramid-Instance"
        ) == panel_identity(config, state_dir)
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, controller):
        self.controller = controller
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    timeout = 15

    def log_message(self, *args):
        pass

    def send(self, code, body, kind="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "X-Pyramid-Instance",
            panel_identity(self.server.controller.config_path, self.server.controller.state_dir),
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def authorized(self, api=False):
        # Reject hostile Host headers (DNS rebinding), cross-origin requests and CSRF.
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            self.send(403, {"error": "请通过本机 127.0.0.1 地址访问"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            self.send(403, {"error": "不允许跨站访问"})
            return False
        if api and not secrets.compare_digest(
            self.headers.get("X-Panel-Token", ""), self.server.token
        ):
            self.send(403, {"error": "页面会话已失效，请刷新页面"})
            return False
        return True

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if not self.authorized(api=path.startswith("/api/")):
            return
        if path in ("/", "/panel.js", "/panel.css"):
            name = {"/": "panel.html", "/panel.js": "panel.js", "/panel.css": "panel.css"}[path]
            content = files("trend_pyramiding").joinpath(name).read_text()
            if path == "/":
                content = content.replace("__PANEL_TOKEN__", self.server.token)
            kind = {"/": "text/html", "/panel.js": "text/javascript", "/panel.css": "text/css"}[
                path
            ]
            self.send(200, content.encode(), kind + "; charset=utf-8")
        elif path in ("/api/status", "/api/instruments"):
            from urllib.parse import parse_qs

            try:
                selected = parse_qs(query).get("mode", ["watch"])[0]
                result = (
                    self.server.controller.snapshot(selected)
                    if path == "/api/status"
                    else {"items": self.server.controller.catalog(selected)}
                )
                self.send(200, result)
            except (ValueError, RuntimeError, OSError) as exc:
                self.send(400, {"error": self.server.controller.redact(str(exc))})
        else:
            self.send(404, {"error": "未找到页面"})

    def do_POST(self):
        if not self.authorized(api=True):
            return
        if self.headers.get("Content-Type") != "application/json":
            self.send(415, {"error": "需要 JSON 请求"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("请求长度无效")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("请求格式无效")
            controller = self.server.controller
            if self.path == "/api/start":
                controller.start(data.get("mode", "watch"), data.get("confirm_live", False))
            elif self.path == "/api/stop":
                controller.stop()
            elif self.path == "/api/settings":
                controller.save_parameters(data.get("mode", "watch"), data.get("values"))
            elif self.path == "/api/selection":
                controller.save_selection(data.get("mode", "watch"), data.get("instruments"))
            elif self.path == "/api/check":
                controller.check(data.get("mode", "watch"))
            else:
                self.send(404, {"error": "操作不存在"})
                return
            self.send(200, {"ok": True})
        except (ValueError, RuntimeError, OSError) as exc:
            self.send(400, {"error": self.server.controller.redact(str(exc))})


def main():
    parser = argparse.ArgumentParser(description="本地 OKX 策略网页控制台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--config", type=Path, default=Path(os.environ.get("PYRAMID_CONFIG", "config/okx.toml"))
    )
    parser.add_argument("--state-dir", type=Path, default=os.environ.get("PYRAMID_STATE_DIR"))
    parser.add_argument("--open", action="store_true", help="自动打开浏览器")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("端口必须在 0–65535 之间")
    try:
        load_local_credentials(args.config)
        state_dir = args.state_dir or args.config.resolve().parent.parent / "state"
        if args.open and existing_panel(args.port, args.config, state_dir):
            origin = f"http://127.0.0.1:{args.port}"
            print(f"网页服务已在运行：{origin}，已打开现有页面。", flush=True)
            webbrowser.open(origin)
            return
        controller = Controller(args.config, state_dir)
        with StateStore(state_dir / "web-controller.json").lock():
            with PanelServer(args.port, controller) as server:

                def shutdown(signum, frame):
                    threading.Thread(target=server.shutdown, daemon=True).start()

                previous = {s: signal.signal(s, shutdown) for s in (signal.SIGINT, signal.SIGTERM)}
                print(f"网页已启动：{server.origin}（尚未启动策略）", flush=True)
                if args.open:
                    webbrowser.open(server.origin)
                try:
                    server.serve_forever()
                finally:
                    print("正在停止策略，等待当前操作结束…", flush=True)
                    controller.close()
                    for s, handler in previous.items():
                        signal.signal(s, handler)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
