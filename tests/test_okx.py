import base64
import hashlib
import hmac
import io
import json
import urllib.error
from decimal import Decimal

import pytest

from trend_pyramiding.okx import (
    Credentials,
    Instrument,
    OKXClient,
    OKXError,
    UncertainWrite,
    rounded,
    universe,
)


class Opener:
    def __init__(self, result=None, error=None):
        self.result = result or {"code": "0", "data": []}
        self.error = error
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.result).encode())


def test_signs_exact_encoded_query_and_body_and_isolates_demo():
    opener = Opener()
    client = OKXClient(
        credentials=Credentials("key", "secret", "pass"),
        demo=True,
        write_enabled=True,
        opener=opener,
    )
    client.now = lambda: 1700000000.125
    client.get("/api/v5/trade/order", {"instId": "BTC-USDT-SWAP", "clOrdId": "tp123"})
    request = opener.requests[0]
    headers = {k.lower(): v for k, v in request.header_items()}
    path = "/api/v5/trade/order?instId=BTC-USDT-SWAP&clOrdId=tp123"
    message = headers["ok-access-timestamp"] + "GET" + path
    expected = base64.b64encode(
        hmac.new(b"secret", message.encode(), hashlib.sha256).digest()
    ).decode()
    assert headers["ok-access-sign"] == expected
    assert headers["x-simulated-trading"] == "1"
    client.post("/api/v5/trade/order", {"sz": "1", "px": "100"})
    request = opener.requests[-1]
    headers = {k.lower(): v for k, v in request.header_items()}
    message = headers["ok-access-timestamp"] + "POST/api/v5/trade/order" + request.data.decode()
    assert (
        headers["ok-access-sign"]
        == base64.b64encode(hmac.new(b"secret", message.encode(), hashlib.sha256).digest()).decode()
    )
    assert request.data == b'{"sz":"1","px":"100"}'
    assert headers["exptime"] == "1700000010125"


def test_read_only_client_blocks_writes_before_network():
    opener = Opener()
    client = OKXClient(opener=opener)
    with pytest.raises(ValueError, match="read-only"):
        client.post("/api/v5/trade/order", {})
    assert not opener.requests


@pytest.mark.parametrize(
    "result",
    [
        {"code": "50004", "data": []},
        {"code": "50013", "data": []},
        {"nonsense": True},
        {"code": "0"},
    ],
)
def test_ambiguous_write_never_retries(result):
    opener = Opener(result)
    client = OKXClient(
        credentials=Credentials("key", "secret", "pass"), write_enabled=True, opener=opener
    )
    with pytest.raises(UncertainWrite):
        client.post("/api/v5/trade/order", {"sz": "1"})
    assert len(opener.requests) == 1


def test_network_timeout_never_retries_or_exposes_credentials():
    opener = Opener(error=urllib.error.URLError("secret must not leak"))
    client = OKXClient(
        credentials=Credentials("key", "secret", "pass"), write_enabled=True, opener=opener
    )
    with pytest.raises(UncertainWrite) as exc:
        client.post("/api/v5/trade/order", {})
    assert "secret" not in str(exc.value)
    assert len(opener.requests) == 1


def test_individual_order_rejection_is_checked():
    client = OKXClient(
        credentials=Credentials("key", "secret", "pass"),
        write_enabled=True,
        opener=Opener({"code": "0", "data": [{"sCode": "51008"}]}),
    )
    with pytest.raises(OKXError) as exc:
        client.post("/api/v5/trade/order", {})
    assert exc.value.code == "51008"


def test_untrusted_endpoint_cannot_receive_credentials():
    with pytest.raises(ValueError, match="official"):
        OKXClient(base_url="https://okx.com.evil.invalid")


def instrument_row(name="BTC", **changes):
    return {
        "instId": f"{name}-USDT-SWAP",
        "instType": "SWAP",
        "settleCcy": "USDT",
        "ctType": "linear",
        "state": "live",
        "ctValCcy": name,
        "ctVal": "0.01",
        "ctMult": "1",
        "lotSz": "0.01",
        "minSz": "0.01",
        "tickSz": "0.1",
        "maxLmtSz": "100000",
        "maxMktSz": "100000",
        "maxStopSz": "100000",
        "instCategory": "1",
        "ruleType": "normal",
        **changes,
    }


def test_contract_conversion_and_rounding():
    instrument = Instrument.parse(instrument_row(ctMult="10"))
    assert instrument.contract_value == Decimal("0.1")
    assert rounded("1.239", "0.01") == Decimal("1.23")
    assert rounded("123.49", "0.5") == Decimal("123.0")
    with pytest.raises(ValueError):
        Instrument.parse(instrument_row(ctValCcy="USD"))
    with pytest.raises(ValueError):
        Instrument.parse(instrument_row("EUR", instCategory="5"))
    with pytest.raises(ValueError):
        Instrument.parse(instrument_row(ruleType="pre_market"))


