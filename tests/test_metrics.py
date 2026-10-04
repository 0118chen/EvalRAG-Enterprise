"""Prometheus exposition: bounded label cardinality and a usable latency histogram.

Two bugs are pinned here. Labelling by the raw request path put a new document id in a
metric label on every request, which grows the in-process counter without bound; and a
latency ``_sum`` with no ``_count`` and no ``_bucket`` lines cannot answer "what is p95".
"""

import uuid

from fastapi.testclient import TestClient

from app.core.metrics import DURATION_BUCKETS, Metrics, normalize_path


def test_normalize_path_collapses_only_the_identifiers() -> None:
    document_id = "3f2a9c1e-4b7d-4e21-9a55-0c1d2e3f4a5b"
    assert normalize_path(f"/api/v1/documents/{document_id}") == "/api/v1/documents/{id}"
    assert normalize_path("/api/v1/knowledge-bases/123456/documents") == (
        "/api/v1/knowledge-bases/{id}/documents"
    )
    # Static paths, short numbers and ordinary words are left alone.
    assert normalize_path("/health/ready") == "/health/ready"
    assert normalize_path("/api/v1/evaluations/42") == "/api/v1/evaluations/42"


def test_labelling_by_a_raw_path_cannot_explode_the_series_count() -> None:
    metrics = Metrics()
    for _ in range(5):
        metrics.observe(f"/api/v1/documents/{uuid.uuid4()}", 200, 0.01)

    rendered = metrics.render()

    assert rendered.count("evalrag_http_requests_total{") == 1
    assert 'path="/api/v1/documents/{id}"' in rendered


def test_the_histogram_is_cumulative_and_carries_count_and_sum() -> None:
    metrics = Metrics()
    metrics.observe("/health", 200, 0.004)  # bucket 0
    metrics.observe("/health", 200, 0.03)  # bucket 3
    metrics.observe("/health", 500, 12.0)  # above every bucket: only +Inf
    rendered = metrics.render()

    def bucket(le: str) -> int:
        line = next(
            item
            for item in rendered.splitlines()
            if item.startswith("evalrag_http_request_duration_seconds_bucket")
            and f'le="{le}"' in item
        )
        return int(line.rsplit(" ", 1)[1])

    assert bucket("0.005") == 1
    assert bucket("0.025") == 1  # nothing landed here, cumulative value holds
    assert bucket("0.05") == 2
    assert bucket("+Inf") == 3
    assert 'evalrag_http_request_duration_seconds_count{path="/health"} 3' in rendered
    assert 'evalrag_http_request_duration_seconds_sum{path="/health"} 12.034' in rendered
    assert len(DURATION_BUCKETS) == 11


def test_the_route_template_reaches_metrics_through_the_real_middleware(
    client: TestClient,
) -> None:
    document_ids = [str(uuid.uuid4()) for _ in range(3)]
    for document_id in document_ids:
        assert client.get(f"/api/v1/documents/{document_id}").status_code == 404
    client.get("/health")

    rendered = client.get("/metrics").text

    assert 'evalrag_http_requests_total{path="/api/v1/documents/{document_id}",status="404"} 3' in rendered
    assert 'path="/health"' in rendered
    for document_id in document_ids:
        assert document_id not in rendered
    assert 'evalrag_http_request_duration_seconds_count{path="/health"}' in rendered


def test_an_unrouted_path_is_still_collapsed(client: TestClient) -> None:
    document_id = str(uuid.uuid4())
    assert client.get(f"/api/v1/nope/{document_id}").status_code == 404

    rendered = client.get("/metrics").text

    assert 'path="/api/v1/nope/{id}"' in rendered
    assert document_id not in rendered
