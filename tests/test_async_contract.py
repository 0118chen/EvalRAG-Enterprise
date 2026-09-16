from app.tasks import process_document


def test_celery_task_is_registered() -> None:
    assert hasattr(process_document, "delay")
