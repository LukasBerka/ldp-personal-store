from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ldp_personal_store.config import get_settings
from ldp_personal_store.main import app
from ldp_personal_store.storage.filesystem import FilesystemBackend
from tests.support import ADMIN_TOKEN, BASE


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    return tmp_path / "storage"


@pytest.fixture
def backend(storage_root: Path) -> FilesystemBackend:
    return FilesystemBackend(storage_root=storage_root, base_uri=BASE)


@pytest.fixture
def pod_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    monkeypatch.setenv("LDP_BASE_URI", BASE)
    monkeypatch.setenv("LDP_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("LDP_ADMIN_TOKEN", ADMIN_TOKEN)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture
def client(pod_env: None) -> Generator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin_token() -> str:
    return ADMIN_TOKEN
