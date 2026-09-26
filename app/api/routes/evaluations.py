from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from app.api.deps import authenticate, get_container, resolve_tenant_id
from app.core.langsmith_eval import dataset_example
from app.schemas import (
    EvaluationCreate,
    EvaluationDataset,
    EvaluationDatasetCreate,
    EvaluationExample,
    EvaluationJob,
)
from app.tasks import process_evaluation

router = APIRouter(prefix="/api/v1", tags=["evaluations"])


@router.post(
    "/evaluation-datasets",
    response_model=EvaluationDataset,
    status_code=201,
)
def create_dataset(
    payload: EvaluationDatasetCreate,
    request: Request,
    _auth: str | None = Depends(authenticate),
) -> EvaluationDataset:
    container = get_container(request)
    tenant_id = resolve_tenant_id(request, payload.tenant_id)
    knowledge_base = container.store.get_knowledge_base(
        payload.knowledge_base_id,
        tenant_id,
    )
    if not knowledge_base:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if container.store.get_evaluation_dataset_by_name(payload.name, tenant_id):
        raise HTTPException(status_code=409, detail="dataset name already exists")
    dataset_id = str(uuid4())
    dataset = EvaluationDataset(
        id=dataset_id,
        tenant_id=tenant_id,
        knowledge_base_id=payload.knowledge_base_id,
        name=payload.name,
        description=payload.description,
        examples=[
            EvaluationExample(
                id=str(uuid4()),
                dataset_id=dataset_id,
                **example.model_dump(),
            )
            for example in payload.examples
        ],
    )
    container.store.save_evaluation_dataset(dataset)
    remote_dataset_id = container.langsmith.ensure_dataset(
        dataset.name,
        dataset.description,
        tenant_id=tenant_id,
    )
    if remote_dataset_id:
        container.langsmith.create_examples(
            remote_dataset_id,
            [
                dataset_example(
                    example.question,
                    dataset.knowledge_base_id,
                    tenant_id=dataset.tenant_id,
                    category=example.category,
                    expected_document_id=example.expected_document_id,
                    reference={
                        "expected_page": example.expected_page,
                        "expected_answer": example.expected_answer,
                    },
                )
                for example in dataset.examples
            ],
        )
    return dataset


@router.get(
    "/evaluation-datasets",
    response_model=list[EvaluationDataset],
)
def list_datasets(
    request: Request,
    tenant_id: str = "",
    _auth: str | None = Depends(authenticate),
) -> list[EvaluationDataset]:
    tenant_id = resolve_tenant_id(request, tenant_id)
    return get_container(request).store.list_evaluation_datasets(tenant_id)


@router.get(
    "/evaluation-datasets/{dataset_id}",
    response_model=EvaluationDataset,
)
def get_dataset(
    dataset_id: str,
    request: Request,
    tenant_id: str = "",
    _auth: str | None = Depends(authenticate),
) -> EvaluationDataset:
    tenant_id = resolve_tenant_id(request, tenant_id)
    dataset = get_container(request).store.get_evaluation_dataset(
        dataset_id,
        tenant_id,
    )
    if not dataset:
        raise HTTPException(status_code=404, detail="dataset not found")
    return dataset


@router.post("/evaluations", response_model=EvaluationJob, status_code=202)
def create_evaluation(
    payload: EvaluationCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    _auth: str | None = Depends(authenticate),
) -> EvaluationJob:
    container = get_container(request)
    tenant_id = resolve_tenant_id(request, payload.tenant_id)
    knowledge_base = container.store.get_knowledge_base(
        payload.knowledge_base_id,
        tenant_id,
    )
    if not knowledge_base:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    dataset = container.store.get_evaluation_dataset_by_name(
        payload.dataset_name,
        tenant_id,
    )
    if not dataset:
        raise HTTPException(status_code=404, detail="evaluation dataset not found")
    if payload.answer_evaluation and container.settings.llm_provider == "mock":
        raise HTTPException(
            status_code=503,
            detail="answer evaluation requires a configured LLM",
        )
    if payload.baseline_evaluation_id:
        baseline = container.store.get_evaluation(payload.baseline_evaluation_id)
        if not baseline or baseline["tenant_id"] != tenant_id:
            # Same 404 as a missing baseline so the endpoint does not confirm
            # that another tenant's evaluation id exists.
            raise HTTPException(status_code=404, detail="baseline evaluation not found")
    evaluation_id = str(uuid4())
    parameters = {
        "document_version": payload.document_version,
        "rerank": payload.rerank,
        "query_rewrite": payload.query_rewrite,
        "answer_evaluation": payload.answer_evaluation,
    }
    container.store.create_evaluation(
        evaluation_id,
        payload.dataset_name,
        payload.retrieval_mode,
        payload.top_k,
        tenant_id=tenant_id,
        knowledge_base_id=payload.knowledge_base_id,
        dataset_id=dataset.id,
        experiment_name=payload.experiment_name,
        baseline_evaluation_id=payload.baseline_evaluation_id,
        parameters=parameters,
    )
    queued = False
    if hasattr(process_evaluation, "delay"):
        try:
            process_evaluation.delay(evaluation_id)
            queued = True
        except Exception:  # noqa: BLE001
            queued = False
    if not queued and container.settings.evaluation_inline_fallback:
        background_tasks.add_task(process_evaluation, evaluation_id)
        queued = True
    if not queued:
        container.store.update_evaluation(
            evaluation_id,
            "failed",
            error_message="Celery worker is not available",
        )
        raise HTTPException(
            status_code=503,
            detail="evaluation task could not be queued",
        )
    evaluation = container.store.get_evaluation(evaluation_id)
    if not evaluation:
        raise HTTPException(status_code=500, detail="evaluation was not persisted")
    return EvaluationJob(**evaluation)


