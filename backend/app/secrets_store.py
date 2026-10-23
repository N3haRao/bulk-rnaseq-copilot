"""Where the Anthropic API key entered on the Settings page is kept.

Order of use: ANTHROPIC_API_KEY in the environment (or .env) if set; otherwise
the key saved from the website, which lives in the operating system's keychain
(macOS Keychain, Windows Credential Manager, a Linux Secret Service) or, where no
keychain exists, in a file only the current user can read.

The key is never sent back to the browser (only a masked form), never written
to logs, and never put into os.environ, so the chat assistant's own code (which
runs with a filtered environment) cannot read it.
"""

import os
import stat
from pathlib import Path

from .config import settings

SERVICE = "agentic-omics-testbed"
ACCOUNT = "anthropic_api_key"


def _keyring():
    """The OS keychain, or None when this machine has none."""
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return None
    kr = keyring.get_keyring()
    return None if isinstance(kr, fail.Keyring) else keyring


def _file() -> Path:
    return settings.data_dir / "secrets" / "anthropic_api_key"


def get_key() -> tuple[str | None, str | None]:
    """(key, where it came from: 'environment' | 'keychain' | 'file'), or (None, None)."""
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env:
        return env, "environment"
    kr = _keyring()
    if kr is not None:
        try:
            k = kr.get_password(SERVICE, ACCOUNT)
            if k:
                return k, "keychain"
        except Exception:  # a locked or broken keychain must not break the site
            pass
    f = _file()
    if f.exists():
        k = f.read_text().strip()
        if k:
            return k, "file"
    return None, None


def save_key(key: str) -> str:
    """Store the key; returns where ('keychain' or 'file')."""
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE, ACCOUNT, key)
            _file().unlink(missing_ok=True)
            return "keychain"
        except Exception:
            pass
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(f.parent, stat.S_IRWXU)  # 700: only this user can list the folder
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)  # 600 from the start
    with os.fdopen(fd, "w") as fh:
        fh.write(key)
    return "file"


def delete_key() -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(SERVICE, ACCOUNT)
        except Exception:
            pass
    _file().unlink(missing_ok=True)


def masked(key: str | None) -> str | None:
    if not key:
        return None
    return f"{key[:7]}…{key[-4:]}" if len(key) > 14 else "…"


def looks_like_key(key: str) -> bool:
    return key.startswith("sk-ant-") and 20 <= len(key) <= 300 and key.isascii() and not any(c.isspace() for c in key)
