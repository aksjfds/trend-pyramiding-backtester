import json
import shutil
import sys
from pathlib import Path

import pytest

from trend_pyramiding import okx_cli


@pytest.fixture
def config(tmp_path):
    source = Path(__file__).resolve().parents[1] / "config"
    shutil.copytree(
        source, tmp_path / "config", ignore=shutil.ignore_patterns("okx.credentials.toml")
    )
    return tmp_path / "config/okx.toml"


@pytest.mark.parametrize("flags", [[], ["--demo", "--live"]])
def test_live_gate_fails_before_credentials_or_network(config, monkeypatch, flags):
    monkeypatch.setattr(sys, "argv", ["pyramid-okx", "run", "--config", str(config), *flags])
    monkeypatch.setattr(
        okx_cli.Credentials, "load", lambda *args: pytest.fail("must not read keys")
    )
    with pytest.raises(SystemExit) as exc:
        okx_cli.main()
    assert exc.value.code == 1


def test_check_constructs_read_only_client_and_never_sets_leverage(config, monkeypatch, capsys):
    class ReadOnly:
        def __init__(self, **kwargs):
            assert kwargs["write_enabled"] is False

        def sync_time(self):
            pass

        def get(self, *args, **kwargs):
            return []

        def post(self, *args):
            pytest.fail("check must not submit any private write")

    monkeypatch.setattr(sys, "argv", ["pyramid-okx", "check", "--config", str(config)])
    monkeypatch.setattr(okx_cli, "OKXClient", ReadOnly)
    monkeypatch.setattr(okx_cli.Credentials, "load", lambda *args: object())
    monkeypatch.setattr(okx_cli, "universe", lambda *args: [])
    monkeypatch.setattr(
        okx_cli,
        "account_snapshot",
        lambda client: {
            "equity": 1000,
            "equity_usd": 1000,
            "available_usdt": 900,
            "trade_permission": True,
        },
    )
    okx_cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["orders_enabled"] is False
    assert result["capital_limit_usdt_approx"] == 200


def test_status_does_not_load_credentials(config, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pyramid-okx", "status", "--config", str(config)])
    monkeypatch.setattr(
        okx_cli.Credentials, "load", lambda *args: pytest.fail("must not read keys")
    )
    okx_cli.main()
    assert "has not been initialized" in capsys.readouterr().out


def test_env_configuration_and_persistent_state_directory(config, tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "volume/state"
    state_dir.mkdir(parents=True)
    (state_dir / "okx-live.json").write_text(
        json.dumps({"version": 1, "capital_ceiling": 200, "pending": None, "markets": {}})
    )
    monkeypatch.setenv("PYRAMID_CONFIG", str(config))
    monkeypatch.setenv("PYRAMID_STATE_DIR", str(state_dir))
    monkeypatch.setattr(sys, "argv", ["pyramid-okx", "status"])
    okx_cli.main()
    assert json.loads(capsys.readouterr().out)["capital_ceiling"] == 200
