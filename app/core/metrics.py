"""Dependency-free Prometheus exposition for the API.

Two properties matter more than the metric set itself:

* **Bounded cardinality.** Labels come from the *route template*
  (``/api/v1/documents/{document_id}``), never from the raw request path. Labelling by
  raw path creates one time series per document id ever requested; the counter grows
  without bound inside the process and the scrape payload grows with it, which is the
  classic way to take a Prometheus server down from the application side.
* **A real histogram.** A latency ``_sum`` on its own cannot answer "what is p95" -
  Prometheus needs the ``_bucket`` lines and the ``_count`` that pairs with ``_sum``.

State is per-process. Running several workers adds their counters at scrape time only
if the exposition is merged by a multi-process collector, which this module does not do.
"""

import re
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import perf_counter

# Prometheus default bucket layout, in seconds: fine enough for a cache hit and wide
# enough for a paid reranker round trip.
DURATION_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

_UID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def normalize_path(path: str) -> str:
    """Collapse identifiers in a path that matched no route, so a 404 stays bounded.

    Routed requests never reach this: the middleware reads the route template off the
    scope. This is the fallback for 404s, where there is no template to read.
    """
    parts = []
    for segment in path.split("/"):
        if _UID.match(segment) or (segment.isdigit() and len(segment) >= 4):
            parts.append("{id}")
        else:
            parts.append(segment)
    return "/".join(parts)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_gauge(value: float) -> str:
    """Render a gauge without a trailing ``.0`` on whole numbers.

    Prometheus accepts either form; the integer form keeps the readiness probe's 0/1
    backend health readable in a ``grep`` and in the tests that assert on the text.
    """
    return str(int(value)) if float(value).is_integer() else repr(float(value))


@dataclass
class _Histogram:
    """Non-cumulative bucket counts; the exposition makes them cumulative."""

    buckets: list[int] = field(default_factory=lambda: [0] * len(DURATION_BUCKETS))
    count: int = 0
    total: float = 0.0

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        for index, bound in enumerate(DURATION_BUCKETS):
            if value <= bound:
                self.buckets[index] += 1
                break


_CACHE_BACKENDS = {
    "MemoryTTLCache": "memory",
    "RedisTTLCache": "redis",
    "NullCache": "none",
}


def cache_backend_name(cache: object) -> str:
    """The ``backend`` label for a cache instance: ``memory``, ``redis`` or ``none``.

    The label is read off the instance - by class name, because this module must not import
    ``app.core.cache`` (that module imports this one) - so the counter follows the object that
    actually served the lookup. A Redis deployment that fell back to ``MemoryTTLCache`` at
    startup therefore reports ``memory``, which is the truth about where the value came from.
    """
    return _CACHE_BACKENDS.get(type(cache).__name__, "unknown")


def _read_pool_number(pool: object, accessor: str) -> float | None:
    """Read one pool accessor, or ``None`` when the pool does not offer it.

    SQLAlchemy exposes most accessors as methods but ``_max_overflow`` as a plain
    attribute, so both shapes are read. A pool whose driver raises is reported as "no
    reading": the scrape must never fail because a gauge could not be sampled.
    """
    raw = getattr(pool, accessor, None)
    if raw is None:
        return None
    try:
        value = raw() if callable(raw) else raw
    except Exception:  # noqa: BLE001 - any driver failure means "no reading", not a crash
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


