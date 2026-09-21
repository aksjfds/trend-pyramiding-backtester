import pytest

from trend_pyramiding.okx import Credentials

SUFFIXES = ("API_KEY", "API_SECRET", "API_PASSPHRASE")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for prefix in ("OKX_", "OKX_DEMO_"):
        for suffix in SUFFIXES:
            monkeypatch.delenv(prefix + suffix, raising=False)


def set_credentials(monkeypatch, prefix, values):
    for suffix, value in zip(SUFFIXES, values):
        monkeypatch.setenv(prefix + suffix, value)


def test_live_and_demo_read_only_their_own_environment(monkeypatch):
    live = ("live-key", "live-secret", "live-pass")
    demo = ("demo-key", "demo-secret", "demo-pass")
    set_credentials(monkeypatch, "OKX_", live)
    set_credentials(monkeypatch, "OKX_DEMO_", demo)
    assert Credentials.load(False) == Credentials(*live)
    assert Credentials.load(True) == Credentials(*demo)
    for value in live:
        assert value not in repr(Credentials.load(False))


@pytest.mark.parametrize("demo", [False, True])
@pytest.mark.parametrize("suffix", SUFFIXES)
@pytest.mark.parametrize("invalid", [None, "", " \t\n"])
def test_missing_or_blank_fields_fail_without_fallback_or_secret_output(
    monkeypatch, demo, suffix, invalid
):
    prefix = "OKX_DEMO_" if demo else "OKX_"
    for other in ("OKX_", "OKX_DEMO_"):
        set_credentials(monkeypatch, other, ("sensitive-key", "sensitive-secret", "sensitive-pass"))
    if invalid is None:
        monkeypatch.delenv(prefix + suffix)
    else:
        monkeypatch.setenv(prefix + suffix, invalid)
    with pytest.raises(ValueError) as exc:
        Credentials.load(demo)
    assert prefix + suffix in str(exc.value)
    assert "sensitive" not in str(exc.value)


@pytest.mark.parametrize("demo", [False, True])
def test_absent_environment_lists_required_names(demo):
    prefix = "OKX_DEMO_" if demo else "OKX_"
    with pytest.raises(ValueError) as exc:
        Credentials.load(demo)
    for suffix in SUFFIXES:
        assert prefix + suffix in str(exc.value)
