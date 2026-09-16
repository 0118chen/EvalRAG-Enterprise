"""Minimal dependency-free Prometheus exposition for the API MVP."""

from collections import Counter
from time import perf_counter


class Metrics:
    def __init__(self) -> None:
        self.requests = Counter()
        self.errors = Counter()
        self.latency_seconds = 0.0

    def observe(self, path: str, status: int, elapsed: float) -> None:
        self.requests[path] += 1
        if status >= 500:
            self.errors[path] += 1
        self.latency_seconds += elapsed

    def render(self) -> str:
        lines = [
            "# HELP evalrag_http_requests_total Total HTTP requests",
            "# TYPE evalrag_http_requests_total counter",
        ]
        lines.extend(f'evalrag_http_requests_total{{path="{path}"}} {count}' for path, count in self.requests.items())
        lines.extend(["# HELP evalrag_http_errors_total Total HTTP 5xx responses", "# TYPE evalrag_http_errors_total counter"])
        lines.extend(f'evalrag_http_errors_total{{path="{path}"}} {count}' for path, count in self.errors.items())
        lines.append(f"evalrag_http_request_latency_seconds_sum {self.latency_seconds}")
        return "\n".join(lines) + "\n"
