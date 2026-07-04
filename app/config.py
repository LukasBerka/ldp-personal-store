"""Application configuration for the Personal LDP Pod.

Defines the :class:`Settings` model (loaded from environment / ``.env`` with the
``LDP_`` prefix), the cached :func:`get_settings` singleton, a FastAPI dependency
alias, and the boot-time TLS precondition check.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LDP_",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # All LDP resource URIs are relative to this; must end with "/"
    base_uri: str = "http://localhost:8000/"

    # Filesystem root for pod data; created on first write
    storage_root: Path = Path("./data")

    host: str = "127.0.0.1"
    port: int = 8000

    # Dev-only autoreload file-watcher; off by default so the canonical run command
    # is production-safe.
    reload: bool = False

    # TLS enforcement policy (see check_tls_precondition below)
    # "off"        — no TLS; only safe when host is loopback
    # "required"   — uvicorn terminates TLS (ssl_keyfile + ssl_certfile must be set at launch)
    # "terminated" — a trusted reverse proxy terminates TLS upstream (trust the deployment)
    tls_mode: Literal["off", "required", "terminated"] = "off"

    # Optional PLAINTEXT admin token used to deterministically seed the bootstrap
    # hash for automated deployments and tests. It is never persisted in plaintext —
    # only its SHA-256 hash is stored. Left unset, the bootstrap generates a random
    # admin token and logs it once instead.
    admin_token: str | None = None

    # Optional PLAINTEXT engine token: the credential the view engine presents on the
    # engine->storage boundary. Only its SHA-256 hash is persisted. Left unset, the
    # bundled deployment mints a fresh engine token on every startup and keeps the
    # plaintext in process memory only; set it explicitly when the engine runs as a
    # separate process against a remote storage server.
    engine_token: str | None = None

    # Base URL of the upstream storage server the view engine talks to. Left unset
    # (the bundled deployment), the engine reaches storage through an in-process
    # ASGI transport — the same HTTP surface, no network socket. Set it to run the
    # engine against a storage server listening elsewhere (loopback or remote).
    storage_url: str | None = None

    @field_validator("base_uri")
    @classmethod
    def _ensure_trailing_slash(cls, v: str) -> str:
        return v if v.endswith("/") else v + "/"


@lru_cache
def get_settings() -> Settings:
    return Settings()


SettingsDep = Annotated[Settings, Depends(get_settings)]


_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})


def check_tls_precondition(settings: Settings) -> None:
    """Raise RuntimeError if the server would serve plaintext on a non-loopback interface.

    Called once from the lifespan startup hook, before accepting requests.
    """
    if settings.tls_mode == "off" and settings.host not in _LOOPBACK_HOSTS:
        raise RuntimeError(
            f"TLS is required: tls_mode='off' but host='{settings.host}' is not loopback. "
            "Set LDP_TLS_MODE to 'required' (uvicorn-native TLS) or 'terminated' "
            "(reverse proxy upstream), or bind to 127.0.0.1 / ::1 / localhost."
        )
