import os
from pathlib import Path

import pytest

from trend_pyramiding.local_credentials import load_local_credentials
from trend_pyramiding.okx import Credentials


@pytest.fixture
def local_config(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("OKX_", "RENDER")):
            monkeypatch.delenv(name)
    # Track mutations made by the loader so real process environment is restored after each test.
    for prefix in ("OKX_", "OKX_DEMO_"):
        for suffix in ("API_KEY", "API_SECRET", "API_PASSPHRASE"):
            monkeypatch.setenv(prefix + suffix, "")
    return tmp_path / "okx.toml"


def write_keys(config, text):
    path = config.with_name("okx.credentials.toml")
    path.write_text(text)
    return path


LIVE = '[live]\napi_key="saved-key"\napi_secret="saved-secret"\npassphrase="saved-pass"\n'
DEMO = '[demo]\napi_key="demo-key"\napi_secret="demo-secret"\npassphrase="demo-pass"\n'


def test_saved_keys_load_separately_and_file_becomes_private(local_config):
    path = write_keys(local_config, LIVE + DEMO)
    path.chmod(0o644)
    load_local_credentials(local_config)
    assert Credentials.load(False).key == "saved-key"
    assert Credentials.load(True).key == "demo-key"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text() == LIVE + DEMO


def test_blank_demo_and_missing_file_allow_panel_to_open(local_config):
    load_local_credentials(local_config)
    write_keys(local_config, LIVE + '[demo]\napi_key=""\napi_secret=""\npassphrase=""\n')
    load_local_credentials(local_config)
    assert Credentials.load(False).secret == "saved-secret"
    with pytest.raises(ValueError, match="OKX_DEMO_API_KEY"):
        Credentials.load(True)


def test_environment_profile_is_not_overwritten_or_mixed(local_config, monkeypatch):
    write_keys(local_config, LIVE + DEMO)
    monkeypatch.setenv("OKX_API_KEY", "environment-account")
    load_local_credentials(local_config)
    assert os.environ["OKX_API_KEY"] == "environment-account"
    assert os.environ["OKX_API_SECRET"] == ""
    with pytest.raises(ValueError, match="OKX_API_SECRET"):
        Credentials.load(False)
    assert Credentials.load(True).key == "demo-key"


@pytest.mark.parametrize("flag", ["RENDER", "RENDER_SERVICE_ID"])
def test_render_does_not_read_local_file(local_config, monkeypatch, flag):
    write_keys(local_config, LIVE)
    monkeypatch.setenv(flag, "true")
    monkeypatch.setattr(Path, "is_file", lambda self: pytest.fail("cloud must not inspect file"))
    load_local_credentials(local_config)
    with pytest.raises(ValueError, match="OKX_API_KEY"):
        Credentials.load(False)


@pytest.mark.parametrize(
    "text",
    [
        '[live]\napi_key="private-test-value"\nbroken-private-test-value',
        '[live]\napi_key="private-test-value"',
        "[live]\napi_key=42",
        'live="private-test-value"',
    ],
)
def test_invalid_file_errors_do_not_leak_values(local_config, text):
    write_keys(local_config, text)
    with pytest.raises(ValueError) as error:
        load_local_credentials(local_config)
    assert "private-test-value" not in str(error.value)
    assert os.environ["OKX_API_KEY"] == ""


def test_invalid_second_profile_does_not_partially_load_first(local_config):
    write_keys(local_config, LIVE + '[demo]\napi_key="incomplete"\n')
    with pytest.raises(ValueError):
        load_local_credentials(local_config)
    assert os.environ["OKX_API_KEY"] == ""


def test_command_line_credentials_remain_environment_only(local_config):
    write_keys(local_config, LIVE)
    with pytest.raises(ValueError, match="OKX_API_KEY"):
        Credentials.load(False)
