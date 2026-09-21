"""Small OKX V5 REST adapter. Reads are retryable; writes are never retried."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import stat
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from pathlib import Path

HOSTS = {
    "https://openapi.okx.com",
    "https://www.okx.com",
    "https://us.okx.com",
    "https://eea.okx.com",
    "https://my.okx.com",
}


class OKXError(RuntimeError):
    def __init__(self, code: str, operation: str):
        self.code = str(code)
        super().__init__(f"OKX {operation} failed (code={self.code})")


class UncertainWrite(RuntimeError):
    """A write may have reached the exchange. Reconcile; do not blindly resend."""


def dec(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("invalid numeric value") from None
    if not result.is_finite():
        raise ValueError("non-finite numeric value")
    return result


def rounded(value, step, *, up: bool = False) -> Decimal:
    step = dec(step)
    if step <= 0:
        raise ValueError("step must be positive")
    return (dec(value) / step).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN) * step


def number(value) -> str:
    return format(dec(value), "f")


@dataclass(repr=False, frozen=True)
class Credentials:
    key: str
    secret: str
    passphrase: str

    @classmethod
    def load(cls, path: Path, demo: bool, config_path: Path | None = None) -> Credentials:
        if config_path is not None and config_path.exists():
            if config_path.is_symlink() or (
                os.name != "nt" and stat.S_IMODE(config_path.stat().st_mode) & 0o077
            ):
                raise ValueError("credentials config must be private (chmod 600), not a symlink")
            try:
                raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
            except (tomllib.TOMLDecodeError, UnicodeError):
                # TOML diagnostics may contain source text, including credential values.
                raise ValueError(
                    "invalid credentials TOML; check quoting and section syntax"
                ) from None
            section = raw.get("demo" if demo else "live", {})
            if not isinstance(section, dict) or set(section) - {
                "api_key",
                "api_secret",
                "passphrase",
            }:
                raise ValueError("invalid credentials section; use api_key, api_secret, passphrase")
            values = [section.get(key, "") for key in ("api_key", "api_secret", "passphrase")]
            if not all(isinstance(value, str) for value in values):
                raise ValueError("credential values must be quoted strings")
            if any(values):
                if not all(value.strip() for value in values):
                    raise ValueError("credentials config is incomplete; fill all three fields")
                return cls(*values)
        prefix = "OKX_DEMO_" if demo else "OKX_"
        values = [
            os.environ.get(prefix + key) for key in ("API_KEY", "API_SECRET", "API_PASSPHRASE")
        ]
        if any(values):
            if not all(values):
                raise ValueError(f"set all three {prefix}API_* environment variables")
            return cls(*values)
        if not path.exists():
            raise ValueError("credentials missing; fill config/okx.credentials.toml locally")
        if path.is_symlink() or (os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077):
            raise ValueError("credentials file must be private (chmod 600), not a symlink")
        raw = json.loads(path.read_text())
        if raw.get("demo") is not demo:
            raise ValueError("credentials environment does not match demo/live")
        values = [raw.get(k, "") for k in ("key", "secret", "passphrase")]
        if not all(isinstance(v, str) and v.strip() for v in values):
            raise ValueError("credentials file is incomplete")
        return cls(*values)


def signature(secret: str, timestamp: str, method: str, path: str, body: str) -> str:
    message = timestamp + method.upper() + path + body
    return base64.b64encode(
        hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
    ).decode()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward authentication headers to a redirect destination.
        return None


class OKXClient:
    def __init__(
        self,
        *,
        base_url="https://openapi.okx.com",
        demo=False,
        credentials: Credentials | None = None,
        write_enabled=False,
        timeout=15,
        opener=None,
    ):
        self.base_url = base_url.rstrip("/")
        if self.base_url not in HOSTS:
            raise ValueError("use an approved official OKX HTTPS API host")
        self.demo = demo
        self.credentials = credentials
        self.write_enabled = write_enabled
        self.timeout = timeout
        self.offset = 0.0
        self.opener = opener or urllib.request.build_opener(_NoRedirect())

    def now(self) -> float:
        return time.time() + self.offset

    def sync_time(self) -> None:
        before = time.time()
        server = float(self.get("/api/v5/public/time", private=False)[0]["ts"]) / 1000
        self.offset = server - (before + time.time()) / 2

    def get(self, path, params=None, *, private=True):
        return self.request("GET", path, params=params, private=private)

    def post(self, path, body):
        return self.request("POST", path, body=body, private=True)

    def request(self, method, path, *, params=None, body=None, private=True):
        if not path.startswith("/api/v5/"):
            raise ValueError("invalid API path")
        if method != "GET" and not self.write_enabled:
            raise ValueError("client is read-only")
        if params:
            path += "?" + urllib.parse.urlencode(params)
        encoded = (
            json.dumps(body, separators=(",", ":"), allow_nan=False) if body is not None else ""
        )
        for attempt in range(3 if method == "GET" else 1):
            headers = {"Content-Type": "application/json", "User-Agent": "trend-pyramiding/0.1"}
            if self.demo:
                headers["x-simulated-trading"] = "1"
            if private:
                if self.credentials is None:
                    raise ValueError("private request requires credentials")
                timestamp = (
                    datetime.fromtimestamp(self.now(), timezone.utc)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z")
                )
                headers.update(
                    {
                        "OK-ACCESS-KEY": self.credentials.key,
                        "OK-ACCESS-PASSPHRASE": self.credentials.passphrase,
                        "OK-ACCESS-TIMESTAMP": timestamp,
                        "OK-ACCESS-SIGN": signature(
                            self.credentials.secret, timestamp, method, path, encoded
                        ),
                    }
                )
            if method == "POST":
                headers["expTime"] = str(int((self.now() + 10) * 1000))
            request = urllib.request.Request(
                self.base_url + path,
                data=encoded.encode() if encoded else None,
                headers=headers,
                method=method,
            )
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    raw = json.load(response)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                if method != "GET":
                    raise UncertainWrite(
                        f"uncertain {method} {path}; reconcile before continuing"
                    ) from None
                if attempt == 2:
                    raise OKXError("transport", path) from None
                time.sleep(0.5 * (attempt + 1))
                continue
            if not isinstance(raw, dict) or "code" not in raw:
                if method != "GET":
                    raise UncertainWrite(f"invalid response to {method} {path}")
                raise OKXError("invalid_response", path)
            if str(raw["code"]) != "0":
                if method != "GET" and str(raw["code"]) in {"50004", "50001", "50013"}:
                    raise UncertainWrite(
                        f"OKX response leaves {method} {path} uncertain (code={raw['code']})"
                    )
                raise OKXError(str(raw["code"]), path)
            rows = raw.get("data")
            if not isinstance(rows, list):
                if method != "GET":
                    raise UncertainWrite(f"missing response data for {path}")
                raise OKXError("invalid_data", path)
            for row in rows:
                if isinstance(row, dict) and str(row.get("sCode", "0")) != "0":
                    if method != "GET" and str(row["sCode"]) in {"50004", "50001", "50013"}:
                        raise UncertainWrite(f"OKX item response leaves {path} uncertain")
                    raise OKXError(str(row["sCode"]), path)
            return rows
        raise AssertionError("unreachable")


@dataclass(frozen=True)
class Instrument:
    inst_id: str
    contract_value: Decimal
    lot: Decimal
    minimum: Decimal
    tick: Decimal
    max_size: Decimal
    fee_group: str = ""

    @classmethod
    def parse(cls, row: dict) -> Instrument:
        base = row["instId"].split("-")[0]
        if (
            row.get("instType") != "SWAP"
            or row.get("settleCcy") != "USDT"
            or row.get("ctType") != "linear"
            or row.get("state") != "live"
            or row.get("ctValCcy") != base
            or row.get("instCategory") != "1"
            or row.get("ruleType") != "normal"
        ):
            raise ValueError("only normal live crypto linear USDT swaps are supported")
        values = [
            dec(row["ctVal"]) * dec(row.get("ctMult") or "1"),
            dec(row["lotSz"]),
            dec(row["minSz"]),
            dec(row["tickSz"]),
            min(dec(row.get(key) or "0") for key in ("maxLmtSz", "maxMktSz", "maxStopSz")),
        ]
        if any(v <= 0 for v in values):
            raise ValueError("invalid contract specifications")
        return cls(row["instId"], *values, row.get("groupId", ""))


def universe(client: OKXClient, count: int, requested: tuple[str, ...] = ()) -> list[Instrument]:
    instruments = {}
    for row in client.get("/api/v5/public/instruments", {"instType": "SWAP"}, private=False):
        try:
            item = Instrument.parse(row)
            instruments[item.inst_id] = item
        except (ValueError, KeyError):
            continue
    if requested:
        if not all(k in instruments for k in requested):
            raise ValueError("a configured instrument is unavailable or unsupported")
        return [instruments[k] for k in requested]
    ranked = []
    for row in client.get("/api/v5/market/tickers", {"instType": "SWAP"}, private=False):
        if row["instId"] in instruments:
            turnover = dec(row.get("volCcy24h") or "0") * dec(row.get("last") or "0")
            if turnover > 0:
                ranked.append((turnover, row["instId"]))
    ranked.sort(reverse=True)
    if len(ranked) < count:
        raise ValueError("not enough liquid swap instruments")
    return [instruments[key] for _, key in ranked[:count]]
