from app.config import Settings
from app.core.observability import TraceManager


def test_trace_manager_records_nested_spans() -> None:
    records = []
    manager = TraceManager(Settings(langsmith_enabled=False), records.append)
    with (
        manager.span("request", metadata={"request_id": "req-1"}) as parent,
        manager.span("retrieval", run_type="retriever") as child,
    ):
        child.set_outputs({"chunk_ids": ["c1"]})
    assert [record.name for record in records] == ["retrieval", "request"]
    assert records[0].parent_id == parent.id
    assert records[0].trace_id == parent.trace_id
    assert records[0].duration_ms >= 0
