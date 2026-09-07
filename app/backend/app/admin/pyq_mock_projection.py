"""PYQ → Mock Bank projection service.

Thin Python orchestration layer that wraps the SECURITY DEFINER RPC
``project_pyq_question_to_mock_bank`` (migration 183).  All eligibility
checks and the atomic upsert live in the DB function; this module provides:

  - preview_paper_projection()   — dry-run: which questions would sync
  - sync_paper_projection()      — call the RPC per eligible question
  - get_paper_projection_status() — aggregated projection state for a paper

The RPC is the single source of truth.  Python never directly writes to
``pyq_mock_question_projections`` or ``mock_question_bank``.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger("career_copilot.admin.pyq_mock_projection")


# ── Eligibility constants (mirrors the RPC — used for preview dry-run) ────────

_ELIGIBLE_Q_TYPES     = frozenset({"mcq"})
_VERIFIED_PAPER       = "verified"
_VERIFIED_QUESTION    = "verified"
_VERIFIED_OPTION      = "verified"
_VERIFIED_TAG         = "verified"
_VERIFIED_STIMULUS    = "verified"
_PRIMARY_TAG_ROLE     = "primary"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _short_label(question_text: str | None, *, limit: int = 80) -> str:
    """A trimmed, single-line question label for operator display (EI-CLEAN-04).

    Empty/whitespace text yields "" (the frontend falls back to a short id).
    """
    text = " ".join((question_text or "").split())
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fetch_paper(sb: Any, paper_id: str) -> dict | None:
    rows = (
        sb.table("pyq_papers")
        .select("id, exam_id, year, trust_status, source_url, source_type, source_document_id")
        .eq("id", paper_id)
        .limit(1)
        .execute()
        .data
    ) or []
    return rows[0] if rows else None


def _fetch_paper_questions(sb: Any, paper_id: str) -> list[dict]:
    return (
        sb.table("pyq_questions")
        .select(
            "id, pyq_paper_id, question_text, question_type, reviewer_status, "
            "correct_option_id, observed_difficulty, expected_solve_time_sec, "
            "explanation_text, language, section_id"
        )
        .eq("pyq_paper_id", paper_id)
        # Deterministic order. Without it PostgREST returns heap order, so a
        # sync that dies partway leaves a set of completed rows that cannot be
        # read as a prefix of anything — the failing row is then invisible from
        # the client side. Ordering also makes a re-run resume-comparable.
        .order("question_number")
        .order("id")
        .execute()
        .data
    ) or []


def _fetch_options_for_question(sb: Any, question_id: str) -> list[dict]:
    return (
        sb.table("pyq_options")
        .select(
            "id, question_id, option_text, option_label, is_correct, "
            "reviewer_status, source_label, display_order"
        )
        .eq("question_id", question_id)
        .execute()
        .data
    ) or []


def _fetch_question_stimuli(sb: Any, question_id: str) -> list[dict]:
    """Fetch a question's shared-stimulus links joined to their stimuli.

    Returns ALL links for the question (regardless of trust) so the eligibility
    gate can detect an unverified link/stimulus, each combined dict carrying
    both the link's and the stimulus's reviewer_status plus the snapshot fields.
    compute_content_hash / the projection snapshot filter to verified-verified.

    Mirrors the SQL join in project_pyq_question_to_mock_bank (migration 229).
    """
    links = (
        sb.table("pyq_question_stimuli")
        .select("id, question_id, stimulus_id, display_order, reviewer_status")
        .eq("question_id", question_id)
        .execute()
        .data
    ) or []
    if not links:
        return []

    stim_ids = [l.get("stimulus_id") for l in links if l.get("stimulus_id")]
    stimuli = (
        (
            sb.table("pyq_stimuli")
            .select("id, stimulus_type, content_text, language, display_order, reviewer_status")
            .in_("id", stim_ids)
            .execute()
            .data
        )
        or []
    ) if stim_ids else []
    stim_map = {s.get("id"): s for s in stimuli}

    combined: list[dict] = []
    for l in links:
        s = stim_map.get(l.get("stimulus_id"), {})
        combined.append({
            "stimulus_id": l.get("stimulus_id"),
            "link_reviewer_status": l.get("reviewer_status"),
            "link_display_order": l.get("display_order"),
            "stimulus_reviewer_status": s.get("reviewer_status"),
            "stimulus_type": s.get("stimulus_type"),
            "content_text": s.get("content_text"),
            "language": s.get("language"),
            "stimulus_display_order": s.get("display_order"),
        })
    return combined


def _fetch_primary_tags(sb: Any, question_id: str) -> list[dict]:
    return (
        sb.table("pyq_question_topic_tags")
        .select("id, question_id, topic_id, tag_role, reviewer_status")
        .eq("question_id", question_id)
        .eq("tag_role", _PRIMARY_TAG_ROLE)
        .execute()
        .data
    ) or []


def _fetch_all_verified_tags(sb: Any, question_id: str) -> list[dict]:
    return (
        sb.table("pyq_question_topic_tags")
        .select("id, question_id, topic_id, tag_role, reviewer_status")
        .eq("question_id", question_id)
        .eq("reviewer_status", _VERIFIED_TAG)
        .execute()
        .data
    ) or []


def _fetch_topic_row(sb: Any, topic_id: str | None) -> dict | None:
    """The ``topics`` row for a tag, or ``None``. ``parent_topic_id`` is what
    tells a microtopic from a top-level topic (migration 268)."""
    if not topic_id:
        return None
    rows = (
        sb.table("topics")
        .select("id, parent_topic_id, subject_id, is_active")
        .eq("id", topic_id)
        .limit(1)
        .execute()
        .data
    ) or []
    return rows[0] if rows else None


def _verified_primary_topic_id(primary_tags: list[dict]) -> str | None:
    """The topic id of the single verified primary tag, or ``None``."""
    verified_primary = [
        t for t in primary_tags
        if t.get("reviewer_status") == _VERIFIED_TAG
        and t.get("tag_role") == _PRIMARY_TAG_ROLE
    ]
    if len(verified_primary) != 1:
        return None
    return verified_primary[0].get("topic_id")


def resolve_primary_topic_split(
    primary_tags: list[dict],
    topic_row: dict | None,
) -> tuple[str | None, str | None]:
    """Split the single verified primary tag into ``(topic_id, microtopic_id)``.

    Mirrors the level resolution in ``project_pyq_question_to_mock_bank``
    (migration 268): ``topics.parent_topic_id IS NULL`` means the tag is a
    top-level topic (microtopic_id stays NULL); otherwise the tag IS the
    microtopic and its parent becomes the topic.

    Returns ``(None, None)`` when there is not exactly one verified primary tag,
    or when its topic row is missing — the RPC blocks those cases before it ever
    reaches the projection write, so there is nothing to hash.
    """
    verified_primary = [
        t for t in primary_tags
        if t.get("reviewer_status") == _VERIFIED_TAG
        and t.get("tag_role") == _PRIMARY_TAG_ROLE
    ]
    if len(verified_primary) != 1 or not topic_row:
        return None, None

    tag_topic_id = verified_primary[0].get("topic_id")
    parent_id = topic_row.get("parent_topic_id")
    if parent_id:
        return parent_id, tag_topic_id
    return tag_topic_id, None


def _fetch_existing_projection(sb: Any, question_id: str) -> dict | None:
    rows = (
        sb.table("pyq_mock_question_projections")
        .select("pyq_question_id, mock_question_id, sync_status, source_content_hash, projected_at, updated_at")
        .eq("pyq_question_id", question_id)
        .limit(1)
        .execute()
        .data
    ) or []
    return rows[0] if rows else None


def compute_content_hash(
    question: dict,
    options: list[dict],
    paper: dict | None = None,
    all_verified_tags: list[dict] | None = None,
    stimuli: list[dict] | None = None,
    primary_microtopic_id: str | None = None,
) -> str:
    """Stable SHA-256 hash of ALL fields projected to mock_question_bank.

    Mirrors the hash computed inside ``project_pyq_question_to_mock_bank``
    (latest authoritative body: migration 268, which appends the resolved
    microtopic_id; separator posture from 239, base formula from migration 183
    Section D + 229 PR-4 additions).  Keep in sync when the RPC hash formula
    changes.

    Formula (GS = \\x1d between top-level fields, FS = \\x1f between items in a
    list, RS = \\x1e within item):
        q_text GS explanation GS difficulty GS language GS expected_time_sec
        GS paper_id GS paper_year
        GS verified_opt_label RS opt_text (joined by FS, sorted by label then id)
        GS correct_opt_text
        GS verified_tag_topic_id RS tag_role (joined by FS, sorted by topic_id then role)
        GS section_id GS opt_source_label RS opt_display_order (joined by FS)
        GS stimulus_type RS content_text RS language RS link_display_order (joined by FS)
        GS resolved_microtopic_id

    All projected fields are included so that changing explanation, difficulty,
    language, expected time, paper year, option ordering (label), or any verified
    topic tag all produce a different hash — causing preview to report "would_update"
    and sync to re-project the row.

    Separator note: the top-level field separator is the ASCII Group Separator
    (\\x1d / chr(29)), NOT NUL (\\x00 / chr(0)). PostgreSQL ``text`` cannot hold a
    null byte — ``chr(0)`` raises "null character not permitted" — so the mirrored
    SQL hash in ``project_pyq_question_to_mock_bank`` crashes the whole projection
    RPC (and thus sync) if a NUL separator is used. The GS/FS/RS trio are all
    non-null control characters that never occur in the projected text fields, so
    they remain unambiguous separators while keeping the SQL hashable as bytea.
    """
    GS, FS, RS = "\x1d", "\x1f", "\x1e"

    q_text       = (question.get("question_text") or "").strip().lower()
    expl         = (question.get("explanation_text") or "").strip().lower()
    raw_diff     = (question.get("observed_difficulty") or "").strip().lower()
    diff         = raw_diff if raw_diff in ("easy", "medium", "hard") else "medium"
    _lang_raw = (question.get("language") or "").strip()
    language  = (_lang_raw or "en").lower()
    _time     = question.get("expected_solve_time_sec")
    exp_time  = "" if _time is None else str(_time)
    paper_id     = str(question.get("pyq_paper_id") or "")
    _p           = paper or {}
    paper_year   = str(_p.get("year") or "")
    paper_exam   = str(_p.get("exam_id") or "")
    paper_src_url  = str(_p.get("source_url") or "")
    paper_src_type = str(_p.get("source_type") or "")
    paper_src_doc_id = str(_p.get("source_document_id") or "")

    verified_opts = sorted(
        (o for o in options if o.get("reviewer_status") == _VERIFIED_OPTION),
        key=lambda o: ((o.get("option_label") or "").lower(), o.get("id") or ""),
    )
    opt_parts = FS.join(
        (o.get("option_label") or "").lower() + RS + (o.get("option_text") or "").strip().lower()
        for o in verified_opts
    )
    correct_opt = next(
        ((o.get("option_text") or "").strip().lower() for o in verified_opts if o.get("is_correct")),
        "",
    )

    v_tags = sorted(
        (t for t in (all_verified_tags or []) if t.get("reviewer_status") == _VERIFIED_TAG),
        key=lambda t: (t.get("topic_id") or "", t.get("tag_role") or ""),
    )
    tag_parts = FS.join(
        (t.get("topic_id") or "") + RS + (t.get("tag_role") or "")
        for t in v_tags
    )

    # ── PR-4 (migration 229) appended fields — lockstep with the SQL hash ─────
    # Appended AFTER the existing fields (existing order preserved) so already-
    # projected rows only re-hash on genuinely new data: section_id, then per-
    # verified-option source_label+display_order (same verified-option ordering),
    # then per-verified-stimulus type+content+language+link display_order.
    section_id = str(question.get("section_id") or "")

    opt_meta_parts = FS.join(
        (o.get("source_label") or "")
        + RS
        + ("" if o.get("display_order") is None else str(o.get("display_order")))
        for o in verified_opts
    )

    def _nulls_last(v: Any) -> tuple[int, Any]:
        return (1, 0) if v is None else (0, v)

    verified_stims = sorted(
        (
            s for s in (stimuli or [])
            if s.get("link_reviewer_status") == _VERIFIED_STIMULUS
            and s.get("stimulus_reviewer_status") == _VERIFIED_STIMULUS
        ),
        key=lambda s: (
            _nulls_last(s.get("link_display_order")),
            _nulls_last(s.get("stimulus_display_order")),
            s.get("stimulus_id") or "",
        ),
    )
    stim_parts = FS.join(
        (s.get("stimulus_type") or "")
        + RS + (s.get("content_text") or "")
        + RS + (s.get("language") or "")
        + RS + ("" if s.get("link_display_order") is None else str(s.get("link_display_order")))
        for s in verified_stims
    )

    # ── Migration 268 appended field — lockstep with the SQL hash ─────────────
    # The resolved microtopic_id (NULL/'' when the primary tag is a top-level
    # topic), appended AFTER every existing field so the hash commits to every
    # value written to mock_question_bank. (The tag aggregate above already
    # carried the tag's own topic_id, so a tag MOVE was detected before 268;
    # what this adds is that the written microtopic is itself hashed. A
    # topics-tree re-parent remains undetected — see the 268 header.)
    # Appending a field appends a GS separator, so already-projected rows do NOT
    # reproduce their pre-268 hash even when the microtopic is NULL — that is a
    # one-time re-projection, documented in the migration header.
    microtopic_id = str(primary_microtopic_id or "")

    raw = GS.join([
        q_text, expl, diff, language, exp_time, paper_id,
        paper_year, paper_exam, paper_src_url, paper_src_type, paper_src_doc_id,
        opt_parts, correct_opt, tag_parts,
        section_id, opt_meta_parts, stim_parts,
        microtopic_id,
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _check_question_eligibility(
    paper: dict,
    question: dict,
    options: list[dict],
    primary_tags: list[dict],
    stimuli: list[dict] | None = None,
) -> tuple[bool, str]:
    """Return (eligible, reason) for a single question.

    Mirrors the eligibility checks in the SECURITY DEFINER RPC.
    """
    if paper.get("trust_status") != _VERIFIED_PAPER:
        return False, f"paper_not_verified:{paper.get('trust_status')}"

    if question.get("reviewer_status") != _VERIFIED_QUESTION:
        return False, f"question_not_verified:{question.get('reviewer_status')}"

    if question.get("question_type") not in _ELIGIBLE_Q_TYPES:
        return False, f"not_mcq:{question.get('question_type')}"

    if not (question.get("question_text") or "").strip():
        return False, "empty_question_text"

    verified_options = [o for o in options if o.get("reviewer_status") == _VERIFIED_OPTION]
    if len(verified_options) < 2:
        return False, f"too_few_verified_options:{len(verified_options)}"

    empty_text = [
        o for o in verified_options
        if not (o.get("option_text") or "").strip()
    ]
    if empty_text:
        return False, f"empty_verified_option_text:{len(empty_text)}"

    correct_options = [o for o in verified_options if o.get("is_correct")]
    if len(correct_options) != 1:
        return False, f"not_exactly_one_correct:{len(correct_options)}"

    correct_id = question.get("correct_option_id")
    if correct_id is not None and correct_options[0].get("id") != correct_id:
        return False, f"correct_option_id_mismatch:{correct_id}"

    verified_primary = [
        t for t in primary_tags
        if t.get("reviewer_status") == _VERIFIED_TAG
        and t.get("tag_role") == _PRIMARY_TAG_ROLE
    ]
    if len(verified_primary) != 1:
        return False, f"not_exactly_one_verified_primary_tag:{len(verified_primary)}"

    # Stimulus verification gate (PR-4, conjunctive trust): if the question has
    # any shared-stimulus link, EVERY link AND its referenced stimulus must be
    # verified. A question with no links is unaffected (still projectable).
    if stimuli:
        all_verified = all(
            s.get("link_reviewer_status") == _VERIFIED_STIMULUS
            and s.get("stimulus_reviewer_status") == _VERIFIED_STIMULUS
            for s in stimuli
        )
        if not all_verified:
            return False, "stimulus_not_verified"

    return True, "eligible"


# ── Public API ─────────────────────────────────────────────────────────────────

def preview_paper_projection(sb: Any, paper_id: str) -> dict:
    """Dry-run: assess projection eligibility for every question in a paper.

    Makes no writes.  Returns a per-question breakdown so the operator can
    see exactly which questions would project and which are blocked and why.

    Returns:
        {
          "paper_id": str,
          "paper": {id, exam_id, year, trust_status},
          "total": int,
          "eligible_count": int,
          "ineligible_count": int,
          "already_projected_count": int,
          "would_update_count": int,
          "would_create_count": int,
          "questions": [{question_id, eligible, reason, existing_projection, content_hash}]
        }
    """
    paper = _fetch_paper(sb, paper_id)
    if paper is None:
        raise LookupError(f"pyq paper {paper_id!r} not found")

    questions = _fetch_paper_questions(sb, paper_id)

    results: list[dict] = []
    eligible_count = 0
    already_projected = 0
    would_update = 0
    would_create = 0

    for q in questions:
        qid = q["id"]
        options    = _fetch_options_for_question(sb, qid)
        p_tags     = _fetch_primary_tags(sb, qid)
        all_tags   = _fetch_all_verified_tags(sb, qid)
        stimuli    = _fetch_question_stimuli(sb, qid)
        projection = _fetch_existing_projection(sb, qid)

        eligible, reason = _check_question_eligibility(paper, q, options, p_tags, stimuli)
        # Migration 268: the hash carries the resolved microtopic, so the preview
        # must resolve it the same way or every microtopic-tagged row would show
        # a permanent false "would_update".
        _, microtopic_id = (
            resolve_primary_topic_split(
                p_tags,
                _fetch_topic_row(sb, _verified_primary_topic_id(p_tags)),
            )
            if eligible else (None, None)
        )
        content_hash = (
            compute_content_hash(
                q, options, paper=paper, all_verified_tags=all_tags, stimuli=stimuli,
                primary_microtopic_id=microtopic_id,
            )
            if eligible else None
        )

        entry: dict = {
            "question_id": qid,
            # EI-CLEAN-04: readable row identity so the operator UI can show the
            # question text instead of a truncated UUID. Trimmed to a short label.
            "label": _short_label(q.get("question_text")),
            "eligible": eligible,
            "reason": reason,
            "existing_projection": projection,
            "content_hash": content_hash,
        }

        if eligible:
            eligible_count += 1
            if projection:
                already_projected += 1
                # Mark would_update when the hash changed (content drift) OR when
                # the projection is not active (e.g. stale/blocked from a paper-level
                # field change that doesn't affect the hash itself).  A stale
                # projection with a matching hash will still be re-projected by the
                # RPC to restore active status.
                if (projection.get("sync_status") != "active"
                        or projection.get("source_content_hash") != content_hash):
                    entry["would_update"] = True
                    would_update += 1
                else:
                    entry["would_update"] = False
            else:
                would_create += 1
        results.append(entry)

    return {
        "paper_id": paper_id,
        "paper": paper,
        "total": len(questions),
        "eligible_count": eligible_count,
        "ineligible_count": len(questions) - eligible_count,
        "already_projected_count": already_projected,
        "would_update_count": would_update,
        "would_create_count": would_create,
        "questions": results,
    }


def _blocked_reason(exc: Exception) -> str:
    """A short, stable reason string for a row the projection RPC rejected.

    The full text goes to the log and to ``detail.error``; this is the part the
    operator acts on.
    """
    text = str(exc)
    low = text.lower()
    if "invalid input syntax for type bytea" in low:
        return ("content_hash_bytea_cast: projected text contains a backslash "
                "escape the hash cast cannot parse")
    if "null character not permitted" in low:
        return "content_hash_null_byte: projected text contains a NUL byte"
    if "duplicate key" in low or "unique constraint" in low:
        return f"unique_violation: {text[:200]}"
    return f"rpc_error: {text[:200]}"


def sync_paper_projection(
    sb: Any,
    paper_id: str,
    actor_id: str,
    *,
    audit_reason: str = "admin_sync",
    question_ids: list[str] | None = None,
) -> dict:
    """Call ``project_pyq_question_to_mock_bank`` for eligible questions.

    When ``question_ids`` is given, only those questions are synced (must
    belong to the paper).  Otherwise all questions in the paper are attempted.

    Returns:
        {
          "paper_id": str,
          "attempted": int,
          "outcomes": {
            "unchanged": int,
            "updated": int,
            "created": int,
            "ineligible": int,
            "error": int,
          },
          "questions": [{question_id, outcome, mock_question_id, detail}]
        }
    """
    paper = _fetch_paper(sb, paper_id)
    if paper is None:
        raise LookupError(f"pyq paper {paper_id!r} not found")

    questions = _fetch_paper_questions(sb, paper_id)
    if question_ids is not None:
        requested = set(question_ids)
        # Validate all requested IDs belong to this paper
        paper_qids = {q["id"] for q in questions}
        foreign = requested - paper_qids
        if foreign:
            raise ValueError(
                f"question_ids not in paper {paper_id!r}: {sorted(foreign)}"
            )
        questions = [q for q in questions if q["id"] in requested]

    results: list[dict] = []
    outcome_counts: dict[str, int] = {
        "unchanged": 0, "updated": 0, "created": 0, "ineligible": 0,
        "error": 0, "blocked": 0,
    }

    for q in questions:
        qid = q["id"]
        try:
            rpc_result = (
                sb.rpc(
                    "project_pyq_question_to_mock_bank",
                    {
                        "p_pyq_question_id": qid,
                        "p_actor_id": actor_id,
                        "p_audit_reason": audit_reason,
                    },
                )
                .execute()
                .data
            )
        except Exception as exc:  # noqa: BLE001
            # One unprojectable row must not abort the run. Each RPC call is its
            # own transaction, so raising here would leave every earlier row
            # committed with nothing recorded about which row failed — the
            # partial-write bug. Mark the row blocked with the reason and carry
            # on, matching how the eligibility gate reports `ineligible`.
            logger.warning(
                "projection blocked for pyq question %s on paper %s: %s",
                qid, paper_id, exc,
            )
            outcome_counts["blocked"] = outcome_counts.get("blocked", 0) + 1
            results.append({
                "question_id": qid,
                "question_number": q.get("question_number"),
                "label": _short_label(q.get("question_text")),
                "outcome": "blocked",
                "mock_question_id": None,
                "reason": _blocked_reason(exc),
                "detail": {"error": str(exc)[:500]},
            })
            continue

        # RPC returns a JSONB record or list-of-one
        result_data: dict = {}
        if isinstance(rpc_result, list) and rpc_result:
            result_data = rpc_result[0] or {}
        elif isinstance(rpc_result, dict):
            result_data = rpc_result

        outcome = result_data.get("outcome", "error")
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
        results.append({
            "question_id": qid,
            "question_number": q.get("question_number"),
            # EI-CLEAN-04: same readable label as preview so sync-result rows show
            # the question text, not a truncated UUID.
            "label": _short_label(q.get("question_text")),
            "outcome": outcome,
            "mock_question_id": result_data.get("mock_question_id"),
            "detail": result_data,
        })

    return {
        "paper_id": paper_id,
        "attempted": len(questions),
        "outcomes": outcome_counts,
        "questions": results,
    }


def get_paper_projection_status(sb: Any, paper_id: str) -> dict:
    """Aggregated projection state for a paper.

    Returns counts by ``sync_status`` and lists any stale/blocked projections
    so the operator can see what needs attention without running a full preview.

    Returns:
        {
          "paper_id": str,
          "paper": {id, exam_id, year, trust_status},
          "total_questions": int,
          "projection_counts": {"active": N, "stale": N, "blocked": N, "archived": N},
          "unprojected_count": int,
          "stale_projections": [{pyq_question_id, mock_question_id, sync_status, updated_at}]
        }
    """
    paper = _fetch_paper(sb, paper_id)
    if paper is None:
        raise LookupError(f"pyq paper {paper_id!r} not found")

    questions = _fetch_paper_questions(sb, paper_id)
    question_ids = [q["id"] for q in questions]

    projections: list[dict] = []
    if question_ids:
        projections = (
            sb.table("pyq_mock_question_projections")
            .select("pyq_question_id, mock_question_id, sync_status, updated_at, last_sync_result")
            .in_("pyq_question_id", question_ids)
            .execute()
            .data
        ) or []

    projection_map = {p["pyq_question_id"]: p for p in projections}
    counts: dict[str, int] = {"active": 0, "stale": 0, "blocked": 0, "archived": 0}
    for p in projections:
        status = p.get("sync_status", "unknown")
        counts[status] = counts.get(status, 0) + 1

    unprojected = len(question_ids) - len(projections)
    stale = [p for p in projections if p.get("sync_status") in ("stale", "blocked")]

    return {
        "paper_id": paper_id,
        "paper": paper,
        "total_questions": len(questions),
        "projection_counts": counts,
        "unprojected_count": unprojected,
        "stale_projections": stale,
    }
