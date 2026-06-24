from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from app import __version__
from app.config import check_tls_precondition, get_settings
from app.vocab import make_system_ns


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    check_tls_precondition(settings)
    app.state.system_ns = make_system_ns(settings.base_uri)
    yield


app = FastAPI(title="Personal LDP Pod", version=__version__, lifespan=lifespan)


class Message(BaseModel):
    message: str


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


@app.get("/")
def root() -> Message:
    return Message(message="Hello")


@app.get("/health")
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=True)


if __name__ == "__main__":
    run()
