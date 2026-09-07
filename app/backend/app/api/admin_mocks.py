"""Admin mock question bank API.

All endpoints require at minimum mock_questions:author permission.
Reviewer and publisher actions are further gated at the service layer.
Unauthorized attempts are logged with action='unauthorized' before 403 is returned.

Mount: /api/admin/mocks  (registered in server.py)
"""
from __future__ import annotations

import logging
from typing import Any, Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from app.core.auth import get_current_user, require_permission
from app.core.permissions import (
    MOCK_QUESTIONS_AUTHOR,
    MOCK_QUESTIONS_REVIEW,
    MOCK_QUESTIONS_PUBLISH,
)
from app.db.supabase_client import get_supabase_admin
from app.admin import mock_questions as svc
from app.admin import mock_import as imp_svc
from app.admin import pyq_mock_projection as proj_svc

logger = logging.getLogger("career_copilot.api.admin_mocks")

router = APIRouter(prefix="/admin/mocks", tags=["admin-mocks"])

# ── Dependency aliases ─────────────────────────────────────────────────────────
require_author    = require_permission(MOCK_QUESTIONS_AUTHOR)
require_reviewer  = require_permission(MOCK_QUESTIONS_REVIEW)
require_publisher = require_permission(MOCK_QUESTIONS_PUBLISH)


def _sb() -> Any:
    return get_supabase_admin()


def _validate_uuid_param(value: str, name: str = "id") -> str:
    """Reject non-UUID path params with 422 before they reach Supabase.

    Without this, a malformed id (e.g. the literal string "undefined" sent by
    a buggy frontend) reaches PostgREST and surfaces as an opaque 500 instead
    of a clear client error.
    """
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"invalid {name}")


def _handle(exc: Exception, context: str = "") -> None:
    """Map service exceptions to HTTP responses."""
    if isinstance(exc, svc.ConflictError):
        raise HTTPException(409, detail=str(exc))
    if isinstance(exc, svc.PermissionError):
        raise HTTPException(403, detail=str(exc))
    if isinstance(exc, LookupError):
        raise HTTPException(404, detail=str(exc))
    if isinstance(exc, ValueError):
        raise HTTPException(422, detail=str(exc))
    logger.exception("admin_mocks unexpected error %s: %s", context, exc)
    raise HTTPException(500, detail=f"internal error{': ' + context if context else ''}")


# ── Pydantic models ────────────────────────────────────────────────────────────

class OptionIn(BaseModel):
    option_text: str
    is_correct: bool = False


class CreateQuestionIn(BaseModel):
    question_text: str
    question_type: str = "mcq"
    difficulty: str = "medium"
    is_conceptual: bool = False
    is_factual: bool = False
    is_current: bool = False
    is_current_based: bool = False
    valid_from: str | None = None
    valid_until: str | None = None
    event_anchor_date: str | None = None
    explanation: str | None = None
    language: str = "en"
    exam_id: str | None = None
    exam_family: str | None = None
    subject_id: str | None = None
    topic_id: str | None = None
    options: list[OptionIn] = Field(..., min_length=2)
    # Provenance fields (PR4) — carry source back to resource/PYQ/CA item.
    source_kind: str | None = None
    source_url: str | None = None
    source_trust: str | None = None
    current_affairs_item_id: str | None = None
    pyq_paper_id: str | None = None
    evidence_text: str | None = None


class UpdateQuestionIn(BaseModel):
    question_text: str | None = None
    question_type: str | None = None
    difficulty: str | None = None
    is_conceptual: bool | None = None
    is_factual: bool | None = None
    is_current: bool | None = None
    is_current_based: bool | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    event_anchor_date: str | None = None
    explanation: str | None = None
    language: str | None = None
    exam_id: str | None = None
    exam_family: str | None = None
    subject_id: str | None = None
    topic_id: str | None = None
    options: list[OptionIn] | None = None
    source_kind: str | None = None
    source_url: str | None = None
    current_affairs_item_id: str | None = None


class TransitionIn(BaseModel):
    notes: str | None = None


class ForceStatusIn(BaseModel):
    to_status: str
    reason: str


class LinkTranslationIn(BaseModel):
    group_id: str | None = None
    partner_question_id: str | None = None


class TopicTagIn(BaseModel):
    topic_id: str
    role: str


