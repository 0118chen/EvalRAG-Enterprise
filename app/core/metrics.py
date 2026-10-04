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
from dataclasses import dataclass, field

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


class Metrics:
    def __init__(self) -> None:
        self.requests: Counter[tuple[str, int]] = Counter()
        self.errors: Counter[str] = Counter()
        self._durations: dict[str, _Histogram] = {}

    def observe(self, path: str, status: int, elapsed: float) -> None:
        label = normalize_path(path)
        self.requests[(label, status)] += 1
        if status >= 500:
            self.errors[label] += 1
        self._durations.setdefault(label, _Histogram()).observe(elapsed)

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
        return "\n".join(lines) + "\n"
