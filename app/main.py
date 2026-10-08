"""FastAPI application entrypoint."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import auth, chat, documents, evaluations, feedback, health, knowledge
from app.config import Settings, get_settings
from app.container import build_container
from app.middleware import MetricsMiddleware, RateLimitMiddleware, RequestContextMiddleware


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    application = FastAPI(
        title="EvalRAG Enterprise",
        version=resolved_settings.rag_version,
    )
    application.state.container = build_container(resolved_settings)

    application.add_middleware(MetricsMiddleware)
    application.add_middleware(RateLimitMiddleware)
    application.add_middleware(RequestContextMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for router in (
        health.router,
        auth.router,
        knowledge.router,
        documents.router,
        chat.router,
        feedback.router,
        evaluations.router,
    ):
        application.include_router(router)
    return application


app = create_app()
