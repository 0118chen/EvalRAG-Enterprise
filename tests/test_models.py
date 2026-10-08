from app.db.models import Base, DocumentRecord, KnowledgeBaseRecord


def test_database_models_have_expected_tables() -> None:
    assert set(Base.metadata.tables) == {
        "knowledge_bases",
        "documents",
        "chunks",
        "evaluation_datasets",
        "evaluation_examples",
        "evaluations",
        "feedback",
        "index_outbox",
        "api_sessions",
    }
    assert DocumentRecord.__tablename__ == "documents"
    assert KnowledgeBaseRecord.__tablename__ == "knowledge_bases"