class SourceIn(BaseModel):
    source_kind: str = "authored"
    source_trust: str = "unverified"
    source_url: str | None = None
    pyq_paper_id: str | None = None
    pyq_year: int | None = None
    evidence_text: str | None = None


class CommitImportIn(BaseModel):
    import_token: str


class ProjectionSyncIn(BaseModel):
    audit_reason: str = Field(..., min_length=8, max_length=500)
    question_ids: list[str] | None = None


# ── Questions CRUD ─────────────────────────────────────────────────────────────

@router.post("/questions", status_code=201)
def create_question(
    body: CreateQuestionIn,
    actor: dict = Depends(require_author),
):
    """[author] Create a new draft question."""
    try:
        return svc.create_question(_sb(), actor, body.model_dump())
    except Exception as exc:
        _handle(exc, "create_question")


@router.patch("/questions/{question_id}")
def update_question(
    question_id: str,
    body: UpdateQuestionIn,
    actor: dict = Depends(require_author),
    x_override_fingerprint: str | None = Header(default=None, alias="X-Override-Fingerprint"),
):
    """[author] Edit own draft or needs_changes question.
    Publisher may override fingerprint collision via X-Override-Fingerprint: true header.
    """
    question_id = _validate_uuid_param(question_id, "question_id")
    perms = set(actor.get("permissions") or [])
    is_publisher = actor.get("role") == "super_admin" or MOCK_QUESTIONS_PUBLISH in perms
    override = is_publisher and (x_override_fingerprint or "").lower() in ("true", "1")
    try:
        data = {k: v for k, v in body.model_dump().items() if v is not None}
        return svc.update_question(_sb(), actor, question_id, data, override_fingerprint=override)
    except Exception as exc:
        _handle(exc, "update_question")


@router.get("/questions")
def list_questions(
    status: str | None = Query(default=None),
    exam_id: str | None = Query(default=None),
    subject_id: str | None = Query(default=None),
    topic_id: str | None = Query(default=None),
    author_id: str | None = Query(default=None),
    language: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    actor: dict = Depends(require_author),
):
    """[author] List questions with filters. Authors see only their own."""
    try:
        return svc.list_questions(
            _sb(), actor,
            status=status, exam_id=exam_id, subject_id=subject_id,
            topic_id=topic_id, author_id=author_id, language=language,
            page=page, page_size=page_size,
        )
    except Exception as exc:
        _handle(exc, "list_questions")


@router.get("/questions/{question_id}")
def get_question(
    question_id: str,
    actor: dict = Depends(require_author),
):
    """[author] Question detail: row + options + sources + tags + review log."""
    question_id = _validate_uuid_param(question_id, "question_id")
    try:
        return svc.get_question_detail(_sb(), actor, question_id)
    except Exception as exc:
        _handle(exc, "get_question")


# ── State machine ──────────────────────────────────────────────────────────────

@router.post("/questions/{question_id}/submit")
def submit_question(
    question_id: str,
    body: TransitionIn = TransitionIn(),
    actor: dict = Depends(require_author),
):
    """[author] Submit draft / needs_changes → in_review."""
    question_id = _validate_uuid_param(question_id, "question_id")
    try:
        return svc.transition(_sb(), actor, question_id, "submit", notes=body.notes)
    except Exception as exc:
        _handle(exc, "submit")


@router.post("/questions/{question_id}/review")
def review_question(
    question_id: str,
    body: TransitionIn = TransitionIn(),
    actor: dict = Depends(require_reviewer),
):
    """[reviewer] draft → reviewed (pipeline first-pass review).

    Marks the question as reviewed by an admin.  A second reviewer must then
    call /verify to promote it to verified before it is selectable by the
    template selector.
    """
    question_id = _validate_uuid_param(question_id, "question_id")
    try:
        return svc.transition(_sb(), actor, question_id, "review", notes=body.notes)
    except Exception as exc:
        _handle(exc, "review")


@router.post("/questions/{question_id}/verify")
def verify_question(
    question_id: str,
    body: TransitionIn = TransitionIn(),
    actor: dict = Depends(require_reviewer),
):
    """[reviewer] reviewed → verified.

    Promotes a reviewed question to verified, making it selectable by the
    template selector.  Rejects self-verify with 409 (same COI rule as
    approve).
    """
    question_id = _validate_uuid_param(question_id, "question_id")
    try:
        return svc.transition(_sb(), actor, question_id, "verify", notes=body.notes)
    except Exception as exc:
        _handle(exc, "verify")


