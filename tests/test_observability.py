from app.config import Settings
from app.core.observability import TraceManager, redact, tenant_hash


def test_sensitive_values_are_redacted() -> None:
    assert redact("联系 13800138000，证件 11010519491231002X") == "联系 [PHONE]，证件 [ID]"


def test_disabled_tracing_is_safe() -> None:
    manager = TraceManager(Settings(langsmith_enabled=False))
    decorated = manager.traceable("test", {})
    assert decorated(lambda: "ok")() == "ok"
    assert len(tenant_hash("tenant-a")) == 16
