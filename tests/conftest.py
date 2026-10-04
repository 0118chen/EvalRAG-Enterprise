"""Shared test fixtures.

The API fixtures deliberately build a *hermetic* app: `app.main.app` resolves
`Settings()` from the developer's `.env`, so a test that imports it writes into the real
`data/evalrag.db` and inherits whatever provider and LangSmith keys are configured
there (a test run could genuinely emit paid traces). Every fixture here pins the
settings it cares about and uses a private database.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def isolated_settings(**overrides: object) -> Settings:
    """Settings that share nothing with the developer's environment.

    The database is a *shared-cache in-memory* SQLite database: it touches no
    filesystem, and unlike a plain ``:memory:`` URL every connection sees the same
    schema - which matters because TestClient runs the sync routes on a worker thread.
    The unique name keeps one test's data out of the next one's.
    """
    name = f"evalrag-test-{uuid.uuid4().hex}"
    values: dict[str, object] = {
        "app_env": "test",
        "database_url": f"sqlite:///file:{name}?mode=memory&cache=shared&uri=true",
        "llm_provider": "mock",
        "langsmith_enabled": False,
        "langsmith_api_key": None,
        "auth_enabled": False,
        "cache_enabled": False,
        "rate_limit_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app(isolated_settings())) as test_client:
        yield test_client
