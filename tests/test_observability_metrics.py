"""Cache / retrieval / Celery / pool metrics and the widened readiness probe.

Every test here runs offline: the Redis probe is fed a fake client, the broker probe a fake
inspector, and the external backends are never constructed against a real endpoint. What the
tests pin down is the *recording path* - a metric that is defined but never incremented is the
failure mode this file exists to catch.
"""

import asyncio
from typing import Any, ClassVar, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.config import Settings
from app.core.backends import HybridRetriever, LocalRetriever, create_retriever
from app.core.cache import MemoryTTLCache, RedisTTLCache
from app.core.ingestion import Chunk
from app.core.metrics import DURATION_BUCKETS, Metrics, cache_backend_name
from app.core.observability import TraceManager
from app.core.query_rewrite import IdentityQueryRewriter
from app.core.reranking import LexicalReranker
from app.core.retrieval_service import RetrievalService
from app.main import create_app


def _settings(tmp_path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": f"sqlite:///{(tmp_path / 'obs.db').as_posix()}",
        "llm_provider": "mock",
        "langsmith_enabled": False,
        "langsmith_api_key": None,
    }
    values.update(overrides)
    return Settings(**values)


def _client(tmp_path, **overrides: Any) -> TestClient:
    return TestClient(create_app(_settings(tmp_path, **overrides)))


def _chunks() -> list[Chunk]:
    return [
        Chunk("c1", "kb", 1, "农户贷款应遵循依法合规、审慎经营的原则", "v1"),
        Chunk("c2", "kb", 1, "其他说明", "v1"),
    ]


class _FakeRedis:
    """Stand-in for ``redis.asyncio.Redis`` built by the readiness probe.

    ``ping`` resolves or raises what the test set on the class; ``aclose`` records that the
    probe closed the client it opened, which is the leak this double exists to notice.
    """

    instances: ClassVar[list["_FakeRedis"]] = []
    error: BaseException | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closed = False
        _FakeRedis.instances.append(self)

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> "_FakeRedis":
        return cls(url=url, **kwargs)

    async def ping(self) -> bool:
        error = _FakeRedis.error
        if error is not None:
            raise error
        return True

    async def aclose(self) -> None:
        self.closed = True


