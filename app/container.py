"""Application dependency container built once per API or worker process."""

from dataclasses import dataclass

from app.config import Settings, get_settings
from app.core.cache import Cache, create_cache
from app.core.langsmith_eval import LangSmithEvaluationAdapter
from app.core.metrics import Metrics
from app.core.observability import TraceManager
from app.core.query_rewrite import QueryRewriter, create_query_rewriter
from app.core.rate_limit import RateLimiter, create_rate_limiter
from app.core.reranking import Reranker, create_reranker
from app.core.retrieval_service import RetrievalService
from app.core.storage import ObjectStore, create_object_store
from app.core.store import SQLAlchemyStore, create_store


@dataclass
class AppContainer:
    settings: Settings
    traces: TraceManager
    store: SQLAlchemyStore
    storage: ObjectStore
    cache: Cache
    query_rewriter: QueryRewriter
    reranker: Reranker
    retrieval: RetrievalService
    rate_limiter: RateLimiter | None
    metrics: Metrics
    langsmith: LangSmithEvaluationAdapter


def build_container(settings: Settings | None = None) -> AppContainer:
    resolved_settings = settings or get_settings()
    store = create_store(resolved_settings.database_url)
    traces = TraceManager(resolved_settings)
    # The cache and the retrieval pipeline report into the same metric state the API
    # exposes, so the handle has to exist before either of them is built.
    metrics = Metrics()
    cache = create_cache(resolved_settings, metrics)
    query_rewriter = create_query_rewriter(resolved_settings)
    reranker = create_reranker(resolved_settings)
    return AppContainer(
        settings=resolved_settings,
        traces=traces,
        store=store,
        storage=create_object_store(resolved_settings),
        cache=cache,
        query_rewriter=query_rewriter,
        reranker=reranker,
        retrieval=RetrievalService(
            resolved_settings,
            traces,
            cache,
            query_rewriter,
            reranker,
            metrics,
        ),
        rate_limiter=create_rate_limiter(resolved_settings),
        metrics=metrics,
        langsmith=LangSmithEvaluationAdapter(resolved_settings),
    )
