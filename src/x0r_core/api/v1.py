from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile, status
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..deps import ApiKeyDep, SessionDep, WorkspaceDep, require_bootstrap
from ..guard import (
    CallerTokenError,
    PolicyFileError,
    audience_key,
    load_policy_text,
    sign_caller,
    token_key_id,
    verify_caller,
)
from ..models import (
    Agent,
    CallerKey,
    Chunk,
    Document,
    EvalCase,
    EvalSuite,
    PolicyDecision,
    Run,
    Workspace,
    WorkspacePolicy,
)
from ..policy import current_workspace_policy, policy_tool_call_id
from ..schemas import (
    CORE_AUDIENCE,
    AgentIn,
    AgentOut,
    CallerKeyIn,
    CallerKeyOut,
    CallerTokenIn,
    CallerTokenOut,
    DocumentIn,
    DocumentOut,
    EvalRunIn,
    EvalSuiteIn,
    EvalSuiteOut,
    IngestOut,
    PolicyDecisionOut,
    PolicyDecisionsIn,
    PolicyDecisionsStored,
    RunIn,
    RunOut,
    SearchIn,
    SearchOut,
    UsageOut,
    VerifierKeyIn,
    VerifierKeyOut,
    WorkspaceCreate,
    WorkspaceCreated,
    WorkspaceOut,
    WorkspacePolicyIn,
    WorkspacePolicyOut,
)
from ..security import generate_key
from ..services import agents as agent_service
from ..services import evals as eval_service
from ..services import ingest as ingest_service
from ..services import retrieval
from ..services import usage as usage_service

router = APIRouter(prefix="/v1")


def _decision_not_saved_error(exc: agent_service.DecisionNotSaved) -> HTTPException:
    if exc.run_id is None:
        message = f"{exc} No tool ran and the run was not saved."
    else:
        message = (
            f"{exc} The tool for that decision was skipped. Earlier tool calls in this run "
            "may already have run. Inspect GET /v1/runs/{run_id} for saved progress."
        )
    return HTTPException(status_code=503, detail={"message": message, "run_id": exc.run_id})


# ── caller keys and tokens ──────────────────────────────────────────────────

MAX_ACTIVE_CALLER_KEYS = 5


def _server_secret() -> bytes:
    secret = get_settings().caller_key_secret
    if len(secret) < 32:
        raise HTTPException(
            status_code=503,
            detail="Caller keys need X0R_CALLER_KEY_SECRET (at least 32 characters) on the server.",
        )
    return secret.encode("utf-8")


def _root_secret(key: CallerKey) -> bytes:
    """The key's signing root, derived from the server secret. Never stored."""
    label = f"x0r-caller-root|{key.workspace_id}|{key.id}|{key.salt}".encode()
    return hmac.new(_server_secret(), label, hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


async def _lock_caller_keys(session, workspace_id: str) -> None:
    """Serialize key changes per workspace. SQLite already has a single writer."""
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
            {"k": f"caller-keys|{workspace_id}"},
        )


async def _count_active(session, workspace_id: str) -> int:
    return await session.scalar(
        select(func.count()).select_from(CallerKey)
        .where(CallerKey.workspace_id == workspace_id, CallerKey.revoked_at.is_(None))
    )


async def _active_key(session, workspace_id: str, key_id: str) -> CallerKey | None:
    key = await session.get(CallerKey, key_id)
    if key is None or key.workspace_id != workspace_id or key.revoked_at is not None:
        return None
    return key


async def _verify_caller_token(session, workspace_id: str, token: str):
    try:
        key = await _active_key(session, workspace_id, token_key_id(token))
        keys = {key.id: audience_key(_root_secret(key), CORE_AUDIENCE)} if key else {}
        return verify_caller(token, keys=keys, audience=CORE_AUDIENCE)
    except CallerTokenError as exc:
        raise HTTPException(
            status_code=401, detail={"message": str(exc), "reason": exc.reason}
        ) from exc


