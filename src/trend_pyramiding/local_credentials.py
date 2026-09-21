"""Load saved credentials only for the loopback desktop control panel."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path


def load_local_credentials(config: Path) -> None:
    # Cloud and CLI entry points never call this loader. Guard Render as well.
    if os.environ.get("RENDER") or os.environ.get("RENDER_SERVICE_ID"):
        return
    profiles = {}
    for profile, prefix in (("live", "OKX_"), ("demo", "OKX_DEMO_")):
        names = [prefix + suffix for suffix in ("API_KEY", "API_SECRET", "API_PASSPHRASE")]
        # Keep a supplied environment as a whole; never mix two accounts' secrets.
        if not any(os.environ.get(name, "").strip() for name in names):
            profiles[profile] = names
    if not profiles:
        return
    path = config.resolve().with_name("okx.credentials.toml")
    if not path.is_file():
        return
    path.chmod(0o600)
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        raise ValueError("本机密钥文件格式错误，请检查 config/okx.credentials.toml") from None
    updates = {}
    for profile, names in profiles.items():
        section = data.get(profile, {})
        if not isinstance(section, dict):
            raise ValueError(f"本机密钥文件的 {profile} 配置格式错误")
        values = [section.get(key, "") for key in ("api_key", "api_secret", "passphrase")]
        if not all(isinstance(value, str) for value in values):
            raise ValueError(f"本机密钥文件的 {profile} 字段必须是文本")
        if not any(value.strip() for value in values):
            continue
        if not all(value.strip() for value in values):
            raise ValueError(f"请完整填写本机密钥文件中的 {profile} 三个字段")
        updates.update(zip(names, values))
    os.environ.update(updates)
