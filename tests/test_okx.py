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


def test_credentials_private_permissions_and_demo_separation(tmp_path, monkeypatch):
    for suffix in ("API_KEY", "API_SECRET", "API_PASSPHRASE"):
        monkeypatch.delenv("OKX_" + suffix, raising=False)
        monkeypatch.delenv("OKX_DEMO_" + suffix, raising=False)
    path = tmp_path / "secret.json"
    path.write_text(json.dumps({"demo": False, "key": "a", "secret": "b", "passphrase": "c"}))
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        Credentials.load(path, False)
    path.chmod(0o600)
    assert Credentials.load(path, False).key == "a"
    with pytest.raises(ValueError, match="environment"):
        Credentials.load(path, True)
    assert "passphrase" not in repr(Credentials.load(path, False))


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
        Instrument.parse(instrument_row("SNDK", instCategory="3"))
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