@router.post("/questions/{question_id}/approve")
def approve_question(
    question_id: str,
    body: TransitionIn = TransitionIn(),
    actor: dict = Depends(require_reviewer),
):
    """[reviewer] in_review → verified. Rejects self-review with 409."""
    question_id = _validate_uuid_param(question_id, "question_id")
    try:
        return svc.transition(_sb(), actor, question_id, "approve", notes=body.notes)
    except Exception as exc:
        _handle(exc, "approve")


@router.post("/questions/{question_id}/request-changes")
def request_changes(
    question_id: str,
    body: TransitionIn = TransitionIn(),
    actor: dict = Depends(require_reviewer),
):
    """[reviewer] in_review → needs_changes."""
    question_id = _validate_uuid_param(question_id, "question_id")
    try:
        return svc.transition(_sb(), actor, question_id, "request_changes", notes=body.notes)
    except Exception as exc:
        _handle(exc, "request_changes")


@router.post("/questions/{question_id}/publish")
def publish_question(
    question_id: str,
    body: TransitionIn = TransitionIn(),
    actor: dict = Depends(require_publisher),
):
    """[publisher] verified → published."""
    question_id = _validate_uuid_param(question_id, "question_id")
    try:
        return svc.transition(_sb(), actor, question_id, "publish", notes=body.notes)
    except Exception as exc:
        _handle(exc, "publish")


@router.post("/questions/{question_id}/archive")
def archive_question(
    question_id: str,
    body: TransitionIn = TransitionIn(),
    actor: dict = Depends(require_publisher),
):
    """[publisher] published → archived."""
    question_id = _validate_uuid_param(question_id, "question_id")
    try:
        return svc.transition(_sb(), actor, question_id, "archive", notes=body.notes)
    except Exception as exc:
        _handle(exc, "archive")


@router.post("/questions/{question_id}/force-status")
def force_status(
    question_id: str,
    body: ForceStatusIn,
    actor: dict = Depends(require_publisher),
):
    """[publisher] Force any status transition, logged with reason."""
    question_id = _validate_uuid_param(question_id, "question_id")
    try:
        return svc.transition(
            _sb(), actor, question_id, "force",
            reason=body.reason, to_status_override=body.to_status
        )
    except Exception as exc:
        _handle(exc, "force_status")


# ── Review queue ───────────────────────────────────────────────────────────────

@router.get("/review-queue")
def review_queue(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    actor: dict = Depends(require_reviewer),
):
    """[reviewer] Questions awaiting review, newest first."""
    try:
        return svc.get_review_queue(_sb(), actor, page=page, page_size=page_size)
    except Exception as exc:
        _handle(exc, "review_queue")


# ── Dedup check ────────────────────────────────────────────────────────────────

@router.post("/questions/{question_id}/dedup-check")
def dedup_check(
    question_id: str,
    threshold: float = Query(default=0.6, ge=0.0, le=1.0),
    actor: dict = Depends(require_author),
):
    """[author] Fingerprint match + top-5 trigram neighbors at given similarity threshold."""
    question_id = _validate_uuid_param(question_id, "question_id")
    try:
        return svc.dedup_check(_sb(), question_id, similarity_threshold=threshold)
    except Exception as exc:
        _handle(exc, "dedup_check")


# ── Bilingual linking ──────────────────────────────────────────────────────────

@router.post("/questions/{question_id}/link-translation")
def link_translation(
    question_id: str,
    body: LinkTranslationIn,
    actor: dict = Depends(require_author),
):
    """[author] Link question to a translation partner in a question group."""
    question_id = _validate_uuid_param(question_id, "question_id")
    try:
        return svc.link_translation(_sb(), actor, question_id, body.model_dump())
    except Exception as exc:
        _handle(exc, "link_translation")


# ── Topic tags ─────────────────────────────────────────────────────────────────

@router.put("/questions/{question_id}/topic-tags")
def set_topic_tags(
    question_id: str,
    tags: list[TopicTagIn],
    actor: dict = Depends(require_author),
):
    """[author] Replace all topic tags for a question."""
    question_id = _validate_uuid_param(question_id, "question_id")
    try:
        return svc.set_topic_tags(_sb(), actor, question_id, [t.model_dump() for t in tags])
    except Exception as exc:
        _handle(exc, "set_topic_tags")


# ── Sources ────────────────────────────────────────────────────────────────────