class _InMemoryRedisClient:
    """The subset of the async Redis API ``RedisTTLCache`` uses, backed by a dict."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value


@pytest.fixture(autouse=True)
def _reset_fake_redis() -> None:
    _FakeRedis.instances = []
    _FakeRedis.error = None


def _install_fake_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patching the real module's attribute rather than ``sys.modules["redis.asyncio"]``:
    # ``import redis.asyncio as x`` resolves through the parent package, so a sys.modules
    # swap is invisible to the code under test.
    redis_asyncio = pytest.importorskip("redis.asyncio")
    monkeypatch.setattr(redis_asyncio, "Redis", _FakeRedis)


# --------------------------------------------------------------------------------------
# cache counters
# --------------------------------------------------------------------------------------


def test_memory_cache_counts_hits_and_misses() -> None:
    metrics = Metrics()
    cache = MemoryTTLCache(ttl_seconds=60, metrics=metrics)

    async def run() -> None:
        assert await cache.get("absent") is None
        await cache.set("present", {"value": 1})
        assert await cache.get("present") == {"value": 1}

    asyncio.run(run())
    text = metrics.render()

    assert 'evalrag_cache_misses_total{backend="memory"} 1' in text
    assert 'evalrag_cache_hits_total{backend="memory"} 1' in text
    assert "# TYPE evalrag_cache_hits_total counter" in text
    assert "# TYPE evalrag_cache_misses_total counter" in text
    assert "# TYPE evalrag_cache_errors_total counter" in text


def test_expired_entry_counts_as_a_miss_not_a_hit() -> None:
    metrics = Metrics()
    now = {"value": 0.0}
    cache = MemoryTTLCache(ttl_seconds=1, clock=lambda: now["value"], metrics=metrics)

    async def run() -> None:
        await cache.set("entry", "value")
        now["value"] = 10.0
        assert await cache.get("entry") is None

    asyncio.run(run())
    text = metrics.render()

    assert 'evalrag_cache_misses_total{backend="memory"} 1' in text
    # The HELP/TYPE lines are always published, so only a labelled sample proves no hit.
    assert 'evalrag_cache_hits_total{backend="memory"}' not in text


def test_redis_cache_separates_an_outage_from_a_miss() -> None:
    metrics = Metrics()
    cache = RedisTTLCache("redis://localhost:6379/0", ttl_seconds=60, metrics=metrics)

    class _FailingClient:
        async def get(self, key: str) -> str | None:
            raise OSError("connection refused")

    cache.client = cast(Any, _FailingClient())

    assert asyncio.run(cache.get("anything")) is None
    text = metrics.render()

    assert 'evalrag_cache_errors_total{backend="redis"} 1' in text
    # An unreachable Redis is not an empty cache: booking it as a miss would quietly
    # deflate the hit ratio instead of pointing at the outage.
    assert 'evalrag_cache_misses_total{backend="redis"}' not in text


def test_redis_cache_counts_hits_and_misses() -> None:
    metrics = Metrics()
    cache = RedisTTLCache("redis://localhost:6379/0", ttl_seconds=60, metrics=metrics)
    cache.client = cast(Any, _InMemoryRedisClient())

    async def run() -> None:
        await cache.set("present", {"value": 1})
        assert await cache.get("present") == {"value": 1}
        assert await cache.get("absent") is None

    asyncio.run(run())
    text = metrics.render()

    assert 'evalrag_cache_hits_total{backend="redis"} 1' in text
    assert 'evalrag_cache_misses_total{backend="redis"} 1' in text


def test_cache_backend_name_reads_the_instance() -> None:
    class Unknown:
        pass

    assert cache_backend_name(MemoryTTLCache()) == "memory"
    assert cache_backend_name(RedisTTLCache("redis://localhost:6379/0")) == "redis"
    # A cache type this module does not know still gets a bounded label instead of raising.
    assert cache_backend_name(Unknown()) == "unknown"


def test_a_cache_without_metrics_does_not_count() -> None:
    # NullCache is returned whenever caching is disabled; it has no counters to bump and
    # must not raise on the miss path either.
    from app.core.cache import NullCache

    assert asyncio.run(NullCache().get("absent")) is None


# --------------------------------------------------------------------------------------
# retrieval stage histograms
# --------------------------------------------------------------------------------------


def test_local_retriever_times_its_channel() -> None:
    metrics = Metrics()
    chunks = _chunks()

    asyncio.run(LocalRetriever(chunks, "sparse", metrics=metrics).search("贷款", 1))
    asyncio.run(LocalRetriever(chunks, "dense", metrics=metrics).search("贷款", 1))

    text = metrics.render()
    assert 'evalrag_retrieval_stage_duration_seconds_count{stage="sparse"} 1' in text
    assert 'evalrag_retrieval_stage_duration_seconds_count{stage="dense"} 1' in text
    assert "# TYPE evalrag_retrieval_stage_duration_seconds histogram" in text


def test_hybrid_retriever_times_both_channels_and_the_fusion() -> None:
    metrics = Metrics()
    chunks = _chunks()
    # Each channel is timed by the retriever that serves it, which is how `create_retriever`
    # wires them; the hybrid itself books only the merge.
    retriever = HybridRetriever(
        LocalRetriever(chunks, "dense", metrics=metrics),
        LocalRetriever(chunks, "sparse", metrics=metrics),
        metrics=metrics,
    )

    results = asyncio.run(retriever.search("贷款", 1))
    text = metrics.render()

    assert results
    for stage in ("dense", "sparse", "fusion"):
        assert (
            f'evalrag_retrieval_stage_duration_seconds_count{{stage="{stage}"}} 1' in text
        )


def test_stage_histogram_exposes_cumulative_buckets() -> None:
    metrics = Metrics()
    asyncio.run(LocalRetriever(_chunks(), "sparse", metrics=metrics).search("贷款", 1))

    stage = metrics._stage_durations["sparse"]
    cumulative = sum(stage.buckets)
    assert cumulative == stage.count == 1
    assert f'le="{DURATION_BUCKETS[0]}"' in metrics.render()


def test_retrieval_service_times_the_fused_stage() -> None:
    metrics = Metrics()
    settings = Settings(query_rewrite_enabled=True, rerank_enabled=True)
    service = RetrievalService(
        settings,
        TraceManager(settings),
        MemoryTTLCache(60),
        IdentityQueryRewriter(),
        LexicalReranker(),
        metrics,
    )

    async def run() -> None:
        return await service.search(
            tenant_id="tenant",
            knowledge_base_id="kb",
            question="农户贷款原则",
            chunks=_chunks(),
            top_k=1,
            mode="hybrid",
            document_version="v1",
        )

    result = asyncio.run(run())
    text = metrics.render()

    assert result.cache_hit is False
    assert 'evalrag_retrieval_stage_duration_seconds_count{stage="fusion"} 1' in text
    assert 'evalrag_retrieval_stage_duration_seconds_count{stage="rerank"} 1' in text


def test_create_retriever_carries_metrics_into_the_channels() -> None:
    metrics = Metrics()
    settings = Settings(dense_retrieval_backend="local", sparse_retrieval_backend="local")
    retriever = create_retriever(
        settings, _chunks(), "hybrid", "kb", "v1", metrics=metrics
    )

    asyncio.run(retriever.search("贷款", 1))
    text = metrics.render()
    assert 'evalrag_retrieval_stage_duration_seconds_count{stage="fusion"} 1' in text


# --------------------------------------------------------------------------------------
# Celery counters
# --------------------------------------------------------------------------------------


def test_celery_counters_are_rendered_per_task() -> None:
    metrics = Metrics()
    metrics.observe_task_started("evalrag.process_document")
    metrics.observe_task_succeeded("evalrag.process_document")
    metrics.observe_task_started("evalrag.process_evaluation")
    metrics.observe_task_failed("evalrag.process_evaluation")

    text = metrics.render()
    assert (
        'evalrag_celery_tasks_started_total{task="evalrag.process_document"} 1' in text
    )
    assert (
        'evalrag_celery_tasks_succeeded_total{task="evalrag.process_document"} 1' in text
    )
    assert (
        'evalrag_celery_tasks_started_total{task="evalrag.process_evaluation"} 1' in text
    )
    assert 'evalrag_celery_tasks_failed_total{task="evalrag.process_evaluation"} 1' in text
    assert "# TYPE evalrag_celery_tasks_started_total counter" in text


def test_task_body_reports_started_succeeded_and_failed(tmp_path, monkeypatch) -> None:
    from app import tasks

    metrics = Metrics()
    monkeypatch.setattr(tasks, "metrics", metrics)

    def ok(value: str) -> str:
        return value

    def boom(value: str) -> str:
        raise RuntimeError("kaboom")

    tracked_ok = tasks._tracked("evalrag.test_task", ok)
    tracked_boom = tasks._tracked("evalrag.test_task_error", boom)

    assert tracked_ok("x") == "x"
    with pytest.raises(RuntimeError):
        tracked_boom("x")

    text = metrics.render()
    assert 'evalrag_celery_tasks_started_total{task="evalrag.test_task"} 1' in text
    assert 'evalrag_celery_tasks_succeeded_total{task="evalrag.test_task"} 1' in text
    assert 'evalrag_celery_tasks_failed_total{task="evalrag.test_task"}' not in text
    assert (
        'evalrag_celery_tasks_failed_total{task="evalrag.test_task_error"} 1' in text
    )
    assert (
        'evalrag_celery_tasks_succeeded_total{task="evalrag.test_task_error"}' not in text
    )


def test_process_document_run_counts_through_the_worker_metrics(tmp_path, monkeypatch) -> None:
    from app import tasks

    metrics = Metrics()
    monkeypatch.setattr(tasks, "metrics", metrics)
    monkeypatch.setattr(
        tasks,
        "_process_document",
        lambda document_id, force=False: {"document_id": document_id, "status": "ready"},
    )
    # Rebuild the public name so it wraps the patched body, exactly like module import does.
    tracked = tasks._tracked("evalrag.process_document", tasks._process_document)

    assert tracked("doc-1")["status"] == "ready"
    text = metrics.render()
    assert 'evalrag_celery_tasks_started_total{task="evalrag.process_document"} 1' in text
    assert 'evalrag_celery_tasks_succeeded_total{task="evalrag.process_document"} 1' in text


# --------------------------------------------------------------------------------------
# database pool gauges
# --------------------------------------------------------------------------------------


def test_pool_gauges_come_from_the_sqlalchemy_pool(tmp_path) -> None:
    metrics = Metrics()
    # A file-backed SQLite URL gets the real QueuePool; the in-memory URL would use
    # SingletonThreadPool, which has no pool_size/overflow to report.
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'pool.db').as_posix()}", pool_size=5, max_overflow=3
    )
    try:
        metrics.read_engine_pool(engine)
    finally:
        engine.dispose()
    text = metrics.render()

    assert 'evalrag_db_pool_size{pool="default"} 5' in text
    assert 'evalrag_db_pool_max_overflow{pool="default"} 3' in text
    assert 'evalrag_db_pool_checked_out{pool="default"} 0' in text
    assert "# TYPE evalrag_db_pool_size gauge" in text


def test_pool_gauge_reading_never_raises_on_a_pool_without_accessors() -> None:
    metrics = Metrics()
    metrics.read_pool(object(), label="broken")
    # No accessor answered, so no sample is published - but the scrape still renders.
    assert 'evalrag_db_pool_size{pool="broken"}' not in metrics.render()


def test_metrics_endpoint_publishes_pool_and_http_series(tmp_path) -> None:
    with _client(tmp_path) as client:
        client.get("/health/live")
        body = client.get("/metrics")

    assert body.status_code == 200
    text = body.text
    assert 'evalrag_http_requests_total{path="/health/live",status="200"} 1' in text
    assert 'evalrag_db_pool_size{pool="default"}' in text
    assert "# TYPE evalrag_retrieval_stage_duration_seconds histogram" in text


# --------------------------------------------------------------------------------------
# external backend health gauge
# --------------------------------------------------------------------------------------


def test_backend_health_gauge_renders_zero_and_one() -> None:
    metrics = Metrics()
    metrics.set_backend_health("milvus", True)
    metrics.set_backend_health("elasticsearch", False)
    text = metrics.render()

    assert 'evalrag_external_backend_up{backend="milvus"} 1' in text
    assert 'evalrag_external_backend_up{backend="elasticsearch"} 0' in text
    assert "# TYPE evalrag_external_backend_up gauge" in text


# --------------------------------------------------------------------------------------
# readiness
# --------------------------------------------------------------------------------------


def _checks(body: dict) -> dict:
    checks = body["checks"]
    assert isinstance(checks, dict)
    return checks


def test_readiness_keeps_the_legacy_fields_and_adds_checks(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["database"] == "ok"
    checks = _checks(body)
    assert checks["database"]["status"] == "ok"
    assert checks["redis"]["status"] == "skipped"
    assert checks["queue"]["status"] == "skipped"
    assert checks["milvus"]["status"] == "skipped"
    assert checks["elasticsearch"]["status"] == "skipped"


def test_readiness_degrades_when_the_broker_has_no_worker(tmp_path, monkeypatch) -> None:
    def no_worker(settings: Settings) -> dict:
        return {"status": "error", "error": "no Celery worker answered the ping"}

    monkeypatch.setattr("app.api.routes.health._inspect_queue", no_worker)
    with _client(tmp_path) as client:
        response = client.get("/health/ready")

    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"] == "ok"
    checks = _checks(body)
    assert checks["queue"]["status"] == "error"
    assert "no Celery worker" in checks["queue"]["error"]
    # One degraded dependency must not hide the healthy ones.
    assert checks["database"]["status"] == "ok"


def _healthy_queue() -> dict:
    return {"status": "ok", "workers": 1, "running": 0}


def test_readiness_degrades_when_the_redis_ping_fails(tmp_path, monkeypatch) -> None:
    _FakeRedis.error = OSError("connection refused")
    _install_fake_redis(monkeypatch)
    # A worker answering keeps the queue check out of this test's way.
    monkeypatch.setattr("app.api.routes.health._inspect_queue", lambda settings: _healthy_queue())

    with _client(tmp_path, cache_backend="redis", cache_enabled=True) as client:
        response = client.get("/health/ready")

    body = response.json()
    assert body["status"] == "degraded"
    checks = _checks(body)
    assert checks["redis"]["status"] == "error"
    assert "connection refused" in checks["redis"]["error"]
    assert checks["queue"]["status"] == "ok"
    # `create_cache` builds one client for the container; the probe builds and closes another.
    assert _FakeRedis.instances[-1].closed is True


def test_readiness_reports_a_reachable_redis_as_ok(tmp_path, monkeypatch) -> None:
    _install_fake_redis(monkeypatch)
    monkeypatch.setattr("app.api.routes.health._inspect_queue", lambda settings: _healthy_queue())

    with _client(tmp_path, cache_backend="redis", cache_enabled=True) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert _checks(response.json())["redis"]["status"] == "ok"


def test_readiness_degrades_when_an_enabled_backend_probe_fails(
    tmp_path, monkeypatch
) -> None:
    class FailingMilvus:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def health(self) -> bool:
            raise RuntimeError("milvus offline")

    # The probe imports the retriever inside the request, so the backend module is the seam.
    monkeypatch.setattr("app.core.backends.MilvusDenseRetriever", FailingMilvus)

    with _client(tmp_path, dense_retrieval_backend="milvus") as client:
        response = client.get("/health/ready")
        metrics = client.get("/metrics")

    body = response.json()
    checks = _checks(body)
    assert response.status_code == 503
    assert checks["milvus"]["status"] == "error"
    assert "milvus offline" in checks["milvus"]["error"]
    assert checks["elasticsearch"]["status"] == "skipped"
    assert 'evalrag_external_backend_up{backend="milvus"} 0' in metrics.text


def test_readiness_marks_a_healthy_backend_up(tmp_path, monkeypatch) -> None:
    class HealthyMilvus:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def health(self) -> bool:
            return True

    monkeypatch.setattr("app.core.backends.MilvusDenseRetriever", HealthyMilvus)

    with _client(tmp_path, dense_retrieval_backend="milvus") as client:
        response = client.get("/health/ready")
        metrics = client.get("/metrics")

    assert response.status_code == 200
    assert _checks(response.json())["milvus"]["status"] == "ok"
    assert 'evalrag_external_backend_up{backend="milvus"} 1' in metrics.text


def test_readiness_returns_503_when_the_database_is_unreachable(tmp_path, monkeypatch) -> None:
    import app.api.routes.health as health_module

    def broken_database(container: Any) -> dict:
        return {"status": "error", "error": "OperationalError: unable to open database file"}

    monkeypatch.setattr(health_module, "_check_database", broken_database)
    with _client(tmp_path) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["database"] == "error"