@router.post("/caller-keys", response_model=CallerKeyOut, status_code=201)
async def create_caller_key(
    payload: CallerKeyIn,
    session: SessionDep,
    workspace: WorkspaceDep,
    idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
):
    """Make a caller key. The first key signs at once. Later keys only verify until you
    activate them, so you can hand their verifier keys to proxies first.

    Send an ``Idempotency-Key`` header to make retries safe: the same value returns the
    same key instead of making another.
    """
    _server_secret()
    await _lock_caller_keys(session, workspace.id)
    if idempotency_key is not None:
        existing = await session.scalar(select(CallerKey).where(
            CallerKey.workspace_id == workspace.id, CallerKey.request_key == idempotency_key))
        if existing is not None:
            return existing
    if await _count_active(session, workspace.id) >= MAX_ACTIVE_CALLER_KEYS:
        raise HTTPException(
            status_code=409,
            detail=f"At most {MAX_ACTIVE_CALLER_KEYS} active caller keys. Revoke one first.",
        )
    has_signing = await session.scalar(select(CallerKey.id).where(
        CallerKey.workspace_id == workspace.id, CallerKey.signing.is_(True),
        CallerKey.revoked_at.is_(None)))
    key = CallerKey(workspace_id=workspace.id, name=payload.name, salt=secrets.token_hex(16),
                    signing=has_signing is None, request_key=idempotency_key)
    session.add(key)
    try:
        await session.flush()
    except IntegrityError:
        # The same Idempotency-Key raced in: return the key the other request made.
        await session.rollback()
        existing = await session.scalar(select(CallerKey).where(
            CallerKey.workspace_id == workspace.id, CallerKey.request_key == idempotency_key))
        if existing is None:
            raise
        return existing
    # Checked again after the insert: with SQLite's single writer, a concurrent create
    # is visible here, so the cap holds without a lock.
    if await _count_active(session, workspace.id) > MAX_ACTIVE_CALLER_KEYS:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"At most {MAX_ACTIVE_CALLER_KEYS} active caller keys. Revoke one first.",
        )
    await session.commit()
    return key


@router.post("/caller-keys/{key_id}/activate", response_model=CallerKeyOut)
async def activate_caller_key(key_id: str, session: SessionDep, workspace: WorkspaceDep):
    """Make this key the one new tokens are signed with. The others keep verifying."""
    await _lock_caller_keys(session, workspace.id)
    key = await _active_key(session, workspace.id, key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="Caller key not found.")
    await session.execute(
        update(CallerKey).where(CallerKey.workspace_id == workspace.id, CallerKey.id != key.id)
        .values(signing=False)
    )
    key.signing = True
    await session.commit()
    await session.refresh(key)
    return key


@router.post("/caller-keys/{key_id}/verifier", response_model=VerifierKeyOut)
async def get_verifier_key(
    key_id: str, payload: VerifierKeyIn, session: SessionDep, workspace: WorkspaceDep
):
    """The key a proxy uses to check tokens for one audience. It can't check or make
    tokens for any other audience, and never for Core."""
    if payload.audience == CORE_AUDIENCE:
        raise HTTPException(status_code=422, detail="Core's own audience key never leaves Core.")
    key = await _active_key(session, workspace.id, key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="Caller key not found.")
    return VerifierKeyOut(key_id=key.id, audience=payload.audience,
                          key=_b64(audience_key(_root_secret(key), payload.audience)))


@router.get("/caller-keys", response_model=list[CallerKeyOut])
async def list_caller_keys(session: SessionDep, workspace: WorkspaceDep):
    rows = await session.execute(
        select(CallerKey).where(CallerKey.workspace_id == workspace.id)
        .order_by(CallerKey.created_at.desc())
    )
    return list(rows.scalars())


@router.delete("/caller-keys/{key_id}", status_code=204)
async def revoke_caller_key(key_id: str, session: SessionDep, workspace: WorkspaceDep):
    """Revoke a key. Core refuses its tokens for every run that starts afterwards. If it
    was the signing key, activate another before minting more tokens."""
    await _lock_caller_keys(session, workspace.id)
    key = await session.get(CallerKey, key_id)
    if key is None or key.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="Caller key not found.")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        key.signing = False
        await session.commit()


@router.post("/caller-tokens", response_model=CallerTokenOut, status_code=201)
async def create_caller_token(
    payload: CallerTokenIn, session: SessionDep, workspace: WorkspaceDep
):
    """Sign a caller with the workspace's signing key, for a proxy or connector to check."""
    key = await session.scalar(select(CallerKey).where(
        CallerKey.workspace_id == workspace.id, CallerKey.signing.is_(True),
        CallerKey.revoked_at.is_(None)))
    if key is None:
        raise HTTPException(
            status_code=409,
            detail="No signing caller key. Create one, or activate one with "
                   "POST /v1/caller-keys/{id}/activate.",
        )
    issued = int(time.time())
    try:
        token = sign_caller(
            payload.caller.to_caller(), key_id=key.id, secret=_root_secret(key),
            audience=payload.audience, ttl=payload.ttl_seconds, now=issued,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CallerTokenOut(
        token=token, key_id=key.id, audience=payload.audience,
        expires_at=datetime.fromtimestamp(issued + payload.ttl_seconds, UTC),
    )


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
        if payload.caller_token is not None:
            caller = await _verify_caller_token(session, workspace.id, payload.caller_token)
        else:
            caller = payload.caller.to_caller() if payload.caller else None
        run = await agent_service.run_agent(
            session, workspace, agent, payload.input, caller=caller,
        )
    except usage_service.QuotaExceeded as exc:
        await session.rollback()
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"X-Quota-Used": str(exc.used), "X-Quota-Limit": str(exc.quota)},
        ) from exc
    except agent_service.DecisionNotSaved as exc:
        raise _decision_not_saved_error(exc) from exc
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


