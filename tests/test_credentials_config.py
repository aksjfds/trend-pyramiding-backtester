import json

import pytest

from trend_pyramiding.okx import Credentials


@pytest.fixture
def files(tmp_path, monkeypatch):
    for prefix in ("OKX_", "OKX_DEMO_"):
        for name in ("API_KEY", "API_SECRET", "API_PASSPHRASE"):
            monkeypatch.delenv(prefix + name, raising=False)
    config = tmp_path / "okx.credentials.toml"
    config.touch(mode=0o600)
    return tmp_path / "legacy.json", config


def test_config_precedes_environment_and_separates_demo(files, monkeypatch):
    legacy, config = files
    config.write_text(
        '[live]\napi_key="live-key"\napi_secret="live-secret"\npassphrase="live-pass"\n'
        '[demo]\napi_key="demo-key"\napi_secret="demo-secret"\npassphrase="demo-pass"\n'
    )
    for name in ("API_KEY", "API_SECRET", "API_PASSPHRASE"):
        monkeypatch.setenv("OKX_" + name, "old-env-value")
    assert Credentials.load(legacy, False, config).key == "live-key"
    assert Credentials.load(legacy, True, config).key == "demo-key"


def test_partial_config_does_not_fall_back_to_another_account(files, monkeypatch):
    legacy, config = files
    config.write_text('[live]\napi_key="new-key"\n')
    for name in ("API_KEY", "API_SECRET", "API_PASSPHRASE"):
        monkeypatch.setenv("OKX_" + name, "old-env-value")
    with pytest.raises(ValueError, match="incomplete"):
        Credentials.load(legacy, False, config)


def test_blank_config_keeps_existing_credentials_working(files):
    legacy, config = files
    config.write_text('[live]\napi_key=""\napi_secret=""\npassphrase=""\n')
    legacy.write_text(json.dumps({"demo": False, "key": "old", "secret": "s", "passphrase": "p"}))
    legacy.chmod(0o600)
    assert Credentials.load(legacy, False, config).key == "old"


def test_live_config_never_supplies_demo_credentials(files):
    legacy, config = files
    config.write_text('[live]\napi_key="live"\napi_secret="secret"\npassphrase="pass"\n')
    with pytest.raises(ValueError, match="credentials missing"):
        Credentials.load(legacy, True, config)


@pytest.mark.parametrize(
    "contents",
    [
        "[live]\napi_secret = sensitive-unquoted-secret",
        '[live]\napi_key="x"\napi_key="sensitive-duplicate-secret"',
    ],
)
def test_parse_errors_do_not_echo_secret_source(files, contents):
    legacy, config = files
    config.write_text(contents)
    with pytest.raises(ValueError) as exc:
        Credentials.load(legacy, False, config)
    assert "sensitive" not in str(exc.value)
    assert "invalid credentials TOML" in str(exc.value)


def test_config_requires_private_permissions(files):
    legacy, config = files
    config.chmod(0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        Credentials.load(legacy, False, config)
