# bachelor-thesis-project

A FastAPI application managed with [uv](https://docs.astral.sh/uv/), type-checked
with [pyrefly](https://pyrefly.org/) and linted/formatted with [ruff](https://docs.astral.sh/ruff/).

## Prerequisites

Install uv (see the [uv install docs](https://docs.astral.sh/uv/getting-started/installation/)):

```sh
pip install uv
```

## Setup

Install all dependencies (creates `.venv` and resolves from `uv.lock`):

```sh
uv sync
```

`uv run <cmd>` automatically uses this environment, so the virtual environment
never has to be activated manually.

## Run the app

```sh
# dev server with autoreload on http://127.0.0.1:8000
uv run fastapi dev app/main.py

# or with uvicorn directly
uv run uvicorn app.main:app --reload

# or as a module
uv run python -m app.main
```

## Lint and format (ruff)

```sh
uv run ruff check .
uv run ruff check --fix .
uv run ruff format .
uv run ruff format --check .
```

## Type-check (pyrefly)

```sh
uv run pyrefly check
```

## Tests (pytest)

```sh
uv run pytest
```
