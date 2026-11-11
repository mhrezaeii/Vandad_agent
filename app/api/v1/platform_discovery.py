"""Public-source and authorised browser discovery endpoints."""
from __future__ import annotations
import json
import uuid
from typing import Annotated
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from app.api.deps import company_scope, pagination_params
from app.core.exceptions import NotFoundError, UnprocessableEntityError
from app.models import Platform, PlatformConnection
from app.schemas.platform_discovery import PlatformDiscoveryRequest, PlatformSourceListResponse, PlatformSourceRead
from app.schemas.platform_knowledge import PlatformKnowledgeExtractRequest, PlatformKnowledgeExtractResponse
from app.security.authorization import CompanyScope
from app.services.credential_service import CredentialService
from app.services.platform_discovery_service import PlatformDiscoveryService
from app.services.platform_knowledge_service import PlatformKnowledgeExtractionError, PlatformKnowledgeService

router = APIRouter(prefix="/platforms", tags=["platform-discovery"])

@router.post("/{platform_id}/discover")
async def discover_platform(platform_id: uuid.UUID, payload: PlatformDiscoveryRequest,
    request: Request, scope: Annotated[CompanyScope, Depends(company_scope)]):
    async with request.app.state.session_factory() as session:
        platform = await session.scalar(select(Platform).where(
            Platform.id == platform_id, Platform.company_id == scope.company_id))
        if platform is None:
            raise NotFoundError("Platform not found.")
        connection, credentials = None, None
        if payload.mode == "browser":
            if payload.connection_id is None:
                raise UnprocessableEntityError("connection_id is required for browser mode.")
            connection = await session.scalar(select(PlatformConnection).where(
                PlatformConnection.id == payload.connection_id,
                PlatformConnection.platform_id == platform_id,
                PlatformConnection.company_id == scope.company_id))
            if connection is None:
                raise NotFoundError("Platform connection not found.")
            if connection.status not in {"connected", "degraded"}:
                raise UnprocessableEntityError("Browser connection must be validated before use.")
            if connection.credential_reference_id:
                resolved = await CredentialService(session, request.app.state.master_key_box).resolve_credential(
                    connection.credential_reference_id, company_id=scope.company_id)
                if resolved.payload is not None:
                    try:
                        credentials = json.loads(resolved.payload.reveal())
                    except (ValueError, TypeError) as exc:
                        raise UnprocessableEntityError(
                            "Browser credential must contain JSON username/password fields.") from exc
        try:
            result = await PlatformDiscoveryService(session).discover(platform, mode=payload.mode,
                max_pages=payload.max_pages, connection=connection, credentials=credentials)
            await session.commit()
            return result
        except (ValueError, RuntimeError) as exc:
            await session.rollback()
            raise UnprocessableEntityError(str(exc)) from exc
        finally:
            credentials = None

@router.get("/{platform_id}/sources", response_model=PlatformSourceListResponse)
async def list_platform_sources(platform_id: uuid.UUID, request: Request,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    pagination: Annotated[tuple[int, int], Depends(pagination_params)]):
    limit, offset = pagination
    async with request.app.state.session_factory() as session:
        platform = await session.scalar(select(Platform).where(
            Platform.id == platform_id, Platform.company_id == scope.company_id))
        if platform is None:
            raise NotFoundError("Platform not found.")
        rows, total = await PlatformDiscoveryService(session).list_sources(
            company_id=scope.company_id, platform_id=platform_id, limit=limit, offset=offset)
        return PlatformSourceListResponse(items=[PlatformSourceRead.model_validate(x) for x in rows],
            total=total, limit=limit, offset=offset)


@router.post("/{platform_id}/knowledge/extract", response_model=PlatformKnowledgeExtractResponse)
async def extract_platform_knowledge(
    platform_id: uuid.UUID,
    payload: PlatformKnowledgeExtractRequest,
    request: Request,
    scope: Annotated[CompanyScope, Depends(company_scope)],
):
    """Extract structured, evidence-backed knowledge from the latest discovered pages."""
    async with request.app.state.session_factory() as session:
        platform = await session.scalar(select(Platform).where(
            Platform.id == platform_id, Platform.company_id == scope.company_id
        ))
        if platform is None:
            raise NotFoundError("Platform not found.")
        try:
            snapshot = await PlatformKnowledgeService(
                session,
                request.app.state.provider_registry,
                request.app.state.provider_executor,
                request.app.state.master_key_box,
            ).extract(
                platform=platform,
                connection_id=payload.connection_id,
                max_documents=payload.max_documents,
            )
            await session.commit()
            return PlatformKnowledgeExtractResponse(
                id=snapshot.id,
                platform_id=snapshot.platform_id,
                source_document_count=len(snapshot.source_document_ids),
                source_fingerprint=snapshot.source_fingerprint,
                model=snapshot.model,
                extracted_at=snapshot.extracted_at,
                knowledge=snapshot.knowledge_json,
            )
        except PlatformKnowledgeExtractionError as exc:
            await session.rollback()
            raise UnprocessableEntityError(
                exc.message, details=[{"code": exc.code}]
            ) from exc
        except Exception:
            await session.rollback()
            raise


@router.get("/{platform_id}/knowledge/latest")
async def get_latest_platform_knowledge(
    platform_id: uuid.UUID,
    request: Request,
    scope: Annotated[CompanyScope, Depends(company_scope)],
):
    """Return the latest tenant-scoped knowledge snapshot, if one exists."""
    from app.models import PlatformKnowledgeSnapshot  # noqa: PLC0415

    async with request.app.state.session_factory() as session:
        platform = await session.scalar(select(Platform).where(
            Platform.id == platform_id, Platform.company_id == scope.company_id
        ))
        if platform is None:
            raise NotFoundError("Platform not found.")
        snapshot = await session.scalar(
            select(PlatformKnowledgeSnapshot).where(
                PlatformKnowledgeSnapshot.company_id == scope.company_id,
                PlatformKnowledgeSnapshot.platform_id == platform_id,
            ).order_by(PlatformKnowledgeSnapshot.extracted_at.desc()).limit(1)
        )
        if snapshot is None:
            raise NotFoundError("Platform knowledge has not been extracted yet.")
        return PlatformKnowledgeExtractResponse(
            id=snapshot.id,
            platform_id=snapshot.platform_id,
            source_document_count=len(snapshot.source_document_ids),
            source_fingerprint=snapshot.source_fingerprint,
            model=snapshot.model,
            extracted_at=snapshot.extracted_at,
            knowledge=snapshot.knowledge_json,
        )
