"""Application configuration for the LDP Personal Store."""

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

    # TLS key and certificate for tls_mode="required". The canonical
    # `python -m ldp_personal_store.main` launch path passes both to uvicorn and refuses to
    # start without them, so a "required" pod can never silently serve plaintext; a direct
    # uvicorn launch may supply them as --ssl-keyfile/--ssl-certfile instead.
    ssl_keyfile: Path | None = None
    ssl_certfile: Path | None = None

    # The pod owner's admin credential (PLAINTEXT). Required for the storage and bundled
    # roles — those lifespans refuse to boot without it (see require_admin_token), so a pod
    # never comes up with an absent or default admin token. The standalone view-engine role
    # does not use it (it validates the *presented* request token against storage), so it is
    # optional at this shared level and enforced per role instead. Only its SHA-256 hash is
    # persisted — the plaintext is never written to disk or the log. The owner chooses the
    # value, e.g. the output of `openssl rand -base64 32`.
    admin_token: str | None = None

    # Optional PLAINTEXT engine token: the credential the view engine presents on the
    # engine->storage boundary. Only its SHA-256 hash is persisted. Left unset, the
    # bundled deployment issues a fresh engine token on every startup and keeps the
    # plaintext in process memory only; set it explicitly when the engine runs as a
    # separate process against a remote storage server.
    engine_token: str | None = None

    # Base URL of the upstream storage server the view engine talks to. Left unset
    # (the bundled deployment), the engine reaches storage through an in-process
    # ASGI transport — the same HTTP surface, no network socket. Set it to run the
    # engine against a storage server listening elsewhere (loopback or remote).
    storage_url: str | None = None

    # The named graph that holds the engine's operating state (token/view/policy records
    # and the access log), kept out of view-CONSTRUCT scope. The engine names it in a
    # standard SPARQL FROM clause to reach that state on any store; this reference server
    # realizes it locally as its reserved `.system/` subtree. Not derived from base_uri —
    # it is a stable logical name the engine and store agree on.
    state_graph: str = "urn:ldp:engine-state"

    # The data source the engine queries for view CONSTRUCTs and binary reads, as opposed
    # to the state store (storage_url) that holds the engine's own records. Left unset it
    # defaults to storage_url — the co-located default where one server holds both. Set it
    # to point the engine at a separate SPARQL/LDP data source (e.g. a third-party store).
    data_source_url: str | None = None

    # The namespace the data-source resources carry: what the engine treats as an
    # "upstream" URI to rewrite into a gated proxy URL and to guard the blob endpoint
    # against. Independent of base_uri (the engine's own public base). Left unset it
    # defaults to base_uri — correct when the data source is the co-located pod.
    data_source_base_uri: str | None = None

    # Credential the engine presents to the data source. Left unset it defaults to
    # engine_token (co-located: same credential as the state store).
    data_source_token: str | None = None

    # Auth scheme the engine uses against the data source: a bearer token, HTTP Basic
    # (data_source_token as "user:password"), or no credential at all.
    data_source_auth: Literal["bearer", "basic", "none"] = "bearer"

    @field_validator("base_uri")
    @classmethod
    def _ensure_trailing_slash(cls, v: str) -> str:
        return v if v.endswith("/") else v + "/"

    @field_validator("data_source_base_uri")
    @classmethod
    def _ensure_optional_trailing_slash(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v if v.endswith("/") else v + "/"

    @property
    def effective_data_source_url(self) -> str | None:
        """The data-source URL, defaulting to the state store (co-located)."""
        return self.data_source_url if self.data_source_url is not None else self.storage_url

    @property
    def effective_data_source_base_uri(self) -> str:
        """The namespace data-source resources carry, defaulting to the engine base."""
        return self.data_source_base_uri if self.data_source_base_uri is not None else self.base_uri

    @property
    def effective_data_source_token(self) -> str | None:
        """The data-source credential, defaulting to the engine token (co-located)."""
        return self.data_source_token if self.data_source_token is not None else self.engine_token


@lru_cache
def get_settings() -> Settings:
    return Settings()


SettingsDep = Annotated[Settings, Depends(get_settings)]


class CorsSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LDP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    cors_allow_origins: str = "*"

    @property
    def allow_origins(self) -> list[str]:
        raw = self.cors_allow_origins.strip()
        if raw == "*":
            return ["*"]
        return [origin.strip() for origin in raw.split(",") if origin.strip()]


@lru_cache
def get_cors_settings() -> CorsSettings:
    return CorsSettings()


def require_admin_token(settings: Settings) -> str:
    """The admin credential for the storage and bundled roles; refuse to boot without it.

    The engine role does not call this — it holds no admin token, authenticating owner
    requests by validating the presented bearer against storage instead.
    """
    if settings.admin_token is None:
        raise RuntimeError(
            "LDP_ADMIN_TOKEN is required: the storage and bundled roles refuse to start "
            "without it, so a pod never comes up with an absent or default admin token."
        )
    return settings.admin_token


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
