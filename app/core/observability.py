"""Trace hierarchy and redaction shared by API, workers and evaluation jobs."""

import hashlib
import logging
import re
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.config import Settings

logger = logging.getLogger(__name__)

_SPAN_CONTEXT: ContextVar[tuple[tuple[str, str], ...]] = ContextVar(
    "evalrag_span_context",
    default=(),
)


def redact(value: str) -> str:
    value = re.sub(r"(?<!\d)(1\d{10})(?!\d)", "[PHONE]", value)
    return re.sub(r"\b\d{17}[0-9Xx]\b", "[ID]", value)


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(key): redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item) for item in value]
    return value


def tenant_hash(tenant_id: str) -> str:
    return hashlib.sha256(tenant_id.encode()).hexdigest()[:16]


@dataclass
class SpanRecord:
    name: str
    run_type: str
    span_id: str
    trace_id: str
    parent_id: str | None
    started_at: datetime
    ended_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    inputs: Any = None
    outputs: Any = None
    error: str | None = None

    @property
    def duration_ms(self) -> float:
        if not self.ended_at:
            return 0.0
        return (self.ended_at - self.started_at).total_seconds() * 1000


@dataclass
class SpanHandle:
    record: SpanRecord

    @property
    def id(self) -> str:
        return self.record.span_id

    @property
    def trace_id(self) -> str:
        return self.record.trace_id

    @property
    def parent_id(self) -> str | None:
        return self.record.parent_id

    def set_outputs(self, outputs: Any) -> None:
        self.record.outputs = redact_value(outputs)

    def update_metadata(self, **metadata: Any) -> None:
        self.record.metadata.update(redact_value(metadata))

    def set_error(self, error: str) -> None:
        self.record.error = redact(error)


SpanRecorder = Callable[[SpanRecord], None]


class TraceManager:
    """Create nested LangSmith spans while remaining fully usable offline."""

    def __init__(self, settings: Settings, recorder: SpanRecorder | None = None):
        self.settings = settings
        self.recorder = recorder

    @property
    def enabled(self) -> bool:
        return bool(self.settings.langsmith_enabled and self.settings.langsmith_api_key)

    @property
    def current_trace_id(self) -> str | None:
        context = _SPAN_CONTEXT.get()
        return context[-1][1] if context else None

    @contextmanager
    def span(
        self,
        name: str,
        *,
        run_type: str = "chain",
        metadata: dict[str, Any] | None = None,
        inputs: Any = None,
        tags: list[str] | None = None,
        trace_id: str | None = None,
    ) -> Iterator[SpanHandle]:
        context = _SPAN_CONTEXT.get()
        parent_id = context[-1][0] if context else None
        resolved_trace_id = context[-1][1] if context else trace_id or str(uuid4())
        span_id = str(uuid4())
        record = SpanRecord(
            name=name,
            run_type=run_type,
            span_id=span_id,
            trace_id=resolved_trace_id,
            parent_id=parent_id,
            started_at=datetime.now(UTC),
            metadata=redact_value(metadata or {}),
            inputs=redact_value(inputs),
        )
        handle = SpanHandle(record)
        token = _SPAN_CONTEXT.set(context + ((span_id, resolved_trace_id),))
        langsmith_context = self._langsmith_context(
            name=name,
            run_type=run_type,
            metadata=record.metadata,
            inputs=record.inputs,
            tags=tags,
        )
        try:
            with ExitStack() as stack:
                try:
                    stack.enter_context(langsmith_context)
                except Exception:
                    logger.warning("LangSmith span failed to start", exc_info=True)
                yield handle
        except Exception as exc:
            record.error = str(exc)
            raise
        finally:
            record.ended_at = datetime.now(UTC)
            _SPAN_CONTEXT.reset(token)
            if self.recorder:
                self.recorder(record)

    def context(
        self,
        name: str,
        *,
        metadata: dict[str, Any] | None = None,
        inputs: Any = None,
    ):
        return self.span(name, metadata=metadata, inputs=inputs)

    def _langsmith_context(
        self,
        *,
        name: str,
        run_type: str,
        metadata: dict[str, Any],
        inputs: Any,
        tags: list[str] | None,
    ):
        if not self.enabled:
            return nullcontext()
        try:
            from langsmith import trace

            return trace(
                name,
                run_type=run_type,
                project_name=self.settings.langsmith_project,
                metadata=metadata,
                inputs=inputs,
                tags=tags or [],
                exceptions_to_handle=(Exception,),
            )
        except Exception:
            logger.warning("LangSmith tracing unavailable", exc_info=True)
            return nullcontext()

    def traceable(self, name: str, metadata: dict[str, Any]):
        """Return a LangSmith decorator when enabled, otherwise an identity decorator."""
        if not self.enabled:
            return lambda function: function
        try:
            from langsmith import traceable

            return traceable(
                name=name,
                metadata=redact_value(metadata),
                process_inputs=lambda inputs: {"input": redact_value(inputs)},
            )
        except Exception:
            logger.warning("LangSmith decorator unavailable", exc_info=True)
            return lambda function: function

    def metadata(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        retrieval_mode: str,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "tenant_id_hash": tenant_hash(tenant_id),
            "knowledge_base_id": knowledge_base_id,
            "retrieval_mode": retrieval_mode,
            "rag_version": self.settings.rag_version,
            "prompt_version": self.settings.prompt_version,
            "llm_model": self.settings.llm_model,
            "environment": self.settings.app_env,
            "request_id": request_id,
            "trace_id": trace_id or self.current_trace_id,
        }
