from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import delete, func, select

from ..config import get_settings
from ..deps import ApiKeyDep, SessionDep, WorkspaceDep, require_bootstrap
from ..models import Agent, Chunk, Document, EvalCase, EvalSuite, Run, Workspace
from ..schemas import (
    AgentIn,
    AgentOut,
    DocumentIn,
    DocumentOut,
    EvalRunIn,
    EvalSuiteIn,
    EvalSuiteOut,
    IngestOut,
    RunIn,
    RunOut,
    SearchIn,
    SearchOut,
    UsageOut,
    WorkspaceCreate,
    WorkspaceCreated,
    WorkspaceOut,
)
from ..security import generate_key
from ..services import agents as agent_service
from ..services import evals as eval_service
from ..services import ingest as ingest_service
from ..services import retrieval
from ..services import usage as usage_service

router = APIRouter(prefix="/v1")


# ── workspaces ───────────────────────────────────────────────────────────────

@router.post(
    "/workspaces",
    response_model=WorkspaceCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_workspace(
    payload: WorkspaceCreate,
    session: SessionDep,
    _: Annotated[None, Depends(require_bootstrap)],
):
    raw, prefix, digest = generate_key()
    workspace = Workspace(
        name=payload.name,
        plan=payload.plan,
        monthly_token_quota=payload.monthly_token_quota,
    )
    session.add(workspace)
    await session.flush()

    from ..models import ApiKey

    key = ApiKey(workspace_id=workspace.id, name=payload.key_name, prefix=prefix, key_hash=digest)
    session.add(key)
    await session.commit()
    return WorkspaceCreated(
        workspace=WorkspaceOut.model_validate(workspace),
        api_key=raw,
        key={"id": key.id, "name": key.name, "prefix": key.prefix, "created_at": key.created_at},
    )


@router.get("/me", response_model=WorkspaceOut)
async def me(workspace: WorkspaceDep, key: ApiKeyDep):
    return workspace


# ── documents ────────────────────────────────────────────────────────────────

def _doc_out(doc: Document, chunks: int) -> DocumentOut:
    return DocumentOut(
        id=doc.id,
        title=doc.title,
        source=doc.source,
        mime=doc.mime,
        meta=doc.meta or {},
        created_at=doc.created_at,
        chunk_count=chunks,
    )


@router.post("/documents", response_model=IngestOut, status_code=201)
async def create_document(payload: DocumentIn, session: SessionDep, workspace: WorkspaceDep):
    doc, count = await ingest_service.ingest_document(
        session,
        workspace,
        title=payload.title,
        content=payload.content,
        source=payload.source,
        mime=payload.mime,
        meta=payload.meta,
        embed=payload.embed,
    )
    await session.commit()
    return IngestOut(document=_doc_out(doc, count), chunks=count)


@router.post("/documents/upload", response_model=IngestOut, status_code=201)
async def upload_document(
    session: SessionDep,
    workspace: WorkspaceDep,
    file: Annotated[UploadFile, File()],
    title: str | None = Query(default=None),
):
    settings = get_settings()
    raw = await file.read(settings.max_upload_bytes + 1)
    if len(raw) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="File too large.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=415, detail="Only UTF-8 text files are supported in v0.1."
        ) from None

    doc, count = await ingest_service.ingest_document(
        session,
        workspace,
        title=title or file.filename or "Untitled",
        content=text,
        source=file.filename or "upload",
        mime=file.content_type or "text/plain",
    )
    await session.commit()
    return IngestOut(document=_doc_out(doc, count), chunks=count)


