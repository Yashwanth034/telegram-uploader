from __future__ import annotations

from typing import Protocol

SERVICE_NAME = "telegram-uploader"

# Only use OS-backed keyrings. Explicitly avoid plaintext/fallback keyring
# implementations even if a user has installed them globally.
_SECURE_BACKEND_PREFIXES = (
    "keyring.backends.secretservice.",
    "keyring.backends.macos.",
    "keyring.backends.windows.",
    "keyring.backends.kwallet.",
)


class _KeyringBackend(Protocol):
    priority: float

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


def _backend_identifier(backend: object) -> str:
    cls = backend.__class__
    return f"{cls.__module__}.{cls.__name__}".lower()


def secure_backend() -> _KeyringBackend | None:
    """Return an OS-backed keyring backend, never a plaintext fallback."""
    try:
        from keyring.backend import get_all_keyring
    except Exception:
        return None

    try:
        backends = list(get_all_keyring())
    except Exception:
        return None

    secure = [
        backend
        for backend in backends
        if _backend_identifier(backend).startswith(_SECURE_BACKEND_PREFIXES)
        and float(getattr(backend, "priority", 0) or 0) > 0
    ]
    if not secure:
        return None
    return max(secure, key=lambda backend: float(getattr(backend, "priority", 0) or 0))


def get_secret(name: str) -> str | None:
    backend = secure_backend()
    if backend is None:
        return None
    try:
        value = backend.get_password(SERVICE_NAME, name)
    except Exception:
        return None
    return value if value else None


def set_secret(name: str, value: str) -> bool:
    backend = secure_backend()
    if backend is None:
        return False
    try:
        backend.set_password(SERVICE_NAME, name, value)
    except Exception:
        return False
    return True


def delete_secret(name: str) -> None:
    backend = secure_backend()
    if backend is None:
        return
    try:
        backend.delete_password(SERVICE_NAME, name)
    except Exception:
        pass
