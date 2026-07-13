"""Shared FastAPI assembly for the pod's role-specific apps."""

from typing import Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel

from ldp_common.apidocs import SECURITY_SCHEMES
from ldp_common.config import CorsSettings, Settings


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


def add_health(app: FastAPI, version: str) -> None:
    @app.get(
        "/health",
        tags=["health"],
        operation_id="healthCheck",
        summary="Liveness probe",
        description="Unauthenticated readiness/liveness check reporting the server version.",
    )
    def health() -> HealthResponse:
        return HealthResponse(status="ok", version=version)


_CORS_METHODS = ["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"]
_CORS_ALLOW_HEADERS = [
    "Authorization",
    "Content-Type",
    "Accept",
    "If-Match",
    "If-None-Match",
    "Prefer",
    "Slug",
]
_CORS_EXPOSE_HEADERS = [
    "ETag",
    "Location",
    "Link",
    "Allow",
    "Accept-Post",
    "Preference-Applied",
    "WWW-Authenticate",
]


def add_cors(app: FastAPI, cors: CorsSettings) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors.allow_origins,
        allow_credentials=False,
        allow_methods=_CORS_METHODS,
        allow_headers=_CORS_ALLOW_HEADERS,
        expose_headers=_CORS_EXPOSE_HEADERS,
        max_age=600,
    )


def install_openapi_security(app: FastAPI) -> None:

    def openapi_with_security() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            tags=app.openapi_tags,
        )
        schema.setdefault("components", {})["securitySchemes"] = SECURITY_SCHEMES
        for path_item in schema["paths"].values():
            for operation in path_item.values():
                content = operation.get("requestBody", {}).get("content", {})
                json_schema = content.get("application/json", {}).get("schema", {})
                is_bytes_placeholder = (
                    json_schema.get("format") == "binary"
                    or json_schema.get("contentMediaType") == "application/octet-stream"
                )
                if len(content) > 1 and is_bytes_placeholder:
                    del content["application/json"]
        app.openapi_schema = schema
        return schema

    app.openapi = openapi_with_security  # type: ignore[method-assign]


def run_uvicorn(import_string: str, settings: Settings) -> None:
    import uvicorn

    if settings.tls_mode == "required" and (
        settings.ssl_keyfile is None or settings.ssl_certfile is None
    ):
        raise RuntimeError(
            "tls_mode='required' needs LDP_SSL_KEYFILE and LDP_SSL_CERTFILE so uvicorn "
            "can terminate TLS; set both, or use tls_mode='terminated' behind a "
            "TLS-terminating reverse proxy."
        )
    uvicorn.run(
        import_string,
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
        ssl_keyfile=settings.ssl_keyfile,
        ssl_certfile=settings.ssl_certfile,
    )