@router.get("/documents", response_model=list[DocumentOut])
async def list_documents(
    session: SessionDep,
    workspace: WorkspaceDep,
    limit: int = Query(default=100, ge=1, le=500),
):
    rows = (
        await session.execute(
            select(Document, func.count(Chunk.id))
            .outerjoin(Chunk, Chunk.document_id == Document.id)
            .where(Document.workspace_id == workspace.id)
            .group_by(Document.id)
            .order_by(Document.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [_doc_out(doc, int(count or 0)) for doc, count in rows]


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(document_id: str, session: SessionDep, workspace: WorkspaceDep):
    doc = await session.get(Document, document_id)
    if doc is None or doc.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="Document not found.")
    await session.execute(delete(Chunk).where(Chunk.document_id == doc.id))
    await session.delete(doc)
    await session.commit()


# ── retrieval ────────────────────────────────────────────────────────────────

@router.post("/search", response_model=SearchOut)
async def search(payload: SearchIn, session: SessionDep, workspace: WorkspaceDep):
    hits = await retrieval.search(session, workspace.id, payload.query, k=payload.k)
    return SearchOut(
        query=payload.query, results=[h.as_dict() for h in hits], count=len(hits)
    )


# ── agents ───────────────────────────────────────────────────────────────────

@router.post("/agents", response_model=AgentOut, status_code=201)
async def create_agent(payload: AgentIn, session: SessionDep, workspace: WorkspaceDep):
    agent = Agent(workspace_id=workspace.id, **payload.model_dump())
    session.add(agent)
    await session.commit()
    return agent


@router.get("/agents", response_model=list[AgentOut])
async def list_agents(session: SessionDep, workspace: WorkspaceDep):
    rows = (
        await session.execute(
            select(Agent)
            .where(Agent.workspace_id == workspace.id)
            .order_by(Agent.created_at.desc())
        )
    ).scalars()
    return list(rows)


@router.get("/agents/{agent_id}", response_model=AgentOut)
async def get_agent(agent_id: str, session: SessionDep, workspace: WorkspaceDep):
    agent = await session.get(Agent, agent_id)
    if agent is None or agent.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="Agent not found.")
    return agent


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, session: SessionDep, workspace: WorkspaceDep):
    agent = await session.get(Agent, agent_id)
    if agent is None or agent.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="Agent not found.")
    await session.delete(agent)
    await session.commit()


@router.post("/agents/{agent_id}/run", response_model=RunOut)
async def run_agent(agent_id: str, payload: RunIn, session: SessionDep, workspace: WorkspaceDep):
    agent = await session.get(Agent, agent_id)
    if agent is None or agent.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="Agent not found.")

    try:
        run = await agent_service.run_agent(session, workspace, agent, payload.input)
    except usage_service.QuotaExceeded as exc:
        await session.rollback()
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"X-Quota-Used": str(exc.used), "X-Quota-Limit": str(exc.quota)},
        ) from exc
    except agent_service.AgentRunError as exc:
        await session.commit()
        raise HTTPException(
            status_code=502,
            detail={"message": str(exc), "run_id": exc.run.id, "status": "failed"},
        ) from exc

    await session.commit()
    return run


# ── runs ─────────────────────────────────────────────────────────────────────

@router.get("/runs", response_model=list[RunOut])
async def list_runs(
    session: SessionDep,
    workspace: WorkspaceDep,
    limit: int = Query(default=50, ge=1, le=200),
):
    rows = (
        await session.execute(
            select(Run)
            .where(Run.workspace_id == workspace.id)
            .order_by(Run.created_at.desc())
            .limit(limit)
        )
    ).scalars()
    return list(rows)


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: str, session: SessionDep, workspace: WorkspaceDep):
    run = await session.get(Run, run_id)
    if run is None or run.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="Run not found.")
    return run


# ── evals ────────────────────────────────────────────────────────────────────

@router.post("/evals", response_model=EvalSuiteOut, status_code=201)
async def create_eval_suite(payload: EvalSuiteIn, session: SessionDep, workspace: WorkspaceDep):
    agent = await session.get(Agent, payload.agent_id)
    if agent is None or agent.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="Agent not found.")

    suite = EvalSuite(workspace_id=workspace.id, agent_id=agent.id, name=payload.name)
    session.add(suite)
    await session.flush()
    for case in payload.cases:
        session.add(EvalCase(suite_id=suite.id, input=case.input, expected=case.expected))
    await session.commit()
    return suite


@router.get("/evals", response_model=list[EvalSuiteOut])
async def list_eval_suites(session: SessionDep, workspace: WorkspaceDep):
    rows = (
        await session.execute(
            select(EvalSuite)
            .where(EvalSuite.workspace_id == workspace.id)
            .order_by(EvalSuite.created_at.desc())
        )
    ).scalars()
    return list(rows)


@router.post("/evals/{suite_id}/run")
async def run_eval_suite(
    suite_id: str, payload: EvalRunIn, session: SessionDep, workspace: WorkspaceDep
):
    suite = await session.get(EvalSuite, suite_id)
    if suite is None or suite.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="Eval suite not found.")

    try:
        result = await eval_service.run_suite(
            session, workspace, suite, threshold=payload.threshold
        )
    except usage_service.QuotaExceeded as exc:
        await session.rollback()
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    await session.commit()
    return result


# ── usage ────────────────────────────────────────────────────────────────────

@router.get("/usage", response_model=UsageOut)
async def get_usage(session: SessionDep, workspace: WorkspaceDep):
    return await usage_service.summary(session, workspace)
