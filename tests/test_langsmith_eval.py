from app.config import Settings
from app.core.langsmith_eval import LangSmithEvaluationAdapter, dataset_example


def test_dataset_example_redacts_sensitive_values() -> None:
    sample = dataset_example("请查询 13800138000 的政策", "kb-1", tenant_id="tenant-a")
    assert sample["inputs"]["question"] == "请查询 [PHONE] 的政策"
    assert sample["metadata"]["tenant_id_hash"] != "tenant-a"


def test_adapter_is_safe_when_disabled() -> None:
    adapter = LangSmithEvaluationAdapter(Settings(langsmith_enabled=False))
    assert adapter.create_dataset("offline") is None
