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

from .live import CONTROL_COMMAND_TTL_SECONDS, LiveConfig, StateStore
from .local_credentials import load_local_credentials
from .okx import Credentials, Instrument, OKXClient, dec
from .runtime import safe_console_print
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
        self.network_lock = threading.Lock()
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

    def candidate_store(self, demo):
        return StateStore(self.store(demo).path.with_suffix(".candidates.json"))

    def approval_store(self, demo):
        return StateStore(self.store(demo).path.with_suffix(".entry-approvals.json"))

    def candidate_scan_store(self, demo):
        return StateStore(self.store(demo).path.with_suffix(".candidate-scan.json"))

    def manual_entry_store(self, demo):
        return StateStore(self.store(demo).path.with_suffix(".manual-entry.json"))

    def _require_trading_worker(self, mode):
        running = self.process is not None and self.process.poll() is None
        if not running or self.stopping or self.closing:
            raise ValueError("策略未运行，不能执行交易操作")
        if mode != self.mode:
            raise ValueError("页面运行方式与当前策略进程不一致，请刷新页面")
        demo, trading = self.profile(self.mode)
        if not trading:
            raise ValueError("只读观察模式不能执行交易操作")
        try:
            beat = json.loads(self.heartbeat.read_text())
        except (OSError, ValueError, TypeError):
            beat = {}
        max_age = max(30, self.config.poll_seconds * 3)
        heartbeat_age = time.time() - float(beat.get("updated_at", 0))
        if (
            beat.get("pid") != self.process.pid
            or beat.get("phase") not in {"ready", "degraded"}
            or heartbeat_age < 0
            or heartbeat_age > max_age
        ):
            raise ValueError("策略心跳已过期或未就绪，请等待运行恢复后重试")
        store = self.store(demo)
        if store.halt_path.exists():
            raise ValueError("策略处于异常暂停状态，不能执行交易操作")
        state = store.load()
        if not state:
            raise ValueError("策略状态尚未初始化，请等待运行准备完成")
        if state.get("pending"):
            raise ValueError("存在待核对订单，不能提交新的交易操作")
        return demo, store, state

    def request_manual_entry(self, mode, instrument, capital_fraction):
        if (
            not isinstance(instrument, str)
            or not instrument.endswith("-USDT-SWAP")
        ):
            raise ValueError("请选择有效的 USDT 永续合约")
        if (
            isinstance(capital_fraction, bool)
            or not isinstance(capital_fraction, (int, float))
            or not 0 < capital_fraction <= 1
        ):
            raise ValueError("资金使用比例必须大于 0% 且不超过 100%")
        with self.lock:
            demo, store, state = self._require_trading_worker(mode)
            tracked = state.get("markets", {}).get(instrument)
            if tracked and tracked.get("position"):
                raise ValueError("该币种已经有策略持仓")

            scan_data = self.candidate_scan_store(demo).load()
            if scan_data and (scan_data.get("request") or scan_data.get("active")):
                raise ValueError("候选扫描正在进行，请等待完成")
            approval_data = self.approval_store(demo).load()
            if approval_data and approval_data.get("approvals"):
                raise ValueError("已有候选开仓正在处理，请等待完成")

            request_store = self.manual_entry_store(demo)
            try:
                with request_store.lock():
                    data = request_store.load() or {"version": 1, "request": None}
                    if data.get("request"):
                        raise ValueError("已有手动开仓请求正在处理，请等待完成")
                    now = time.time()
                    request_store.save(
                        {
                            "version": 1,
                            "request": {
                                "id": secrets.token_hex(12),
                                "instrument": instrument,
                                "capital_fraction": float(capital_fraction),
                                "requested_at": now,
                                "expires_at": now + CONTROL_COMMAND_TTL_SECONDS,
                            },
                        }
                    )
            except RuntimeError:
                raise ValueError("策略正在处理手动开仓，请稍后重试") from None

    def request_candidate_scan(self, mode):
        with self.lock:
            demo, _, _ = self._require_trading_worker(mode)
            manual_data = self.manual_entry_store(demo).load()
            if manual_data and manual_data.get("request"):
                raise ValueError("已有手动开仓正在处理，请等待完成")
            approval_data = self.approval_store(demo).load()
            if approval_data and approval_data.get("approvals"):
                raise ValueError("已有候选开仓正在处理，请等待完成")

            scan_store = self.candidate_scan_store(demo)
            try:
                with scan_store.lock():
                    data = scan_store.load() or {"version": 1, "request": None}
                    if data.get("request") or data.get("active"):
                        raise ValueError("候选扫描已提交，请等待完成")
                    now = time.time()
                    scan_store.save(
                        {
                            "version": 1,
                            "request": {
                                "id": secrets.token_hex(12),
                                "requested_at": now,
                                "expires_at": now + CONTROL_COMMAND_TTL_SECONDS,
                            },
                        }
                    )
            except RuntimeError:
                raise ValueError("策略正在处理候选扫描，请稍后重试") from None

    def entry_candidates(self, demo):
        data = self.candidate_store(demo).load()
        if data is None:
            return []
        candidates = data.get("candidates")
        if not isinstance(candidates, list):
            raise ValueError("待确认开仓列表格式错误")
        result = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if not {"id", "instrument", "volume_usdt_24h"} <= set(candidate):
                continue
            result.append(
                {
                    "id": str(candidate["id"]),
                    "instrument": str(candidate["instrument"]),
                    "volume_usdt_24h": str(candidate["volume_usdt_24h"]),
                }
            )
        # Preserve the worker's 24h-volume ranking.
        return result

    def approve_entry(self, mode, candidate_id):
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError("请选择有效的待开仓信号")
        with self.lock:
            demo, _, _ = self._require_trading_worker(mode)
            manual_data = self.manual_entry_store(demo).load()
            if manual_data and manual_data.get("request"):
                raise ValueError("已有手动开仓正在处理，请等待完成")
            scan_data = self.candidate_scan_store(demo).load()
            if scan_data and (scan_data.get("request") or scan_data.get("active")):
                raise ValueError("候选扫描正在进行，请等待完成")
            candidate = next(
                (item for item in self.entry_candidates(demo) if item["id"] == candidate_id),
                None,
            )
            if candidate is None:
                raise ValueError("该候选已不存在，请重新生成候选")

            queue = self.approval_store(demo)
            try:
                with queue.lock():
                    data = queue.load() or {"version": 1, "approvals": []}
                    approvals = data.get("approvals", [])
                    if not isinstance(approvals, list):
                        raise ValueError("开仓批准队列格式错误")
                    if approvals:
                        raise ValueError("已有候选开仓正在处理，请等待完成")
                    now = time.time()
                    approvals = [
                        {
                            "id": candidate_id,
                            "requested_at": now,
                            "expires_at": now + CONTROL_COMMAND_TTL_SECONDS,
                        }
                    ]
                    queue.save({"version": 1, "approvals": approvals})
            except RuntimeError:
                raise ValueError("策略正在处理开仓批准，请稍后重试") from None

    def okx_connectivity(self):
        """Probe the configured OKX REST host and always return a displayable result."""
        with self.network_lock:
            started = time.perf_counter()
            before = time.time()
            try:
                client = OKXClient(base_url=self.config.base_url, demo=False, timeout=2)
                rows = client.get("/api/v5/public/time", private=False)
                after = time.time()
                if not rows or not isinstance(rows[0], dict) or not rows[0].get("ts"):
                    raise ValueError("OKX public time response is invalid")
                server_time = float(rows[0]["ts"]) / 1000
                return {
                    "connected": True,
                    "checked_at": after,
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                    "clock_skew_ms": round((server_time - (before + after) / 2) * 1000),
                    "endpoint": self.config.base_url,
                    "error": None,
                }
            except Exception as exc:
                detail = self.redact(str(exc)).strip() or type(exc).__name__
                return {
                    "connected": False,
                    "checked_at": time.time(),
                    "latency_ms": None,
                    "clock_skew_ms": None,
                    "endpoint": self.config.base_url,
                    "error": detail[:500],
                }

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

    def effective_settings(self, demo):
        return load_settings(
            replace(self.config, demo=demo), self.strategy, self.store(demo)
        )

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
                safe_console_print(entry["text"], flush=True)
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
            heartbeat_fresh = bool(
                heartbeat
                and heartbeat.get("phase") in {"ready", "degraded"}
                and 0 <= time.time() - float(heartbeat.get("updated_at", 0))
                <= max(30, self.config.poll_seconds * 3)
            )
            config, strategy = replace(self.config, demo=demo), self.strategy
            try:
                config, strategy = self.effective_settings(demo)
            except (ValueError, OSError, KeyError, TypeError, AttributeError):
                error = "无法读取已保存参数，请检查 settings.json"
            candidates = []
            candidate_error = None
            scan_pending = False
            manual_entry_pending = False
            approval_pending = False
            try:
                candidates = self.entry_candidates(demo)
                scan_data = self.candidate_scan_store(demo).load()
                scan_pending = bool(
                    scan_data and (scan_data.get("request") or scan_data.get("active"))
                )
                manual_data = self.manual_entry_store(demo).load()
                manual_entry_pending = bool(manual_data and manual_data.get("request"))
                approval_data = self.approval_store(demo).load()
                approval_pending = bool(approval_data and approval_data.get("approvals"))
            except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
                candidate_error = self.redact(str(exc))
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
                },
                "parameters": {"values": settings_values(config, strategy), "fields": schema()},
                "settings_locked": bool(
                    running
                    or halt
                    or error
                    or (
                        state
                        and (
                            state.get("pending")
                            or any(
                                m.get("position")
                                for m in state.get("markets", {}).values()
                            )
                        )
                    )
                ),
                "entry_candidates": candidates,
                "entry_candidate_error": candidate_error,
                "candidate_scan_pending": scan_pending,
                "candidate_scan_enabled": bool(
                    running
                    and heartbeat_fresh
                    and self.profile(self.mode)[1]
                    and not self.stopping
                    and not halt
                    and not bool((state or {}).get("pending"))
                    and not manual_entry_pending
                    and not approval_pending
                ),
                "entry_approval_enabled": bool(
                    running
                    and heartbeat_fresh
                    and self.profile(self.mode)[1]
                    and not self.stopping
                    and not halt
                    and not bool((state or {}).get("pending"))
                    and not manual_entry_pending
                    and not scan_pending
                    and not approval_pending
                ),
                "entry_approval_pending": approval_pending,
                "manual_entry_pending": manual_entry_pending,
                "manual_entry_enabled": bool(
                    running
                    and heartbeat_fresh
                    and self.profile(self.mode)[1]
                    and not self.stopping
                    and not halt
                    and not bool((state or {}).get("pending"))
                    and not manual_entry_pending
                    and not scan_pending
                    and not approval_pending
                ),
                "manual_entry_instruments": [
                    row["instrument"]
                    for row in rows
                    if not row.get("contracts") or float(row.get("contracts") or 0) == 0
                ],
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
        elif path == "/api/connectivity":
            self.send(200, self.server.controller.okx_connectivity())
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
            elif self.path == "/api/check":
                controller.check(data.get("mode", "watch"))
            elif self.path == "/api/approve-entry":
                controller.approve_entry(data.get("mode", "watch"), data.get("candidate_id"))
            elif self.path == "/api/generate-candidates":
                controller.request_candidate_scan(data.get("mode", "watch"))
            elif self.path == "/api/manual-entry":
                controller.request_manual_entry(
                    data.get("mode", "watch"),
                    data.get("instrument"),
                    data.get("capital_fraction"),
                )
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
        if existing_panel(args.port, args.config, state_dir):
            origin = f"http://127.0.0.1:{args.port}"
            raise ValueError(
                f"已有网页服务正在运行：{origin}。请先停止旧进程，再在当前终端重新启动。"
            )
        controller = Controller(args.config, state_dir)
        with StateStore(state_dir / "web-controller.json").lock():
            with PanelServer(args.port, controller) as server:

                def shutdown(signum, frame):
                    threading.Thread(target=server.shutdown, daemon=True).start()

                previous = {s: signal.signal(s, shutdown) for s in (signal.SIGINT, signal.SIGTERM)}
                safe_console_print(f"网页已启动：{server.origin}（尚未启动策略）", flush=True)
                if args.open:
                    webbrowser.open(server.origin)
                try:
                    server.serve_forever()
                finally:
                    safe_console_print("正在停止策略，等待当前操作结束…", flush=True)
                    controller.close()
                    for s, handler in previous.items():
                        signal.signal(s, handler)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
