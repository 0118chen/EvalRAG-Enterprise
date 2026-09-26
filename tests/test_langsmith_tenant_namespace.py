"""LangSmith datasets are global to the account, so tenant scoping is enforced locally."""

import asyncio
import sys
import types
from types import SimpleNamespace

from app.config import Settings
from app.core.langsmith_eval import LangSmithEvaluationAdapter, remote_dataset_name


def test_remote_dataset_name_is_stable_per_tenant_and_does_not_leak_the_tenant_id() -> None:
    first = remote_dataset_name("tenant-a", "policy-eval")

    assert first == remote_dataset_name("tenant-a", "policy-eval")
    assert first != remote_dataset_name("tenant-b", "policy-eval")
    assert first.startswith("policy-eval--")
    assert "tenant-a" not in first


class FakeLangSmith:
    """Stand-in for the langsmith package that records remote dataset names."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.listed: list[str] = []
        self.experiment_data: list[str] = []

    def install(self, monkeypatch) -> None:
        recorder = self

        class Client:
            def __init__(self, api_key=None, api_url=None) -> None:
                self.api_key = api_key

            def create_dataset(self, dataset_name: str, description: str = ""):
                recorder.created.append(dataset_name)
                return SimpleNamespace(id=f"remote-{dataset_name}")

            def list_datasets(self, dataset_name: str, limit: int = 1):
                recorder.listed.append(dataset_name)
                return []

            def list_examples(self, dataset_id: str):
                return []

        async def aevaluate(
            target,
            data=None,
            evaluators=None,
            experiment_prefix=None,
            metadata=None,
            client=None,
        ):
            recorder.experiment_data.append(data)
            return SimpleNamespace(experiment_name=experiment_prefix, url=None)

        module = types.ModuleType("langsmith")
        module.Client = Client
        module.aevaluate = aevaluate
        monkeypatch.setitem(sys.modules, "langsmith", module)


def test_two_tenants_with_the_same_dataset_name_get_different_remote_datasets(
    monkeypatch,
) -> None:
    recorder = FakeLangSmith()
    recorder.install(monkeypatch)
    adapter = LangSmithEvaluationAdapter(
        Settings(langsmith_enabled=True, langsmith_api_key="test-key")
    )

    adapter.ensure_dataset("policy-eval", tenant_id="tenant-a")
    adapter.ensure_dataset("policy-eval", tenant_id="tenant-b")

    assert recorder.created == [
        remote_dataset_name("tenant-a", "policy-eval"),
        remote_dataset_name("tenant-b", "policy-eval"),
    ]
    # Lookup must also go through the namespaced name, otherwise tenant B would
    # silently attach to tenant A's remote dataset.
    assert recorder.listed == recorder.created


def test_experiment_runs_against_the_tenant_namespaced_dataset(monkeypatch) -> None:
    recorder = FakeLangSmith()
    recorder.install(monkeypatch)
    adapter = LangSmithEvaluationAdapter(
        Settings(langsmith_enabled=True, langsmith_api_key="test-key")
    )

    asyncio.run(
        adapter.run_experiment(
            dataset_name="policy-eval",
            tenant_id="tenant-a",
            target=lambda inputs: {},
            evaluators=[],
            experiment_prefix="run",
            metadata={},
        )
    )

    assert recorder.experiment_data == [remote_dataset_name("tenant-a", "policy-eval")]