class Metrics:
    """Per-process metric state, rendered as Prometheus text by :meth:`render`."""

    def __init__(self) -> None:
        self.requests: Counter[tuple[str, int]] = Counter()
        self.errors: Counter[str] = Counter()
        self._durations: dict[str, _Histogram] = {}
        self.cache_hits: Counter[str] = Counter()
        self.cache_misses: Counter[str] = Counter()
        self.cache_errors: Counter[str] = Counter()
        self.celery_started: Counter[str] = Counter()
        self.celery_succeeded: Counter[str] = Counter()
        self.celery_failed: Counter[str] = Counter()
        self._stage_durations: dict[str, _Histogram] = {}
        self._pool_gauges: dict[str, dict[str, float]] = {}
        self._backend_health: dict[str, float] = {}

    def observe(self, path: str, status: int, elapsed: float) -> None:
        label = normalize_path(path)
        self.requests[(label, status)] += 1
        if status >= 500:
            self.errors[label] += 1
        self._durations.setdefault(label, _Histogram()).observe(elapsed)

    # -- cache -------------------------------------------------------------------------

    def observe_cache_hit(self, backend: str) -> None:
        self.cache_hits[backend] += 1

    def observe_cache_miss(self, backend: str) -> None:
        self.cache_misses[backend] += 1

    def observe_cache_error(self, backend: str) -> None:
        self.cache_errors[backend] += 1

    # -- retrieval ---------------------------------------------------------------------

    def observe_stage(self, stage: str, elapsed: float) -> None:
        """Record one retrieval stage duration under its ``stage`` label."""
        self._stage_durations.setdefault(stage, _Histogram()).observe(elapsed)

    @contextmanager
    def time_stage(self, stage: str) -> Iterator[None]:
        """Measure a block of code into ``evalrag_retrieval_stage_duration_seconds``.

        The timing lives here rather than in a decorator because the interesting boundary
        is a single call inside a much longer pipeline: timing the whole ``search`` would
        report the sum of every channel under one label and hide which one is slow.
        """
        started = perf_counter()
        try:
            yield
        finally:
            self.observe_stage(stage, perf_counter() - started)

    # -- celery ------------------------------------------------------------------------

    def observe_task_started(self, task: str) -> None:
        self.celery_started[task] += 1

    def observe_task_succeeded(self, task: str) -> None:
        self.celery_succeeded[task] += 1

    def observe_task_failed(self, task: str) -> None:
        self.celery_failed[task] += 1

    # -- database pool -----------------------------------------------------------------

    def read_pool(self, pool: object, label: str = "default") -> None:
        """Snapshot one SQLAlchemy pool into per-``pool`` gauges.

        Read from the pool's own accessors (never by reaching into ``_pool``) so this keeps
        working across SQLAlchemy implementations - ``StaticPool`` for the in-memory SQLite
        used in tests, ``QueuePool`` for PostgreSQL. A pool that does not implement an
        accessor is skipped instead of crashing the scrape.
        """
        gauges: dict[str, float] = {}
        for name, accessor in (
            ("size", "size"),
            ("checked_out", "checkedout"),
            ("idle", "checkedin"),
            ("overflow", "overflow"),
            ("max_overflow", "_max_overflow"),
        ):
            value = _read_pool_number(pool, accessor)
            if value is not None:
                gauges[name] = value
        if gauges:
            self._pool_gauges[label] = gauges

    def read_engine_pool(self, engine: object, label: str = "default") -> None:
        pool = getattr(engine, "pool", None)
        if pool is not None:
            self.read_pool(pool, label)

    # -- external backends -------------------------------------------------------------

    def set_backend_health(self, backend: str, healthy: bool) -> None:
        self._backend_health[backend] = 1.0 if healthy else 0.0

    def render(self) -> str:
        lines = [
            "# HELP evalrag_http_requests_total Total HTTP requests",
            "# TYPE evalrag_http_requests_total counter",
        ]
        lines.extend(
            f'evalrag_http_requests_total{{path="{_escape(path)}",status="{status}"}} {count}'
            for (path, status), count in sorted(self.requests.items())
        )
        lines.extend(
            [
                "# HELP evalrag_http_errors_total Total HTTP 5xx responses",
                "# TYPE evalrag_http_errors_total counter",
            ]
        )
        lines.extend(
            f'evalrag_http_errors_total{{path="{_escape(path)}"}} {count}'
            for path, count in sorted(self.errors.items())
        )
        lines.extend(
            [
                "# HELP evalrag_http_request_duration_seconds Request latency in seconds",
                "# TYPE evalrag_http_request_duration_seconds histogram",
            ]
        )
        for path, histogram in sorted(self._durations.items()):
            label = _escape(path)
            cumulative = 0
            for bound, observed in zip(DURATION_BUCKETS, histogram.buckets, strict=True):
                cumulative += observed
                lines.append(
                    "evalrag_http_request_duration_seconds_bucket"
                    f'{{path="{label}",le="{bound}"}} {cumulative}'
                )
            lines.append(
                "evalrag_http_request_duration_seconds_bucket"
                f'{{path="{label}",le="+Inf"}} {histogram.count}'
            )
            lines.append(
                f'evalrag_http_request_duration_seconds_sum{{path="{label}"}} {histogram.total}'
            )
            lines.append(
                f'evalrag_http_request_duration_seconds_count{{path="{label}"}} {histogram.count}'
            )
        self._render_cache(lines)
        self._render_stages(lines)
        self._render_celery(lines)
        self._render_pool(lines)
        self._render_backend_health(lines)
        return "\n".join(lines) + "\n"

    def _render_cache(self, lines: list[str]) -> None:
        for name, help_text, counter in (
            ("evalrag_cache_hits_total", "Cache lookups served from the cache", self.cache_hits),
            ("evalrag_cache_misses_total", "Cache lookups that found nothing", self.cache_misses),
            ("evalrag_cache_errors_total", "Cache operations that raised", self.cache_errors),
        ):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} counter")
            lines.extend(
                f'{name}{{backend="{_escape(backend)}"}} {count}'
                for backend, count in sorted(counter.items())
            )

    def _render_stages(self, lines: list[str]) -> None:
        name = "evalrag_retrieval_stage_duration_seconds"
        lines.append(f"# HELP {name} Retrieval stage latency in seconds")
        lines.append(f"# TYPE {name} histogram")
        for stage, histogram in sorted(self._stage_durations.items()):
            label = _escape(stage)
            cumulative = 0
            for bound, observed in zip(DURATION_BUCKETS, histogram.buckets, strict=True):
                cumulative += observed
                lines.append(
                    f'{name}_bucket{{stage="{label}",le="{bound}"}} {cumulative}'
                )
            lines.append(f'{name}_bucket{{stage="{label}",le="+Inf"}} {histogram.count}')
            lines.append(f'{name}_sum{{stage="{label}"}} {histogram.total}')
            lines.append(f'{name}_count{{stage="{label}"}} {histogram.count}')

    def _render_celery(self, lines: list[str]) -> None:
        for name, help_text, counter in (
            ("evalrag_celery_tasks_started_total", "Celery tasks that started", self.celery_started),
            (
                "evalrag_celery_tasks_succeeded_total",
                "Celery tasks that finished without raising",
                self.celery_succeeded,
            ),
            ("evalrag_celery_tasks_failed_total", "Celery tasks that raised", self.celery_failed),
        ):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} counter")
            lines.extend(
                f'{name}{{task="{_escape(task)}"}} {count}'
                for task, count in sorted(counter.items())
            )

    def _render_pool(self, lines: list[str]) -> None:
        for gauge, help_text in (
            ("size", "Connections the pool currently holds"),
            ("checked_out", "Connections checked out by callers"),
            ("idle", "Connections idle in the pool"),
            ("overflow", "Connections opened beyond pool_size"),
            ("max_overflow", "Configured overflow ceiling"),
        ):
            name = f"evalrag_db_pool_{gauge}"
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            for label, gauges in sorted(self._pool_gauges.items()):
                if gauge in gauges:
                    lines.append(
                        f'{name}{{pool="{_escape(label)}"}} {_format_gauge(gauges[gauge])}'
                    )

    def _render_backend_health(self, lines: list[str]) -> None:
        name = "evalrag_external_backend_up"
        lines.append(f"# HELP {name} 1 when the external backend answered its probe")
        lines.append(f"# TYPE {name} gauge")
        lines.extend(
            f'{name}{{backend="{_escape(backend)}"}} {_format_gauge(value)}'
            for backend, value in sorted(self._backend_health.items())
        )
