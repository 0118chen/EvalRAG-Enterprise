from app.config import Settings
from evaluation import experiment_metadata, retrieval_evaluators


def test_evaluation_metadata_is_safe() -> None:
    metadata = experiment_metadata(Settings(), "policy-recall-basic", "hybrid", 5)
    assert "api_key" not in str(metadata).lower()
    assert len(retrieval_evaluators()) == 2
