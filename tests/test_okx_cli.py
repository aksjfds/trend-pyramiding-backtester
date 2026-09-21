import json
import shutil
import sys
from pathlib import Path

import pytest

from trend_pyramiding import okx_cli


@pytest.fixture
def config(tmp_path):
    source = Path(__file__).resolve().parents[1] / "config"
    (tmp_path / "config").mkdir()
    for name in ("okx.toml", "default.toml"):
        shutil.copy2(source / name, tmp_path / "config" / name)
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
        lambda client, **kwargs: {
            "equity": 1000,
            "equity_usd": 1000,
            "available_usdt": 900,
            "trade_permission": True,
        },
    )
    okx_cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["orders_enabled"] is False
    import tomllib

    assert (
        result["capital_limit_usdt_approx"]
        == 1000 * tomllib.loads(config.read_text())["capital_fraction"]
    )


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


@pytest.mark.parametrize("demo", [False, True])
def test_check_uses_environment_credentials(config, monkeypatch, capsys, demo):
    from trend_pyramiding.okx import Credentials

    for prefix in ("OKX_", "OKX_DEMO_"):
        for suffix in ("API_KEY", "API_SECRET", "API_PASSPHRASE"):
            monkeypatch.setenv(prefix + suffix, prefix + suffix)
    prefix = "OKX_DEMO_" if demo else "OKX_"

    class ReadOnly:
        def __init__(self, **kwargs):
            assert kwargs["credentials"] == Credentials(
                prefix + "API_KEY", prefix + "API_SECRET", prefix + "API_PASSPHRASE"
            )
            assert kwargs["demo"] is demo
            assert kwargs["write_enabled"] is False

        def sync_time(self):
            pass

    monkeypatch.setattr(
        sys,
        "argv",
        ["pyramid-okx", "check", "--config", str(config), *(["--demo"] if demo else [])],
    )
    monkeypatch.setattr(okx_cli, "OKXClient", ReadOnly)
    monkeypatch.setattr(okx_cli, "check_account", lambda *args: {"authenticated": True})
    okx_cli.main()
    assert json.loads(capsys.readouterr().out)["authenticated"] is True


def test_missing_environment_never_reads_old_files_or_connects(config, monkeypatch, capsys):
    for suffix in ("API_KEY", "API_SECRET", "API_PASSPHRASE"):
        monkeypatch.delenv("OKX_" + suffix, raising=False)
    # Even an old local setup with all old credential sources must require runtime variables.
    old = config.parent / "okx.credentials.toml"
    old.write_text('[live]\napi_key="old"\napi_secret="old"\npassphrase="old"\n')
    old.chmod(0o600)
    legacy = config.parent.parent / "secrets/okx-live.json"
    legacy.parent.mkdir()
    legacy.write_text(
        json.dumps({"demo": False, "key": "old", "secret": "old", "passphrase": "old"})
    )
    legacy.chmod(0o600)
    monkeypatch.setenv("OKX_CREDENTIALS_FILE", str(old))
    monkeypatch.setattr(sys, "argv", ["pyramid-okx", "check", "--config", str(config)])
    monkeypatch.setattr(okx_cli, "OKXClient", lambda **kwargs: pytest.fail("must not connect"))
    with pytest.raises(SystemExit) as exc:
        okx_cli.main()
    assert exc.value.code == 1
    assert "OKX_API_KEY" in capsys.readouterr().err


def test_removed_credentials_command_is_rejected(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["pyramid-okx", "credentials"])
    with pytest.raises(SystemExit) as exc:
        okx_cli.main()
    assert exc.value.code == 2


def test_cli_reads_web_settings_before_account_check(config, tmp_path, monkeypatch, capsys):
    from trend_pyramiding.live import LiveConfig, StateStore
    from trend_pyramiding.settings import save_settings

    live, strategy = LiveConfig.load(config)
    store = StateStore(tmp_path / "persist/okx-live.json")
    save_settings(
        live,
        strategy,
        store,
        {"live": {"capital_fraction": 0.25, "leverage": 5, "bar": "4H"}, "strategy": {}},
    )

    class ReadOnly:
        def __init__(self, **kwargs):
            assert not kwargs["write_enabled"]

        def sync_time(self):
            pass

    def check(client, cfg, actual_store):
        assert cfg.capital_fraction == 0.25 and cfg.leverage == 5 and cfg.bar == "4H"
        assert actual_store.path == store.path
        return {"settings_applied": True}

    monkeypatch.setattr(
        sys,
        "argv",
        ["pyramid-okx", "check", "--config", str(config), "--state-dir", str(store.path.parent)],
    )
    monkeypatch.setattr(okx_cli, "OKXClient", ReadOnly)
    monkeypatch.setattr(okx_cli.Credentials, "load", lambda *args: object())
    monkeypatch.setattr(okx_cli, "check_account", check)
    okx_cli.main()
    assert json.loads(capsys.readouterr().out)["settings_applied"]