@router.get("/evaluations", response_model=list[EvaluationJob])
def list_evaluations(
    request: Request,
    tenant_id: str = "",
    dataset_name: str | None = None,
    status: str | None = None,
    _auth: str | None = Depends(authenticate),
) -> list[EvaluationJob]:
    tenant_id = resolve_tenant_id(request, tenant_id)
    evaluations = get_container(request).store.list_evaluations(
        tenant_id,
        dataset_name=dataset_name,
        status=status,
    )
    return [EvaluationJob(**item) for item in evaluations]


@router.get("/evaluations/{evaluation_id}", response_model=EvaluationJob)
def get_evaluation(
    evaluation_id: str,
    request: Request,
    tenant_id: str = "",
    _auth: str | None = Depends(authenticate),
) -> EvaluationJob:
    evaluation = get_container(request).store.get_evaluation(evaluation_id)
    if not evaluation:
        raise HTTPException(status_code=404, detail="evaluation not found")
    resolved_tenant = resolve_tenant_id(
        request,
        tenant_id or evaluation["tenant_id"],
    )
    if evaluation["tenant_id"] != resolved_tenant:
        raise HTTPException(status_code=404, detail="evaluation not found")
    return EvaluationJob(**evaluation)


@router.get("/evaluations/{evaluation_id}/results")
def get_evaluation_results(
    evaluation_id: str,
    request: Request,
    tenant_id: str = "",
    _auth: str | None = Depends(authenticate),
) -> dict:
    evaluation = get_container(request).store.get_evaluation(evaluation_id)
    if not evaluation:
        raise HTTPException(status_code=404, detail="evaluation not found")
    resolved_tenant = resolve_tenant_id(
        request,
        tenant_id or evaluation["tenant_id"],
    )
    if evaluation["tenant_id"] != resolved_tenant:
        raise HTTPException(status_code=404, detail="evaluation not found")
    return {
        "id": evaluation_id,
        "status": evaluation["status"],
        "results": evaluation["results"],
        "error_message": evaluation["error_message"],
    }


@router.get("/evaluations/{evaluation_id}/compare")
def compare_evaluation(
    evaluation_id: str,
    request: Request,
    baseline_id: str | None = None,
    tenant_id: str = "",
    _auth: str | None = Depends(authenticate),
) -> dict:
    store = get_container(request).store
    current = store.get_evaluation(evaluation_id)
    if not current:
        raise HTTPException(status_code=404, detail="evaluation not found")
    resolved_tenant = resolve_tenant_id(
        request,
        tenant_id or current["tenant_id"],
    )
    if current["tenant_id"] != resolved_tenant:
        raise HTTPException(status_code=404, detail="evaluation not found")
    resolved_baseline_id = baseline_id or current.get("baseline_evaluation_id")
    baseline = (
        store.get_evaluation(resolved_baseline_id)
        if resolved_baseline_id
        else None
    )
    if baseline and baseline["tenant_id"] != resolved_tenant:
        raise HTTPException(status_code=404, detail="baseline evaluation not found")
    current_metrics = (current.get("results") or {}).get("metrics", {})
    baseline_metrics = (baseline.get("results") or {}).get("metrics", {}) if baseline else {}
    delta = {
        key: current_metrics[key] - baseline_metrics[key]
        for key in current_metrics
        if key in baseline_metrics
        and isinstance(current_metrics[key], (int, float))
        and isinstance(baseline_metrics[key], (int, float))
    }
    return {
        "evaluation_id": evaluation_id,
        "dataset_name": current["dataset_name"],
        "current": current_metrics,
        "baseline_id": resolved_baseline_id,
        "baseline": baseline_metrics,
        "delta": delta,
    }
