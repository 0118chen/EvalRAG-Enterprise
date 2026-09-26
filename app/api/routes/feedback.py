from uuid import uuid4

from fastapi import APIRouter, Depends, Request

from app.api.deps import authenticate, get_container, resolve_tenant_id
from app.schemas import FeedbackRequest

router = APIRouter(prefix="/api/v1", tags=["feedback"])


@router.post("/feedback")
def feedback(
    payload: FeedbackRequest,
    request: Request,
    _auth: str | None = Depends(authenticate),
) -> dict[str, str]:
    container = get_container(request)
    tenant_id = resolve_tenant_id(request, payload.tenant_id)
    feedback_id = str(uuid4())
    container.store.save_feedback(
        feedback_id,
        payload.trace_id,
        payload.feedback,
        payload.comment,
        container.settings.rag_version,
        container.settings.prompt_version,
        tenant_id,
    )
    return {
        "status": "accepted",
        "id": feedback_id,
        "trace_id": payload.trace_id,
    }
