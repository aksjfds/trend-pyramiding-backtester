"""Small OKX V5 REST adapter. Reads are retryable; writes are never retried."""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation

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


class TransientRead(OKXError):
    """A temporary read failure; never raised for a write."""


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
    def load(cls, demo: bool) -> Credentials:
        """Read only the selected account's runtime environment variables."""
        prefix = "OKX_DEMO_" if demo else "OKX_"
        names = [prefix + key for key in ("API_KEY", "API_SECRET", "API_PASSPHRASE")]
        values = [os.environ.get(name, "") for name in names]
        missing = [name for name, value in zip(names, values) if not value.strip()]
        if missing:
            raise ValueError("missing OKX environment variables: " + ", ".join(missing))
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
        self.write_attempts = 0
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
        if method != "GET":
            self.write_attempts += 1
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
            except urllib.error.HTTPError as exc:
                status = exc.code
                exc.close()
                if method != "GET":
                    raise UncertainWrite(f"uncertain {method} {path} (HTTP {status})") from None
                if status not in {408, 429, 500, 502, 503, 504}:
                    raise OKXError(f"http_{status}", path) from None
                if attempt == 2:
                    raise TransientRead(f"http_{status}", path) from None
                time.sleep(0.5 * (attempt + 1))
                continue
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                ValueError,
                http.client.HTTPException,
            ):
                if method != "GET":
                    raise UncertainWrite(
                        f"uncertain {method} {path}; reconcile before continuing"
                    ) from None
                if attempt == 2:
                    raise TransientRead("transport", path) from None
                time.sleep(0.5 * (attempt + 1))
                continue
            if not isinstance(raw, dict) or "code" not in raw:
                if method != "GET":
                    raise UncertainWrite(f"invalid response to {method} {path}")
                raise OKXError("invalid_response", path)
            if str(raw["code"]) != "0":
                if method == "GET" and private and str(raw["code"]) == "50102" and attempt < 2:
                    self.sync_time()
                    continue
                if method == "GET" and str(raw["code"]) in {
                    "50001",
                    "50004",
                    "50011",
                    "50013",
                    "50040",
                }:
                    if attempt == 2:
                        raise TransientRead(str(raw["code"]), path)
                    time.sleep(0.5 * (attempt + 1))
                    continue
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
    category: str = "1"

    @classmethod
    def parse(cls, row: dict) -> Instrument:
        base = row["instId"].split("-")[0]
        if (
            row.get("instType") != "SWAP"
            or row.get("settleCcy") != "USDT"
            or row.get("ctType") != "linear"
            or row.get("state") != "live"
            or row.get("ctValCcy") != base
            or row.get("instCategory") not in {"1", "3", "4"}
            or row.get("ruleType") != "normal"
        ):
            raise ValueError(
                "only normal live crypto, stock or metal linear USDT swaps are supported"
            )
        values = [
            dec(row["ctVal"]) * dec(row.get("ctMult") or "1"),
            dec(row["lotSz"]),
            dec(row["minSz"]),
            dec(row["tickSz"]),
            min(dec(row.get(key) or "0") for key in ("maxLmtSz", "maxMktSz", "maxStopSz")),
        ]
        if any(v <= 0 for v in values):
            raise ValueError("invalid contract specifications")
        return cls(row["instId"], *values, row.get("groupId", ""), row["instCategory"])


def supported_instruments(client: OKXClient) -> list[Instrument]:
    """Return every currently supported live linear USDT swap."""
    instruments = {}
    for row in client.get("/api/v5/public/instruments", {"instType": "SWAP"}, private=False):
        try:
            item = Instrument.parse(row)
            instruments[item.inst_id] = item
        except (ValueError, KeyError):
            continue
    return [instruments[key] for key in sorted(instruments)]


def universe(client: OKXClient, count: int, requested: tuple[str, ...] = ()) -> list[Instrument]:
    instruments = {item.inst_id: item for item in supported_instruments(client)}
    if requested:
        if not all(k in instruments for k in requested):
            raise ValueError("a configured instrument is unavailable or unsupported")
        return [instruments[k] for k in requested]
    ranked = []
    for row in client.get("/api/v5/market/tickers", {"instType": "SWAP"}, private=False):
        if row["instId"] in instruments and instruments[row["instId"]].category == "1":
            turnover = dec(row.get("volCcy24h") or "0") * dec(row.get("last") or "0")
            if turnover > 0:
                ranked.append((turnover, row["instId"]))
    ranked.sort(reverse=True)
    if len(ranked) < count:
        raise ValueError("not enough liquid swap instruments")
    return [instruments[key] for _, key in ranked[:count]]