@router.put("/policy", response_model=WorkspacePolicyOut)
async def put_policy(payload: WorkspacePolicyIn, session: SessionDep, workspace: WorkspaceDep):
    try:
        policy = load_policy_text(payload.policy.encode("utf-8"))
    except PolicyFileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    workspace_id = workspace.id
    dialect = session.get_bind().dialect.name
    for attempt in range(3):
        if dialect == "postgresql":
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                {"k": f"workspace-policy|{workspace_id}"},
            )
        elif dialect == "sqlite":
            # Take SQLite's writer lock via the pending API key update before reading.
            await session.flush()
        current = await current_workspace_policy(session, workspace_id)
        if current is not None and current.digest == policy.digest:
            await session.commit()
            return current
        revision = current.revision + 1 if current else 1
        uploaded = WorkspacePolicy(workspace_id=workspace_id, revision=revision,
                                   digest=policy.digest, source=payload.policy)
        session.add(uploaded)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            if attempt == 2:
                raise
        else:
            return uploaded


@router.get("/policy", response_model=WorkspacePolicyOut)
async def get_policy(session: SessionDep, workspace: WorkspaceDep):
    policy = await current_workspace_policy(session, workspace.id)
    if policy is None:
        raise HTTPException(status_code=404, detail="No workspace policy has been uploaded.")
    return policy


@router.post("/policy/decisions", response_model=PolicyDecisionsStored)
async def post_policy_decisions(
    payload: PolicyDecisionsIn, session: SessionDep, workspace: WorkspaceDep,
):
    if not payload.decisions:
        return PolicyDecisionsStored(stored=0, duplicates=0)

    workspace_id = workspace.id
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        await session.flush()

    digests = {item.policy_digest for item in payload.decisions if item.policy_digest}
    revisions = dict((await session.execute(
        select(WorkspacePolicy.digest, func.max(WorkspacePolicy.revision))
        .where(WorkspacePolicy.workspace_id == workspace_id, WorkspacePolicy.digest.in_(digests))
        .group_by(WorkspacePolicy.digest)
    )).all())
    rows = []
    # Acquire conflicting keys in the same order across batches.
    for item in sorted(payload.decisions, key=lambda item: item.decision_id):
        rows.append({
            "workspace_id": workspace_id,
            "source": "sdk",
            "decision_key": str(item.decision_id),
            "tool": item.tool,
            "tool_call_id": policy_tool_call_id(item.tool_call_id) if item.tool_call_id else None,
            "allowed": item.allowed,
            "reason": item.reason,
            "caller_subject": item.caller_subject,
            "policy_version": item.policy_version,
            "policy_digest": item.policy_digest,
            "policy_revision": revisions.get(item.policy_digest),
            "decided_at": item.decided_at,
        })
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    statement = insert(PolicyDecision).values(rows).on_conflict_do_nothing(
        index_elements=["workspace_id", "decision_key"],
    ).returning(PolicyDecision.id)
    stored = len((await session.scalars(statement)).all())
    await session.commit()
    return PolicyDecisionsStored(stored=stored, duplicates=len(rows) - stored)


@router.get("/policy/decisions", response_model=list[PolicyDecisionOut])
async def list_policy_decisions(
    session: SessionDep,
    workspace: WorkspaceDep,
    limit: int = Query(default=50, ge=1, le=200),
    allowed: bool | None = None,
    run_id: str | None = None,
    tool_call_id: str | None = Query(default=None, max_length=4096),
):
    query = select(PolicyDecision).where(PolicyDecision.workspace_id == workspace.id)
    if tool_call_id is not None:
        query = query.where(PolicyDecision.tool_call_id == policy_tool_call_id(tool_call_id))
    if allowed is not None:
        query = query.where(PolicyDecision.allowed == allowed)
    if run_id is not None:
        query = query.where(PolicyDecision.run_id == run_id)
    rows = (
        await session.execute(
            query.order_by(PolicyDecision.created_at.desc(), PolicyDecision.id.desc()).limit(limit)
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
        # Earlier cases may already have consumed tokens. Preserve their runs and meter.
        await session.commit()
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except agent_service.DecisionNotSaved as exc:
        raise _decision_not_saved_error(exc) from exc

    await session.commit()
    return result


# ── usage ────────────────────────────────────────────────────────────────────

@router.get("/usage", response_model=UsageOut)
async def get_usage(session: SessionDep, workspace: WorkspaceDep):
    return await usage_service.summary(session, workspace)