def test_universe_ranks_by_base_volume_times_price_and_filters_contracts():
    class Market:
        def get(self, path, params=None, **kwargs):
            if path.endswith("instruments"):
                return [
                    instrument_row("BTC"),
                    instrument_row("ETH"),
                    instrument_row("BAD", ctType="inverse"),
                ]
            return [
                {"instId": "BTC-USDT-SWAP", "volCcy24h": "100", "last": "1000"},
                {"instId": "ETH-USDT-SWAP", "volCcy24h": "1000", "last": "10"},
                {"instId": "BAD-USDT-SWAP", "volCcy24h": "999999", "last": "1000"},
            ]

    assert universe(Market(), 1)[0].inst_id == "BTC-USDT-SWAP"


def test_live_public_request_omits_demo_and_secret_headers():
    opener = Opener()
    OKXClient(opener=opener).get("/api/v5/public/time", private=False)
    headers = {k.lower(): v for k, v in opener.requests[0].header_items()}
    assert "x-simulated-trading" not in headers
    assert "ok-access-key" not in headers


@pytest.mark.parametrize(
    "name,category,value,lot",
    [
        ("XAU", "4", "0.001", "1"),
        ("SNDK", "3", "1", "0.001"),
        ("CRCL", "3", "1", "0.1"),
    ],
)
def test_stock_and_metal_contracts_preserve_size_and_fee_group(name, category, value, lot):
    contract = Instrument.parse(
        instrument_row(name, instCategory=category, ctVal=value, lotSz=lot, minSz=lot, groupId="4")
    )
    assert contract.inst_id == name + "-USDT-SWAP"
    assert contract.category == category
    assert contract.contract_value == Decimal(value)
    assert contract.minimum == Decimal(lot)
    assert contract.fee_group == "4"


def test_tradfi_is_selectable_manually_without_changing_automatic_crypto_universe():
    class Market:
        def get(self, path, params=None, **kwargs):
            if path.endswith("instruments"):
                return [
                    instrument_row("BTC"),
                    instrument_row("XAU", instCategory="4"),
                    instrument_row("CRCL", instCategory="3"),
                ]
            return [
                {"instId": name + "-USDT-SWAP", "volCcy24h": vol, "last": "1"}
                for name, vol in [("BTC", "100"), ("XAU", "99999"), ("CRCL", "999999")]
            ]

    assert universe(Market(), 1)[0].inst_id == "BTC-USDT-SWAP"
    selected = universe(Market(), 5, ("XAU-USDT-SWAP", "CRCL-USDT-SWAP"))
    assert [i.inst_id for i in selected] == ["XAU-USDT-SWAP", "CRCL-USDT-SWAP"]


@pytest.mark.parametrize("code", ["50011", "50013", "50040"])
def test_read_rate_limits_are_transient_but_writes_are_never_retried(code, monkeypatch):
    from trend_pyramiding.okx import TransientRead

    monkeypatch.setattr("trend_pyramiding.okx.time.sleep", lambda _: None)
    opener = Opener({"code": code, "data": []})
    client = OKXClient(
        opener=opener, credentials=Credentials("key", "secret", "pass"), write_enabled=True
    )
    with pytest.raises(TransientRead):
        client.get("/api/v5/account/balance")
    assert len(opener.requests) == 3
    with pytest.raises((OKXError, UncertainWrite)):
        client.post("/api/v5/trade/order", {})
    assert len(opener.requests) == 4
    assert client.write_attempts == 1


@pytest.mark.parametrize("status,expected_attempts", [(429, 3), (503, 3), (401, 1), (403, 1)])
def test_http_read_failure_classification(status, expected_attempts, monkeypatch):
    from trend_pyramiding.okx import TransientRead

    monkeypatch.setattr("trend_pyramiding.okx.time.sleep", lambda _: None)
    opener = Opener(error=urllib.error.HTTPError("https://openapi.okx.com", status, "", {}, None))
    client = OKXClient(opener=opener)
    with pytest.raises(OKXError) as error:
        client.get("/api/v5/public/time", private=False)
    assert isinstance(error.value, TransientRead) == (expected_attempts == 3)
    assert len(opener.requests) == expected_attempts


def test_expired_read_timestamp_resyncs_but_does_not_replay_a_write():
    import time

    class ClockOpener(Opener):
        def open(self, request, timeout):
            self.requests.append(request)
            if request.full_url.endswith("/public/time"):
                result = {"code": "0", "data": [{"ts": str(int(time.time() * 1000))}]}
            elif len(self.requests) == 1:
                result = {"code": "50102", "data": []}
            else:
                result = {"code": "0", "data": []}
            return io.BytesIO(json.dumps(result).encode())

    opener = ClockOpener()
    client = OKXClient(opener=opener, credentials=Credentials("key", "secret", "pass"))
    assert client.get("/api/v5/account/balance") == []
    assert len(opener.requests) == 3
    assert opener.requests[1].full_url.endswith("/public/time")
    assert client.write_attempts == 0
