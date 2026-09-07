"""Tests for PYQ → Mock Bank projection service (migration 183).

Covers:
  - compute_content_hash (pure unit)
  - _check_question_eligibility (all disqualifying paths)
  - preview_paper_projection (ineligible, eligible-new, eligible-update, eligible-no-change)
  - sync_paper_projection (RPC happy-path, error path, unknown-question-id guard)
  - get_paper_projection_status (counts, stale detection)
  - API endpoint permission gates (403, 404, 200)
  - MCQ exactly-one-correct validation fix in mock_questions.create_question
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin.pyq_mock_projection import (
    _check_question_eligibility,
    _fetch_question_stimuli,
    compute_content_hash,
    get_paper_projection_status,
    preview_paper_projection,
    resolve_primary_topic_split,
    sync_paper_projection,
)
from app.admin.mock_questions import create_question
from app.api import admin_mocks as admin_mocks_api
from app.core.auth import get_current_user
from tests.persona_questions._stub import SBStub

# ─── Fixtures ─────────────────────────────────────────────────────────────────

PAPER_ID  = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
EXAM_ID   = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
Q_ID      = "11111111-1111-1111-1111-111111111111"
TOPIC_ID  = "tttttttt-tttt-tttt-tttt-tttttttttttt"
# Migration 270: a microtopic is a topics row whose parent_topic_id is TOPIC_ID.
MICROTOPIC_ID = "mmmmmmmm-mmmm-mmmm-mmmm-mmmmmmmmmmmm"
SUBJECT_ID = "ssssssss-ssss-ssss-ssss-ssssssssssss"
ACTOR_ID  = "actor-001"


def _paper(trust_status: str = "verified") -> dict:
    return {
        "id": PAPER_ID,
        "exam_id": EXAM_ID,
        "year": 2023,
        "trust_status": trust_status,
        "source_url": "https://example.com/paper.pdf",
        "source_type": "official",
    }


def _question(
    reviewer_status: str = "verified",
    question_type: str = "mcq",
    question_text: str = "What is X?",
    **extras,
) -> dict:
    base = {
        "id": Q_ID,
        "pyq_paper_id": PAPER_ID,
        "question_text": question_text,
        "question_type": question_type,
        "reviewer_status": reviewer_status,
        "correct_option_id": None,
        "observed_difficulty": "medium",
        "expected_solve_time_sec": 60,
        "explanation_text": "Because X.",
        "language": "en",
        "section_id": None,
    }
    base.update(extras)
    return base


def _options(
    n: int = 4,
    correct_idx: int = 0,
    reviewer_status: str = "verified",
) -> list[dict]:
    return [
        {
            "id": f"opt-{i}",
            "question_id": Q_ID,
            "option_text": f"Option {chr(65 + i)}",
            "option_label": chr(65 + i),
            "is_correct": i == correct_idx,
            "reviewer_status": reviewer_status,
            "source_label": f"({chr(97 + i)})",
            "display_order": i + 1,
        }
        for i in range(n)
    ]


def _stimulus(
    reviewer_status: str = "verified",
    stimulus_id: str = "stim-1",
    content_text: str = "A shared reading passage.",
    display_order: int = 1,
    **extras,
) -> dict:
    base = {
        "id": stimulus_id,
        "pyq_paper_id": PAPER_ID,
        "stimulus_type": "passage",
        "content_text": content_text,
        "language": "en",
        "display_order": display_order,
        "reviewer_status": reviewer_status,
    }
    base.update(extras)
    return base


def _question_stimulus(
    stimulus_id: str = "stim-1",
    reviewer_status: str = "verified",
    display_order: int = 1,
    question_id: str = Q_ID,
    **extras,
) -> dict:
    base = {
        "id": f"qs-{stimulus_id}",
        "question_id": question_id,
        "stimulus_id": stimulus_id,
        "display_order": display_order,
        "reviewer_status": reviewer_status,
    }
    base.update(extras)
    return base


def _combined_stimuli(stimuli: list[dict], links: list[dict]) -> list[dict]:
    """Build the combined link+stimulus dicts via the real fetch translation."""
    sb = SBStub()
    sb.db["pyq_stimuli"] = stimuli
    sb.db["pyq_question_stimuli"] = links
    return _fetch_question_stimuli(sb, Q_ID)


def _primary_tag(reviewer_status: str = "verified") -> list[dict]:
    return [
        {
            "id": "tag-1",
            "question_id": Q_ID,
            "topic_id": TOPIC_ID,
            "tag_role": "primary",
            "reviewer_status": reviewer_status,
        }
    ]


def _topic_row(
    topic_id: str = TOPIC_ID,
    parent_topic_id: str | None = None,
    subject_id: str = SUBJECT_ID,
) -> dict:
    """A ``topics`` row. ``parent_topic_id=None`` is a top-level topic; a set
    parent makes it a microtopic (Migration 270 level resolution)."""
    return {
        "id": topic_id,
        "parent_topic_id": parent_topic_id,
        "subject_id": subject_id,
        "is_active": True,
    }


def _microtopic_tag(reviewer_status: str = "verified") -> list[dict]:
    """A verified primary tag pointing at a MICROTOPIC (child of TOPIC_ID)."""
    return [
        {
            "id": "tag-1",
            "question_id": Q_ID,
            "topic_id": MICROTOPIC_ID,
            "tag_role": "primary",
            "reviewer_status": reviewer_status,
        }
    ]


def _seed_sb(
    paper: dict | None = None,
    questions: list[dict] | None = None,
    options: list[dict] | None = None,
    tags: list[dict] | None = None,
    projections: list[dict] | None = None,
    stimuli: list[dict] | None = None,
    question_stimuli: list[dict] | None = None,
    topics: list[dict] | None = None,
) -> SBStub:
    sb = SBStub()
    sb.db["pyq_papers"] = [paper or _paper()]
    sb.db["pyq_questions"] = questions if questions is not None else [_question()]
    sb.db["pyq_options"] = options if options is not None else _options()
    sb.db["pyq_question_topic_tags"] = tags if tags is not None else _primary_tag()
    sb.db["pyq_mock_question_projections"] = projections or []
    sb.db["pyq_stimuli"] = stimuli or []
    sb.db["pyq_question_stimuli"] = question_stimuli or []
    # Migration 270: the preview resolves the primary tag's level from
    # topics.parent_topic_id. Default fixture is a top-level topic.
    sb.db["topics"] = topics if topics is not None else [_topic_row()]
    return sb


# ─── Unit: compute_content_hash ───────────────────────────────────────────────

class TestComputeContentHash:
    def test_deterministic(self):
        q = _question()
        opts = _options()
        h1 = compute_content_hash(q, opts)
        h2 = compute_content_hash(q, opts)
        assert h1 == h2

    def test_changes_when_question_text_changes(self):
        opts = _options()
        h1 = compute_content_hash(_question(question_text="A"), opts)
        h2 = compute_content_hash(_question(question_text="B"), opts)
        assert h1 != h2

    def test_changes_when_option_text_changes(self):
        q = _question()
        opts_a = _options()
        opts_b = [dict(o, option_text="ZZZ") if i == 0 else o for i, o in enumerate(_options())]
        assert compute_content_hash(q, opts_a) != compute_content_hash(q, opts_b)

    def test_changes_when_correct_option_changes(self):
        q = _question()
        opts_0 = _options(correct_idx=0)
        opts_1 = _options(correct_idx=1)
        assert compute_content_hash(q, opts_0) != compute_content_hash(q, opts_1)

    def test_case_insensitive_question_text(self):
        opts = _options()
        h1 = compute_content_hash(_question(question_text="What IS X?"), opts)
        h2 = compute_content_hash(_question(question_text="what is x?"), opts)
        assert h1 == h2

    # ── Regression: NUL-separator crash (migration 239) ────────────────────────

    def test_hashed_content_uses_no_null_byte_separator(self):
        """The joined hash input must not contain a NUL byte.

        The SQL mirror (project_pyq_question_to_mock_bank) hashes the identical
        field set with the same separators; PostgreSQL text cannot hold chr(0),
        so a NUL separator here would mean the RPC crashes the whole PYQ->mock
        sync. Guard the Python side so the two mirrors can never drift back to a
        null separator. Feed values through every list branch (options, primary
        + extra verified tags, stimuli) so all separators are exercised.
        """
        import app.admin.pyq_mock_projection as mod

        captured: dict[str, str] = {}
        real_sha256 = mod.hashlib.sha256

        def _spy(data: bytes):
            captured["raw"] = data.decode("utf-8")
            return real_sha256(data)

        q = _question(question_text="What is X?", explanation_text="Because Y.")
        opts = _options()
        tags = _primary_tag() + [
            {"topic_id": "t-extra", "tag_role": "secondary", "reviewer_status": "verified"},
        ]
        stimuli = [{
            "stimulus_id": "s1", "stimulus_type": "passage",
            "content_text": "A shared passage.", "language": "en",
            "link_display_order": 0, "stimulus_display_order": 0,
            "link_reviewer_status": "verified",
            "stimulus_reviewer_status": "verified",
        }]
        with patch.object(mod.hashlib, "sha256", _spy):
            compute_content_hash(q, opts, paper=_paper(), all_verified_tags=tags, stimuli=stimuli)

        assert "raw" in captured, "sha256 was not called"
        assert "\x00" not in captured["raw"], "NUL byte in hash input would crash the SQL projection RPC"
        # top-level Group Separator is used; NUL is not.
        assert "\x1d" in captured["raw"]

    # ── Regression: every projected field changes the hash ─────────────────────

    def test_changes_when_explanation_changes(self):
        opts = _options()
        h1 = compute_content_hash(_question(explanation_text="Explanation A"), opts)
        h2 = compute_content_hash(_question(explanation_text="Explanation B"), opts)
        assert h1 != h2

    def test_changes_when_difficulty_changes(self):
        opts = _options()
        h1 = compute_content_hash(_question(observed_difficulty="easy"), opts)
        h2 = compute_content_hash(_question(observed_difficulty="hard"), opts)
        assert h1 != h2

    def test_changes_when_language_changes(self):
        opts = _options()
        h1 = compute_content_hash(_question(language="en"), opts)
        h2 = compute_content_hash(_question(language="hi"), opts)
        assert h1 != h2

    def test_changes_when_expected_time_changes(self):
        opts = _options()
        h1 = compute_content_hash(_question(expected_solve_time_sec=60), opts)
        h2 = compute_content_hash(_question(expected_solve_time_sec=120), opts)
        assert h1 != h2

    def test_changes_when_paper_year_changes(self):
        opts = _options()
        h1 = compute_content_hash(_question(), opts, paper={"year": 2020})
        h2 = compute_content_hash(_question(), opts, paper={"year": 2023})
        assert h1 != h2

    def test_changes_when_option_label_changes(self):
        opts_a = _options()
        # Swap labels on two options (different ordering)
        opts_b = [dict(o, option_label="B" if o["option_label"] == "A" else
                              "A" if o["option_label"] == "B" else o["option_label"])
                  for o in _options()]
        h1 = compute_content_hash(_question(), opts_a)
        h2 = compute_content_hash(_question(), opts_b)
        assert h1 != h2

    def test_changes_when_non_primary_verified_tag_added(self):
        opts = _options()
        extra_tag = {
            "id": "tag-2",
            "question_id": Q_ID,
            "topic_id": "topic-secondary",
            "tag_role": "secondary",
            "reviewer_status": "verified",
        }
        h1 = compute_content_hash(_question(), opts, all_verified_tags=_primary_tag())
        h2 = compute_content_hash(_question(), opts, all_verified_tags=_primary_tag() + [extra_tag])
        assert h1 != h2

    def test_unverified_tag_does_not_affect_hash(self):
        opts = _options()
        unverified_tag = {
            "id": "tag-3",
            "question_id": Q_ID,
            "topic_id": "topic-unverified",
            "tag_role": "secondary",
            "reviewer_status": "draft",
        }
        h1 = compute_content_hash(_question(), opts, all_verified_tags=_primary_tag())
        h2 = compute_content_hash(_question(), opts, all_verified_tags=_primary_tag() + [unverified_tag])
        assert h1 == h2

    def test_changes_when_paper_exam_id_changes(self):
        opts = _options()
        h1 = compute_content_hash(_question(), opts, paper={"exam_id": "exam-A", "year": 2023})
        h2 = compute_content_hash(_question(), opts, paper={"exam_id": "exam-B", "year": 2023})
        assert h1 != h2

    def test_changes_when_paper_source_url_changes(self):
        opts = _options()
        h1 = compute_content_hash(_question(), opts, paper={"source_url": "https://old.example.com/paper.pdf"})
        h2 = compute_content_hash(_question(), opts, paper={"source_url": "https://new.example.com/paper.pdf"})
        assert h1 != h2

    def test_changes_when_paper_source_type_changes(self):
        opts = _options()
        h1 = compute_content_hash(_question(), opts, paper={"source_type": "official"})
        h2 = compute_content_hash(_question(), opts, paper={"source_type": "unofficial"})
        assert h1 != h2

    # ── Normalization parity (Python must match the SQL hash formula) ──────────

    def test_blank_language_normalizes_to_en(self):
        """Empty and NULL language must both hash identically to 'en'."""
        opts = _options()
        h_empty = compute_content_hash(_question(language=""), opts)
        h_en    = compute_content_hash(_question(language="en"), opts)
        assert h_empty == h_en

    def test_whitespace_language_normalizes_to_en(self):
        """Whitespace-only language must normalize to 'en', not to empty string."""
        opts = _options()
        h_ws = compute_content_hash(_question(language="   "), opts)
        h_en = compute_content_hash(_question(language="en"), opts)
        assert h_ws == h_en

    def test_whitespace_difficulty_normalizes_to_stripped_value(self):
        """Difficulty with surrounding whitespace must hash the same as the trimmed form."""
        opts = _options()
        h_padded  = compute_content_hash(_question(observed_difficulty="easy  "), opts)
        h_trimmed = compute_content_hash(_question(observed_difficulty="easy"), opts)
        assert h_padded == h_trimmed

    def test_zero_expected_time_differs_from_none(self):
        """expected_solve_time_sec=0 is a valid value ('0') and must not hash as None ('')."""
        opts = _options()
        h_zero = compute_content_hash(_question(expected_solve_time_sec=0), opts)
        h_none = compute_content_hash(_question(expected_solve_time_sec=None), opts)
        assert h_zero != h_none

    # ── PR-4 (migration 229): section, printed-order, stimulus fidelity ────────

    def test_changes_when_section_id_changes(self):
        opts = _options()
        h1 = compute_content_hash(_question(section_id="sec-A"), opts)
        h2 = compute_content_hash(_question(section_id="sec-B"), opts)
        assert h1 != h2

    def test_changes_when_option_source_label_changes(self):
        opts_a = _options()
        opts_b = [dict(o, source_label="ROMAN-IV") if i == 0 else o
                  for i, o in enumerate(_options())]
        assert compute_content_hash(_question(), opts_a) != compute_content_hash(_question(), opts_b)

    def test_changes_when_option_display_order_changes(self):
        opts_a = _options()
        opts_b = [dict(o, display_order=99) if i == 0 else o
                  for i, o in enumerate(_options())]
        assert compute_content_hash(_question(), opts_a) != compute_content_hash(_question(), opts_b)

    def test_changes_when_verified_stimulus_content_changes(self):
        s1 = _combined_stimuli([_stimulus(content_text="Passage one")], [_question_stimulus()])
        s2 = _combined_stimuli([_stimulus(content_text="Passage two")], [_question_stimulus()])
        h1 = compute_content_hash(_question(), _options(), stimuli=s1)
        h2 = compute_content_hash(_question(), _options(), stimuli=s2)
        assert h1 != h2

    def test_stimulus_hash_is_stable_when_unchanged(self):
        s = _combined_stimuli([_stimulus()], [_question_stimulus()])
        h1 = compute_content_hash(_question(), _options(), stimuli=s)
        h2 = compute_content_hash(_question(), _options(), stimuli=s)
        assert h1 == h2

    def test_adding_verified_stimulus_changes_hash(self):
        h_none = compute_content_hash(_question(), _options(), stimuli=[])
        s = _combined_stimuli([_stimulus()], [_question_stimulus()])
        h_with = compute_content_hash(_question(), _options(), stimuli=s)
        assert h_none != h_with

    def test_unverified_stimulus_does_not_affect_hash(self):
        """A link or stimulus that is not verified-verified must be excluded from the hash."""
        h_none = compute_content_hash(_question(), _options(), stimuli=[])
        s_unverified_stim = _combined_stimuli(
            [_stimulus(reviewer_status="pending")], [_question_stimulus(reviewer_status="verified")]
        )
        s_unverified_link = _combined_stimuli(
            [_stimulus(reviewer_status="verified")], [_question_stimulus(reviewer_status="pending")]
        )
        assert compute_content_hash(_question(), _options(), stimuli=s_unverified_stim) == h_none
        assert compute_content_hash(_question(), _options(), stimuli=s_unverified_link) == h_none


# ─── Unit: microtopic split (Migration 270) ──────────────────────────────────

class TestPrimaryTopicSplit:
    """The projection wrote only ``topic_id``, so a primary tag pointing at a
    microtopic was flattened and ``microtopic_id`` stayed NULL. Migration 270
    resolves the tag's level from ``topics.parent_topic_id`` and splits it;
    ``resolve_primary_topic_split`` is the Python mirror of that logic."""

    # ── Case 1: primary tag is a MICROTOPIC ──────────────────────────────────

    def test_microtopic_tag_splits_into_parent_and_microtopic(self):
        topic_id, microtopic_id = resolve_primary_topic_split(
            _microtopic_tag(),
            _topic_row(topic_id=MICROTOPIC_ID, parent_topic_id=TOPIC_ID),
        )
        assert topic_id == TOPIC_ID
        assert microtopic_id == MICROTOPIC_ID

    def test_microtopic_tag_changes_the_hash(self):
        """A tag moving from a top-level topic to a microtopic must re-hash, or
        the RPC returns 'unchanged' and the bank row keeps the stale level."""
        h_topic = compute_content_hash(
            _question(), _options(), primary_microtopic_id=None
        )
        h_micro = compute_content_hash(
            _question(), _options(), primary_microtopic_id=MICROTOPIC_ID
        )
        assert h_topic != h_micro

    def test_hash_distinguishes_two_microtopics_under_one_parent(self):
        """Both rows project the same topic_id, so only microtopic_id in the
        hash separates them."""
        h_a = compute_content_hash(_question(), _options(), primary_microtopic_id="micro-a")
        h_b = compute_content_hash(_question(), _options(), primary_microtopic_id="micro-b")
        assert h_a != h_b

    def test_preview_hash_matches_resolved_microtopic(self):
        """End-to-end: preview must resolve the microtopic the same way the RPC
        does, or every microtopic-tagged row shows a permanent would_update."""
        sb = _seed_sb(
            tags=_microtopic_tag(),
            topics=[_topic_row(topic_id=MICROTOPIC_ID, parent_topic_id=TOPIC_ID)],
        )
        out = preview_paper_projection(sb, PAPER_ID)
        entry = out["questions"][0]
        assert entry["eligible"] is True
        expected = compute_content_hash(
            _question(),
            _options(),
            paper=_paper(),
            all_verified_tags=_microtopic_tag(),
            stimuli=[],
            primary_microtopic_id=MICROTOPIC_ID,
        )
        assert entry["content_hash"] == expected

    # ── Case 2: primary tag is a TOP-LEVEL TOPIC ─────────────────────────────

    def test_top_level_tag_keeps_topic_and_nulls_microtopic(self):
        topic_id, microtopic_id = resolve_primary_topic_split(
            _primary_tag(),
            _topic_row(topic_id=TOPIC_ID, parent_topic_id=None),
        )
        assert topic_id == TOPIC_ID
        assert microtopic_id is None

    def test_top_level_tag_hash_is_null_safe(self):
        """coalesce-to-'' parity with the SQL: a NULL microtopic must produce a
        real digest, and the same one whether it arrives as None or ''."""
        h_none = compute_content_hash(_question(), _options(), primary_microtopic_id=None)
        h_empty = compute_content_hash(_question(), _options(), primary_microtopic_id="")
        assert h_none == h_empty
        assert len(h_none) == 64

    def test_preview_hash_for_top_level_tag_carries_no_microtopic(self):
        sb = _seed_sb()  # default fixture: TOPIC_ID with parent_topic_id=None
        out = preview_paper_projection(sb, PAPER_ID)
        entry = out["questions"][0]
        assert entry["eligible"] is True
        assert entry["content_hash"] == compute_content_hash(
            _question(),
            _options(),
            paper=_paper(),
            all_verified_tags=_primary_tag(),
            stimuli=[],
            primary_microtopic_id=None,
        )

    # ── Case 3: NO primary tag ───────────────────────────────────────────────

    def test_no_primary_tag_resolves_to_nothing(self):
        assert resolve_primary_topic_split([], _topic_row()) == (None, None)

    def test_unverified_primary_tag_resolves_to_nothing(self):
        assert resolve_primary_topic_split(
            _primary_tag(reviewer_status="pending"), _topic_row()
        ) == (None, None)

    def test_two_primary_tags_resolve_to_nothing(self):
        """The RPC blocks on primary_topic_tag_count_not_one before it writes,
        so there is no level to resolve."""
        tags = _primary_tag() + _microtopic_tag()
        assert resolve_primary_topic_split(tags, _topic_row()) == (None, None)

    def test_missing_topic_row_resolves_to_nothing(self):
        """A dangling tag — the RPC blocks on primary_topic_invalid_or_inactive."""
        assert resolve_primary_topic_split(_primary_tag(), None) == (None, None)

    def test_preview_leaves_untagged_question_ineligible_and_unhashed(self):
        sb = _seed_sb(tags=[])
        out = preview_paper_projection(sb, PAPER_ID)
        entry = out["questions"][0]
        assert entry["eligible"] is False
        assert entry["content_hash"] is None


# ─── Unit: _check_question_eligibility ────────────────────────────────────────

class TestCheckEligibility:
    def _eligible_call(self, **overrides):
        paper  = {**_paper(), **overrides.get("paper", {})}
        q      = {**_question(), **overrides.get("question", {})}
        opts   = overrides.get("options", _options())
        tags   = overrides.get("tags", _primary_tag())
        stims  = overrides.get("stimuli")
        return _check_question_eligibility(paper, q, opts, tags, stims)

    def test_fully_eligible(self):
        eligible, reason = self._eligible_call()
        assert eligible is True
        assert reason == "eligible"

    def test_paper_not_verified(self):
        eligible, reason = self._eligible_call(paper={"trust_status": "pending"})
        assert eligible is False
        assert "paper_not_verified" in reason

    def test_question_not_verified(self):
        eligible, reason = self._eligible_call(question={"reviewer_status": "draft"})
        assert eligible is False
        assert "question_not_verified" in reason

    def test_not_mcq(self):
        eligible, reason = self._eligible_call(question={"question_type": "msq"})
        assert eligible is False
        assert "not_mcq" in reason

    def test_empty_question_text(self):
        eligible, reason = self._eligible_call(question={"question_text": "  "})
        assert eligible is False
        assert "empty_question_text" in reason

    def test_too_few_verified_options(self):
        # Only 1 verified option
        opts = [
            {**_options()[0], "reviewer_status": "verified"},
            {**_options()[1], "reviewer_status": "draft"},
            {**_options()[2], "reviewer_status": "draft"},
        ]
        eligible, reason = self._eligible_call(options=opts)
        assert eligible is False
        assert "too_few_verified_options" in reason

    def test_zero_correct_options(self):
        opts = [dict(o, is_correct=False) for o in _options()]
        eligible, reason = self._eligible_call(options=opts)
        assert eligible is False
        assert "not_exactly_one_correct" in reason

    def test_two_correct_options(self):
        opts = _options()
        opts[0] = dict(opts[0], is_correct=True)
        opts[1] = dict(opts[1], is_correct=True)
        eligible, reason = self._eligible_call(options=opts)
        assert eligible is False
        assert "not_exactly_one_correct" in reason

    def test_unverified_correct_blocks_eligibility(self):
        """Unverified option with is_correct=True must not count; no verified correct → blocked."""
        opts = [
            {"id": "opt-0", "question_id": Q_ID, "option_text": "A", "is_correct": False, "reviewer_status": "verified"},
            {"id": "opt-1", "question_id": Q_ID, "option_text": "B", "is_correct": False, "reviewer_status": "verified"},
            {"id": "opt-2", "question_id": Q_ID, "option_text": "C", "is_correct": True,  "reviewer_status": "draft"},
        ]
        eligible, reason = self._eligible_call(options=opts)
        assert eligible is False
        assert "not_exactly_one_correct" in reason

    def test_empty_verified_option_text_blocked(self):
        """Verified option with blank text must block projection."""
        opts = [
            {"id": "opt-0", "question_id": Q_ID, "option_text": "  ", "is_correct": True,  "reviewer_status": "verified"},
            {"id": "opt-1", "question_id": Q_ID, "option_text": "B",  "is_correct": False, "reviewer_status": "verified"},
        ]
        eligible, reason = self._eligible_call(options=opts)
        assert eligible is False
        assert "empty_verified_option_text" in reason

    def test_correct_option_id_mismatch_blocked(self):
        """correct_option_id pointing to a non-correct verified option must block."""
        opts = _options(correct_idx=0)  # opt-0 is the verified correct
        q = {**_question(), "correct_option_id": "opt-3"}  # pointer disagrees
        eligible, reason = self._eligible_call(question=q, options=opts)
        assert eligible is False
        assert "correct_option_id_mismatch" in reason

    def test_correct_option_id_matching_passes(self):
        """correct_option_id that matches the verified correct option must pass."""
        opts = _options(correct_idx=0)  # opt-0 is correct
        q = {**_question(), "correct_option_id": "opt-0"}
        eligible, reason = self._eligible_call(question=q, options=opts)
        assert eligible is True

    def test_correct_option_id_null_skips_check(self):
        """correct_option_id = None must not trigger the mismatch check."""
        opts = _options(correct_idx=1)
        q = {**_question(), "correct_option_id": None}
        eligible, reason = self._eligible_call(question=q, options=opts)
        assert eligible is True

    def test_no_primary_tag(self):
        eligible, reason = self._eligible_call(tags=[])
        assert eligible is False
        assert "not_exactly_one_verified_primary_tag" in reason

    def test_unverified_primary_tag(self):
        tags = [dict(_primary_tag()[0], reviewer_status="draft")]
        eligible, reason = self._eligible_call(tags=tags)
        assert eligible is False
        assert "not_exactly_one_verified_primary_tag" in reason

    def test_secondary_tag_does_not_count_as_primary(self):
        tags = [dict(_primary_tag()[0], tag_role="secondary")]
        eligible, reason = self._eligible_call(tags=tags)
        assert eligible is False
        assert "not_exactly_one_verified_primary_tag" in reason

    # ── PR-4 (migration 229): stimulus verification gate ──────────────────────

    def test_no_stimulus_links_is_eligible(self):
        eligible, reason = self._eligible_call(stimuli=[])
        assert eligible is True
        assert reason == "eligible"

    def test_all_verified_stimuli_is_eligible(self):
        stims = _combined_stimuli([_stimulus()], [_question_stimulus()])
        eligible, reason = self._eligible_call(stimuli=stims)
        assert eligible is True
        assert reason == "eligible"

    def test_unverified_stimulus_blocks(self):
        stims = _combined_stimuli(
            [_stimulus(reviewer_status="pending")], [_question_stimulus(reviewer_status="verified")]
        )
        eligible, reason = self._eligible_call(stimuli=stims)
        assert eligible is False
        assert reason == "stimulus_not_verified"

    def test_unverified_link_blocks(self):
        stims = _combined_stimuli(
            [_stimulus(reviewer_status="verified")], [_question_stimulus(reviewer_status="pending")]
        )
        eligible, reason = self._eligible_call(stimuli=stims)
        assert eligible is False
        assert reason == "stimulus_not_verified"

    def test_one_unverified_among_verified_blocks(self):
        stims = _combined_stimuli(
            [_stimulus(stimulus_id="stim-1"),
             _stimulus(stimulus_id="stim-2", reviewer_status="pending", display_order=2)],
            [_question_stimulus(stimulus_id="stim-1", display_order=1),
             _question_stimulus(stimulus_id="stim-2", display_order=2)],
        )
        eligible, reason = self._eligible_call(stimuli=stims)
        assert eligible is False
        assert reason == "stimulus_not_verified"


# ─── preview_paper_projection ────────────────────────────────────────────────

class TestPreviewPaperProjection:
    def test_paper_not_found_raises_lookup(self):
        sb = SBStub()
        with pytest.raises(LookupError):
            preview_paper_projection(sb, PAPER_ID)

    def test_empty_paper(self):
        sb = _seed_sb(questions=[])
        result = preview_paper_projection(sb, PAPER_ID)
        assert result["total"] == 0
        assert result["eligible_count"] == 0

    def test_eligible_new_question(self):
        sb = _seed_sb()
        result = preview_paper_projection(sb, PAPER_ID)
        assert result["eligible_count"] == 1
        assert result["would_create_count"] == 1
        assert result["already_projected_count"] == 0
        q_entry = result["questions"][0]
        assert q_entry["eligible"] is True
        assert q_entry["content_hash"] is not None

    def test_preview_entry_carries_readable_label(self):
        # EI-CLEAN-04: every preview row exposes a readable label (question text)
        # so the operator UI shows text, not a truncated UUID.
        sb = _seed_sb()
        result = preview_paper_projection(sb, PAPER_ID)
        assert result["questions"][0]["label"] == "What is X?"

    def test_preview_label_truncates_long_and_collapses_whitespace(self):
        long_q = _question(question_text="  " + "word " * 40 + "  ")
        sb = _seed_sb(questions=[long_q])
        result = preview_paper_projection(sb, PAPER_ID)
        label = result["questions"][0]["label"]
        assert len(label) <= 80
        assert label.endswith("…")
        assert "  " not in label  # whitespace collapsed to single spaces

    def test_ineligible_unverified_paper(self):
        sb = _seed_sb(paper=_paper(trust_status="pending"))
        result = preview_paper_projection(sb, PAPER_ID)
        assert result["eligible_count"] == 0
        assert result["ineligible_count"] == 1
        assert "paper_not_verified" in result["questions"][0]["reason"]

    def test_already_projected_no_change(self):
        # Hash must be computed with the same inputs preview uses (paper + all verified tags).
        content_hash = compute_content_hash(
            _question(), _options(), paper=_paper(), all_verified_tags=_primary_tag()
        )
        projection = {
            "pyq_question_id": Q_ID,
            "mock_question_id": "mock-1",
            "sync_status": "active",
            "source_content_hash": content_hash,
            "projected_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
        sb = _seed_sb(projections=[projection])
        result = preview_paper_projection(sb, PAPER_ID)
        q_entry = result["questions"][0]
        assert q_entry["eligible"] is True
        assert q_entry["would_update"] is False
        assert result["would_update_count"] == 0

    def test_already_projected_stale(self):
        projection = {
            "pyq_question_id": Q_ID,
            "mock_question_id": "mock-1",
            "sync_status": "stale",
            "source_content_hash": "old-hash",
            "projected_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
        sb = _seed_sb(projections=[projection])
        result = preview_paper_projection(sb, PAPER_ID)
        q_entry = result["questions"][0]
        assert q_entry["eligible"] is True
        assert q_entry["would_update"] is True
        assert result["would_update_count"] == 1

    def test_stale_projection_with_matching_hash_reports_would_update(self):
        """Stale sync_status alone (not hash divergence) must trigger re-projection.

        If a projection is stale but the hash hasn't changed, preview must still
        report would_update=True so the operator knows the RPC needs to run to
        restore active status.
        """
        content_hash = compute_content_hash(
            _question(), _options(), paper=_paper(), all_verified_tags=_primary_tag()
        )
        projection = {
            "pyq_question_id": Q_ID,
            "mock_question_id": "mock-1",
            "sync_status": "stale",
            "source_content_hash": content_hash,  # hash matches — but status is not active
            "projected_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
        sb = _seed_sb(projections=[projection])
        result = preview_paper_projection(sb, PAPER_ID)
        q_entry = result["questions"][0]
        assert q_entry["eligible"] is True
        assert q_entry["would_update"] is True, (
            "stale projection with matching hash must still require re-projection"
        )
        assert result["would_update_count"] == 1

    # ── PR-4 (migration 229): stimulus fidelity in preview ────────────────────

    def test_preview_blocks_unverified_stimulus(self):
        sb = _seed_sb(
            stimuli=[_stimulus(reviewer_status="pending")],
            question_stimuli=[_question_stimulus()],
        )
        result = preview_paper_projection(sb, PAPER_ID)
        q_entry = result["questions"][0]
        assert q_entry["eligible"] is False
        assert "stimulus_not_verified" in q_entry["reason"]
        assert q_entry["content_hash"] is None

    def test_preview_passes_with_verified_stimulus(self):
        sb = _seed_sb(
            stimuli=[_stimulus()],
            question_stimuli=[_question_stimulus()],
        )
        result = preview_paper_projection(sb, PAPER_ID)
        q_entry = result["questions"][0]
        assert q_entry["eligible"] is True
        assert q_entry["content_hash"] is not None

    def test_preview_hash_reflects_stimulus_content(self):
        sb_none = _seed_sb()
        sb_with = _seed_sb(
            stimuli=[_stimulus()],
            question_stimuli=[_question_stimulus()],
        )
        h_none = preview_paper_projection(sb_none, PAPER_ID)["questions"][0]["content_hash"]
        h_with = preview_paper_projection(sb_with, PAPER_ID)["questions"][0]["content_hash"]
        assert h_none is not None and h_with is not None
        assert h_none != h_with


# ─── sync_paper_projection ────────────────────────────────────────────────────

class TestSyncPaperProjection:
    def _sb_with_rpc(self, rpc_return: Any = None, rpc_raises: Exception | None = None) -> SBStub:
        sb = _seed_sb()
        original_rpc = sb.rpc

        def patched_rpc(name, params=None):
            if name == "project_pyq_question_to_mock_bank":
                if rpc_raises:
                    class _R:
                        def execute(self):
                            raise rpc_raises
                    return _R()
                data = rpc_return if rpc_return is not None else [{"outcome": "created", "mock_question_id": "mock-new"}]
                class _R:
                    def execute(self_inner):
                        class _Exec:
                            data = rpc_return if rpc_return is not None else [{"outcome": "created", "mock_question_id": "mock-new"}]
                        return _Exec()
                return _R()
            return original_rpc(name, params)

        sb.rpc = patched_rpc
        return sb

    def test_paper_not_found_raises_lookup(self):
        sb = SBStub()
        with pytest.raises(LookupError):
            sync_paper_projection(sb, PAPER_ID, ACTOR_ID)

    def test_rpc_called_for_each_question(self):
        calls = []
        sb = _seed_sb()
        original_rpc = sb.rpc

        def track_rpc(name, params=None):
            if name == "project_pyq_question_to_mock_bank":
                calls.append(params)
                class _R:
                    def execute(self):
                        class _E:
                            data = [{"outcome": "created", "mock_question_id": "mock-new"}]
                        return _E()
                return _R()
            return original_rpc(name, params)

        sb.rpc = track_rpc
        result = sync_paper_projection(sb, PAPER_ID, ACTOR_ID)
        assert result["attempted"] == 1
        assert len(calls) == 1
        assert calls[0]["p_pyq_question_id"] == Q_ID
        assert calls[0]["p_actor_id"] == ACTOR_ID
        # EI-CLEAN-04: sync-result rows carry the readable label, like preview.
        assert result["questions"][0]["label"] == "What is X?"

    def test_rpc_exception_blocks_the_row_instead_of_aborting_the_run(self):
        """A row the RPC rejects is reported, not raised.

        Each RPC call is its own transaction, so raising out of the loop left
        every earlier row committed and said nothing about which row failed.
        """
        sb = _seed_sb()
        original_rpc = sb.rpc

        def fail_rpc(name, params=None):
            if name == "project_pyq_question_to_mock_bank":
                class _R:
                    def execute(self):
                        raise RuntimeError("DB error")
                return _R()
            return original_rpc(name, params)

        sb.rpc = fail_rpc
        out = sync_paper_projection(sb, PAPER_ID, ACTOR_ID)
        assert out["attempted"] == 1
        assert out["outcomes"]["blocked"] == 1
        row = out["questions"][0]
        assert row["outcome"] == "blocked"
        assert row["question_id"] == Q_ID
        assert row["reason"].startswith("rpc_error:")
        assert "DB error" in row["detail"]["error"]

    def test_a_blocked_row_does_not_stop_the_questions_after_it(self):
        """The partial-write bug: one bad row aborted the remaining rows."""
        q_bad = _question(id="11111111-1111-1111-1111-111111111111", question_number=1)
        q_ok1 = _question(id="22222222-2222-2222-2222-222222222222", question_number=2)
        q_ok2 = _question(id="33333333-3333-3333-3333-333333333333", question_number=3)
        sb = _seed_sb(
            questions=[q_bad, q_ok1, q_ok2],
            options=[o | {"id": f"{q['id']}-{o['id']}", "question_id": q["id"]}
                     for q in (q_bad, q_ok1, q_ok2) for o in _options()],
            tags=[t | {"id": f"{q['id']}-tag", "question_id": q["id"]}
                  for q in (q_bad, q_ok1, q_ok2) for t in _primary_tag()],
        )
        original_rpc = sb.rpc

        def mixed_rpc(name, params=None):
            if name == "project_pyq_question_to_mock_bank":
                bad = params and params.get("p_pyq_question_id") == q_bad["id"]

                class _R:
                    def execute(self):
                        if bad:
                            raise RuntimeError(
                                'invalid input syntax for type bytea')

                        class _E:
                            data = [{"outcome": "created", "mock_question_id": "m"}]
                        return _E()
                return _R()
            return original_rpc(name, params)

        sb.rpc = mixed_rpc
        out = sync_paper_projection(sb, PAPER_ID, ACTOR_ID)
        assert out["attempted"] == 3
        assert out["outcomes"]["blocked"] == 1
        assert out["outcomes"]["created"] == 2
        blocked = [r for r in out["questions"] if r["outcome"] == "blocked"]
        assert [r["question_id"] for r in blocked] == [q_bad["id"]]

    def test_bytea_cast_failure_gets_a_named_reason(self):
        """The RBI 2024 Q95 symptom: a backslash the ::bytea cast cannot parse."""
        sb = _seed_sb()
        original_rpc = sb.rpc

        def fail_rpc(name, params=None):
            if name == "project_pyq_question_to_mock_bank":
                class _R:
                    def execute(self):
                        raise RuntimeError(
                            'invalid input syntax for type bytea')
                return _R()
            return original_rpc(name, params)

        sb.rpc = fail_rpc
        out = sync_paper_projection(sb, PAPER_ID, ACTOR_ID)
        assert out["questions"][0]["reason"].startswith("content_hash_bytea_cast:")

    def test_sync_reads_questions_in_a_deterministic_order(self):
        """Without an explicit order the completed set is not a readable prefix."""
        qs = [_question(id=f"{n:08d}-0000-0000-0000-000000000000", question_number=n)
              for n in (95, 81, 200)]
        sb = _seed_sb(
            questions=qs,
            options=[o | {"id": f"{q['id']}-{o['id']}", "question_id": q["id"]}
                     for q in qs for o in _options()],
            tags=[t | {"id": f"{q['id']}-tag", "question_id": q["id"]}
                  for q in qs for t in _primary_tag()],
        )
        original_rpc = sb.rpc
        seen: list[str] = []

        def tracking_rpc(name, params=None):
            if name == "project_pyq_question_to_mock_bank":
                seen.append(params["p_pyq_question_id"])

                class _R:
                    def execute(self):
                        class _E:
                            data = [{"outcome": "created", "mock_question_id": "m"}]
                        return _E()
                return _R()
            return original_rpc(name, params)

        sb.rpc = tracking_rpc
        sync_paper_projection(sb, PAPER_ID, ACTOR_ID)
        numbers = [next(q["question_number"] for q in qs if q["id"] == qid) for qid in seen]
        assert numbers == sorted(numbers)

    def test_every_result_row_carries_its_question_number(self):
        """The client could not map a UUID back to a question number."""
        sb = _seed_sb()
        out = sync_paper_projection(sb, PAPER_ID, ACTOR_ID)
        assert all("question_number" in r for r in out["questions"])

    def test_unknown_question_id_rejected(self):
        sb = _seed_sb()
        with pytest.raises(ValueError, match="not in paper"):
            sync_paper_projection(sb, PAPER_ID, ACTOR_ID, question_ids=["99999999-9999-9999-9999-999999999999"])

    def test_outcome_counted_correctly(self):
        sb = _seed_sb()
        original_rpc = sb.rpc

        def ok_rpc(name, params=None):
            if name == "project_pyq_question_to_mock_bank":
                class _R:
                    def execute(self):
                        class _E:
                            data = [{"outcome": "unchanged", "mock_question_id": "mock-1"}]
                        return _E()
                return _R()
            return original_rpc(name, params)

        sb.rpc = ok_rpc
        result = sync_paper_projection(sb, PAPER_ID, ACTOR_ID)
        assert result["outcomes"]["unchanged"] == 1


# ─── get_paper_projection_status ─────────────────────────────────────────────

class TestGetPaperProjectionStatus:
    def test_paper_not_found(self):
        with pytest.raises(LookupError):
            get_paper_projection_status(SBStub(), PAPER_ID)

    def test_no_projections(self):
        sb = _seed_sb()
        result = get_paper_projection_status(sb, PAPER_ID)
        assert result["total_questions"] == 1
        assert result["unprojected_count"] == 1
        assert result["projection_counts"]["active"] == 0

    def test_active_projection(self):
        projection = {
            "pyq_question_id": Q_ID,
            "mock_question_id": "mock-1",
            "sync_status": "active",
            "updated_at": "2026-01-01T00:00:00",
            "last_sync_result": {},
        }
        sb = _seed_sb(projections=[projection])
        result = get_paper_projection_status(sb, PAPER_ID)
        assert result["projection_counts"]["active"] == 1
        assert result["unprojected_count"] == 0
        assert result["stale_projections"] == []

    def test_stale_projection_listed(self):
        projection = {
            "pyq_question_id": Q_ID,
            "mock_question_id": "mock-1",
            "sync_status": "stale",
            "updated_at": "2026-01-01T00:00:00",
            "last_sync_result": {},
        }
        sb = _seed_sb(projections=[projection])
        result = get_paper_projection_status(sb, PAPER_ID)
        assert result["projection_counts"]["stale"] == 1
        assert len(result["stale_projections"]) == 1


# ─── API endpoint tests ───────────────────────────────────────────────────────

def _make_app(sb: SBStub, actor: dict) -> TestClient:
    app = FastAPI()
    app.include_router(admin_mocks_api.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: actor
    admin_mocks_api.get_supabase_admin = lambda: sb  # type: ignore[assignment]
    return TestClient(app, raise_server_exceptions=False)


def _author_actor():
    return {"id": ACTOR_ID, "role": "admin", "permissions": ["mock_questions:author"]}


def _publisher_actor():
    return {"id": ACTOR_ID, "role": "admin", "permissions": ["mock_questions:author", "mock_questions:publish"]}


class TestProjectionAPIEndpoints:
    def test_preview_returns_200_for_author(self):
        sb = _seed_sb()
        client = _make_app(sb, _author_actor())
        resp = client.get(f"/api/admin/mocks/pyq-papers/{PAPER_ID}/projection/preview")
        assert resp.status_code == 200
        data = resp.json()
        assert data["paper_id"] == PAPER_ID

    def test_preview_404_for_unknown_paper(self):
        sb = SBStub()
        client = _make_app(sb, _author_actor())
        missing = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        resp = client.get(f"/api/admin/mocks/pyq-papers/{missing}/projection/preview")
        assert resp.status_code == 404

    def test_sync_requires_publisher(self):
        sb = _seed_sb()
        client = _make_app(sb, _author_actor())  # only author, not publisher
        resp = client.post(
            f"/api/admin/mocks/pyq-papers/{PAPER_ID}/projection/sync",
            json={"audit_reason": "operator_test_sync"},
        )
        assert resp.status_code == 403

    def test_sync_returns_200_for_publisher(self):
        sb = _seed_sb()
        original_rpc = sb.rpc

        def ok_rpc(name, params=None):
            if name == "project_pyq_question_to_mock_bank":
                class _R:
                    def execute(self):
                        class _E:
                            data = [{"outcome": "created", "mock_question_id": "mock-new"}]
                        return _E()
                return _R()
            return original_rpc(name, params)

        sb.rpc = ok_rpc
        client = _make_app(sb, _publisher_actor())
        resp = client.post(
            f"/api/admin/mocks/pyq-papers/{PAPER_ID}/projection/sync",
            json={"audit_reason": "operator_manual_sync"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["attempted"] == 1

    def test_sync_blocked_row_returns_422_naming_the_question(self):
        """The old behaviour was a sanitised 500 that named nothing."""
        sb = _seed_sb()
        original_rpc = sb.rpc

        def fail_rpc(name, params=None):
            if name == "project_pyq_question_to_mock_bank":
                class _R:
                    def execute(self):
                        raise RuntimeError("invalid input syntax for type bytea")
                return _R()
            return original_rpc(name, params)

        sb.rpc = fail_rpc
        client = _make_app(sb, _publisher_actor())
        resp = client.post(
            f"/api/admin/mocks/pyq-papers/{PAPER_ID}/projection/sync",
            json={"audit_reason": "operator_manual_sync"},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["error"] == "projection_blocked"
        assert detail["question_id"] == Q_ID
        assert detail["blocked"][0]["question_id"] == Q_ID
        assert detail["blocked"][0]["reason"].startswith("content_hash_bytea_cast:")
        # Progress stays legible: the counts from the completed run come back.
        assert detail["attempted"] == 1
        assert detail["outcomes"]["blocked"] == 1

    def test_sync_no_longer_returns_a_sanitised_500_for_a_bad_row(self):
        sb = _seed_sb()
        original_rpc = sb.rpc

        def fail_rpc(name, params=None):
            if name == "project_pyq_question_to_mock_bank":
                class _R:
                    def execute(self):
                        raise RuntimeError("boom")
                return _R()
            return original_rpc(name, params)

        sb.rpc = fail_rpc
        client = _make_app(sb, _publisher_actor())
        resp = client.post(
            f"/api/admin/mocks/pyq-papers/{PAPER_ID}/projection/sync",
            json={"audit_reason": "operator_manual_sync"},
        )
        assert resp.status_code != 500
        assert resp.json()["detail"] != "internal error: projection_sync"

    def test_sync_audit_reason_required(self):
        """POST /sync without body must return 422 (audit_reason is required)."""
        sb = _seed_sb()
        client = _make_app(sb, _publisher_actor())
        resp = client.post(f"/api/admin/mocks/pyq-papers/{PAPER_ID}/projection/sync")
        assert resp.status_code == 422

    def test_sync_audit_reason_too_short(self):
        """audit_reason shorter than 8 chars must return 422."""
        sb = _seed_sb()
        client = _make_app(sb, _publisher_actor())
        resp = client.post(
            f"/api/admin/mocks/pyq-papers/{PAPER_ID}/projection/sync",
            json={"audit_reason": "short"},
        )
        assert resp.status_code == 422

    def test_sync_conflict_outcome_returns_409(self):
        """RPC returning outcome='conflict' must surface as 409 from the endpoint."""
        sb = _seed_sb()
        original_rpc = sb.rpc

        def conflict_rpc(name, params=None):
            if name == "project_pyq_question_to_mock_bank":
                class _R:
                    def execute(self):
                        class _E:
                            data = [{
                                "outcome": "conflict",
                                "mock_question_id": "mock-1",
                                "conflicting_pyq_id": "other-pyq",
                            }]
                        return _E()
                return _R()
            return original_rpc(name, params)

        sb.rpc = conflict_rpc
        client = _make_app(sb, _publisher_actor())
        resp = client.post(
            f"/api/admin/mocks/pyq-papers/{PAPER_ID}/projection/sync",
            json={"audit_reason": "operator_manual_sync"},
        )
        assert resp.status_code == 409

    def test_status_returns_200_for_author(self):
        sb = _seed_sb()
        client = _make_app(sb, _author_actor())
        resp = client.get(f"/api/admin/mocks/pyq-papers/{PAPER_ID}/projection/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "projection_counts" in data

    def test_invalid_paper_uuid_returns_422(self):
        sb = _seed_sb()
        client = _make_app(sb, _author_actor())
        resp = client.get("/api/admin/mocks/pyq-papers/not-a-uuid/projection/preview")
        assert resp.status_code == 422


# ─── MCQ exactly-one-correct validation fix ──────────────────────────────────

class TestMCQExactlyOneCorrect:
    """Verify create_question now rejects MCQ with 0 or 2+ correct options."""

    def _actor(self) -> dict:
        return {"id": ACTOR_ID, "role": "admin", "permissions": []}

    def _base_data(self, options=None) -> dict:
        return {
            "question_text": "Test question?",
            "question_type": "mcq",
            "options": options or [
                {"option_text": "A", "is_correct": True},
                {"option_text": "B", "is_correct": False},
            ],
        }

    def test_zero_correct_options_raises(self):
        sb = SBStub()
        opts = [{"option_text": "A", "is_correct": False}, {"option_text": "B", "is_correct": False}]
        with pytest.raises(ValueError, match="exactly one correct"):
            create_question(sb, self._actor(), self._base_data(options=opts))

    def test_two_correct_options_raises(self):
        sb = SBStub()
        opts = [{"option_text": "A", "is_correct": True}, {"option_text": "B", "is_correct": True}]
        with pytest.raises(ValueError, match="exactly one correct"):
            create_question(sb, self._actor(), self._base_data(options=opts))

    def test_exactly_one_correct_succeeds(self):
        sb = SBStub()
        opts = [{"option_text": "A", "is_correct": True}, {"option_text": "B", "is_correct": False}]
        result = create_question(sb, self._actor(), self._base_data(options=opts))
        assert result["question_type"] == "mcq"
