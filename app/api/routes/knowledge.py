from uuid import uuid4

from fastapi import APIRouter, Depends, Request

from app.api.deps import authenticate, get_container, resolve_tenant_id
from app.schemas import KnowledgeBase, KnowledgeBaseCreate

router = APIRouter(prefix="/api/v1", tags=["knowledge-bases"])


@router.post(
    "/knowledge-bases",
    response_model=KnowledgeBase,
    status_code=201,
)
def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    request: Request,
    tenant_id: str = "",
    _auth: str | None = Depends(authenticate),
) -> KnowledgeBase:
    container = get_container(request)
    resolved_tenant = resolve_tenant_id(request, tenant_id)
    knowledge_base = KnowledgeBase(
        id=str(uuid4()),
        tenant_id=resolved_tenant,
        **payload.model_dump(),
    )
    container.store.save_knowledge_base(knowledge_base)
    return knowledge_base


@router.get("/knowledge-bases", response_model=list[KnowledgeBase])
def list_knowledge_bases(
    request: Request,
    tenant_id: str = "",
    _auth: str | None = Depends(authenticate),
) -> list[KnowledgeBase]:
    resolved_tenant = resolve_tenant_id(request, tenant_id)
    return get_container(request).store.list_knowledge_bases(resolved_tenant)
