from fastapi import FastAPI
from pydantic import BaseModel

from app import __version__

app = FastAPI(title="API", version=__version__)


class Message(BaseModel):
    message: str


@app.get("/")
def root() -> Message:
    return Message(message="Hello")


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)


if __name__ == "__main__":
    run()