@router.put("/questions/{question_id}/sources")
def set_sources(
    question_id: str,
    sources: list[SourceIn],
    actor: dict = Depends(require_author),
):
    """[author] Replace all sources for a question."""
    question_id = _validate_uuid_param(question_id, "question_id")
    try:
        return svc.set_sources(_sb(), actor, question_id, [s.model_dump() for s in sources])
    except Exception as exc:
        _handle(exc, "set_sources")


# ── Bulk import ────────────────────────────────────────────────────────────────

# ── PYQ → Mock Bank projection ────────────────────────────────────────────────

@router.get("/pyq-papers/{paper_id}/projection/preview")
def projection_preview(
    paper_id: str,
    actor: dict = Depends(require_author),
):
    """[author] Dry-run: assess which questions in a PYQ paper would project."""
    paper_id = _validate_uuid_param(paper_id, "paper_id")
    try:
        return proj_svc.preview_paper_projection(_sb(), paper_id)
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc))
    except Exception as exc:
        _handle(exc, "projection_preview")


@router.post("/pyq-papers/{paper_id}/projection/sync", status_code=200)
def projection_sync(
    paper_id: str,
    body: ProjectionSyncIn,
    actor: dict = Depends(require_publisher),
):
    """[publisher] Project eligible PYQ questions into mock_question_bank.

    Not atomic: the projection RPC commits per question, so a row that the RPC
    rejects is reported as ``blocked`` and the run continues rather than
    aborting and leaving earlier rows committed with nothing said about which
    row failed. Blocked rows come back as a 422 naming them.
    """
    paper_id = _validate_uuid_param(paper_id, "paper_id")
    actor_id = actor.get("id")
    try:
        result = proj_svc.sync_paper_projection(
            _sb(), paper_id, actor_id,
            audit_reason=body.audit_reason,
            question_ids=body.question_ids,
        )
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc))
    except Exception as exc:
        _handle(exc, "projection_sync")

    # Surface fingerprint conflicts from RPC as 409 (not silent outcome counts).
    conflicts = [q for q in result.get("questions", []) if q.get("outcome") == "conflict"]
    if conflicts:
        raise HTTPException(409, detail={
            "error": "fingerprint_conflict",
            "question_id": conflicts[0].get("question_id"),
            "detail": conflicts[0].get("detail", {}),
        })

    # Rows the RPC rejected. The run completed and every other row was
    # attempted, so the counts are real progress — but the caller must be told
    # WHICH rows failed, which the old sanitised 500 never did.
    blocked = [q for q in result.get("questions", []) if q.get("outcome") == "blocked"]
    if blocked:
        raise HTTPException(422, detail={
            "error": "projection_blocked",
            "question_id": blocked[0].get("question_id"),
            "blocked": [
                {
                    "question_id": b.get("question_id"),
                    "question_number": b.get("question_number"),
                    "reason": b.get("reason"),
                }
                for b in blocked
            ],
            "attempted": result.get("attempted"),
            "outcomes": result.get("outcomes"),
        })

    return result


@router.get("/pyq-papers/{paper_id}/projection/status")
def projection_status(
    paper_id: str,
    actor: dict = Depends(require_author),
):
    """[author] Aggregated projection state for a PYQ paper."""
    paper_id = _validate_uuid_param(paper_id, "paper_id")
    try:
        return proj_svc.get_paper_projection_status(_sb(), paper_id)
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc))
    except Exception as exc:
        _handle(exc, "projection_status")


@router.post("/questions/import/dry-run")
async def import_dry_run(
    file: UploadFile = File(...),
    exam_id: str | None = Query(default=None),
    actor: dict = Depends(require_publisher),
):
    """[publisher] Parse CSV/JSON upload, return per-row preview + import_token."""
    content = await file.read()
    content_type = file.content_type or ""
    try:
        return imp_svc.dry_run(_sb(), actor, content, content_type, exam_id_override=exam_id)
    except Exception as exc:
        _handle(exc, "import_dry_run")


@router.post("/questions/import/commit")
def import_commit(
    body: CommitImportIn,
    actor: dict = Depends(require_publisher),
):
    """[publisher] Commit a dry-run import by token. Idempotent: re-commit skips duplicates."""
    try:
        return imp_svc.commit_import(_sb(), actor, body.import_token)
    except Exception as exc:
        _handle(exc, "import_commit")
