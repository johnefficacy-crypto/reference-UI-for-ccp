"""Per-exam action console read tests (Wave 4.6I-BE).

Asserts status parity with the 4.6H list, hard/advisory area grounding,
deterministic action ordering, CTA-route validity, evidence refs without
confidence, no-percentage guards, and fail-closed reads.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import admin_exam_intelligence as admin_api
from app.core.auth import get_current_user
from app.core.errors import DatabaseError
from app.exam_intelligence import console_detail as cd
from app.exam_intelligence import work_queue as wq
from tests.persona_questions._stub import SBStub


# Pin work_queue._now() so staleness calculations are deterministic.
# stale_cutoff = 2026-06-09; _RECENT (2026-06-16) is not stale.
_FIXED_NOW = datetime(2026, 6, 23, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pin_now(monkeypatch):
    monkeypatch.setattr(wq, "_now", lambda: _FIXED_NOW)

# After the deep-link fix (design-lock Section 7.2), all action CTAs must use the
# canonical /exams/:exam_id route with a per-area ?tab= parameter.
_CANONICAL_EXAMS_PREFIX = "/admin/exam-intelligence/exams/"


def _build_app(sb, role="super_admin"):
    app = FastAPI()
    app.include_router(admin_api.router, prefix="/api")
    admin_api.get_supabase_admin = lambda: sb  # type: ignore[assignment]
    user = {"id": "admin-1", "role": role,
            "permissions": ["exam_intelligence.review"] if role == "admin" else []}
    app.dependency_overrides[get_current_user] = lambda: user
    return app


# Relative to "now" so the fixture never rots past the 14-day staleness boundary.
from datetime import timedelta as _td  # noqa: E402
_RECENT = (_FIXED_NOW - _td(days=2)).isoformat()


class _Seed:
    def __init__(self):
        self.db = {t: [] for t in (
            "exams", "exam_phases", "exam_topic_coverage", "syllabus_topic_mentions",
            "exam_policy_updates", "pyq_papers", "pyq_questions",
            "pyq_question_topic_tags", "pyq_options", "organizations", "exam_families",
            "document_assets", "exam_competition_metrics", "mock_question_bank",
        )}

    def exam(self, eid, *, name, mode="core", phases=1, locked=1, reviewed=0,
             vpyq=0, pending_syl=0, org=None, family=None):
        self.db["exams"].append({
            "id": eid, "slug": eid, "name": name, "exam_type": "recruitment",
            "is_active": True, "exam_family_id": family, "management_mode": mode,
            "cadence": "annual", "conducting_organization_id": org,
        })
        for i in range(phases):
            self.db["exam_phases"].append({"id": f"{eid}-ph{i}", "exam_id": eid})
        for i in range(locked):
            self.db["exam_topic_coverage"].append(
                {"id": f"{eid}-cl{i}", "exam_id": eid, "reviewer_status": "locked", "created_at": _RECENT})
        for i in range(reviewed):
            self.db["exam_topic_coverage"].append(
                {"id": f"{eid}-cr{i}", "exam_id": eid, "reviewer_status": "reviewed", "created_at": _RECENT})
        if vpyq:
            self.db["pyq_papers"].append({"id": f"{eid}-pp", "exam_id": eid, "trust_status": "verified"})
            for i in range(vpyq):
                qid = f"{eid}-vq{i}"
                self.db["pyq_questions"].append(
                    {"id": qid, "pyq_paper_id": f"{eid}-pp", "reviewer_status": "verified", "created_at": _RECENT})
                self.db["pyq_question_topic_tags"].append(
                    {"id": f"{qid}-t", "question_id": qid, "reviewer_status": "verified", "created_at": _RECENT})
        for i in range(pending_syl):
            self.db["syllabus_topic_mentions"].append(
                {"id": f"{eid}-sp{i}", "exam_id": eid, "reviewer_status": "pending", "created_at": _RECENT})
        return self


def _full_seed():
    s = _Seed()
    s.db["organizations"].append({"id": "org1", "name": "Staff Selection Commission"})
    s.db["exam_families"].append({"id": "fam1", "name": "SSC Family"})
    s.exam("rdy", name="Ready", locked=1, vpyq=1, org="org1", family="fam1")        # ready
    s.exam("b1", name="No Phases", phases=0, locked=0)                                # blocked (2 gates)
    s.exam("b2", name="Reviewed Not Locked", locked=0, reviewed=2)                    # blocked
    s.exam("npyq", name="Missing Pyq", locked=1, vpyq=0)                              # needs_action
    s.exam("pend", name="Pending", locked=1, vpyq=1, pending_syl=1)                   # needs_action
    return s.db


def _client(role="super_admin", db=None):
    sb = SBStub(db if db is not None else _full_seed())
    return TestClient(_build_app(sb, role=role)), sb


def _detail(client, eid):
    r = client.get(f"/api/admin/exam-intelligence/console/exams/{eid}")
    return r


# ── Permission + 404 ────────────────────────────────────────────────────────

def test_admin_ok_non_admin_forbidden():
    ok, _ = _client(role="admin")
    assert _detail(ok, "rdy").status_code == 200
    denied, _ = _client(role="user")
    assert _detail(denied, "rdy").status_code == 403


def test_unknown_exam_404():
    client, _ = _client()
    assert _detail(client, "ghost").status_code == 404


# ── Status parity (the headline claim) ──────────────────────────────────────

def test_status_parity_with_list():
    client, _ = _client()
    listing = client.get("/api/admin/exam-intelligence/console/exams?active_state=all&limit=100").json()
    list_status = {row["id"]: row["status"] for row in listing["items"]}
    for eid, expected in list_status.items():
        body = _detail(client, eid).json()
        assert body["activation_verdict"]["status"] == expected, eid


def test_blocked_and_needs_action_classification():
    client, _ = _client()
    assert _detail(client, "b1").json()["activation_verdict"]["status"] == "blocked"
    assert _detail(client, "b2").json()["activation_verdict"]["status"] == "blocked"  # reviewed≠locked
    assert _detail(client, "npyq").json()["activation_verdict"]["status"] == "needs_action"  # missing pyq advisory
    assert _detail(client, "rdy").json()["activation_verdict"]["status"] == "ready"


# ── Mock readiness is separate + advisory ───────────────────────────────────

def test_mock_readiness_does_not_change_activation_status():
    client, _ = _client()
    body = _detail(client, "rdy").json()
    assert body["mock_readiness"]["status"] in {"blocked", "thin_bank", "ready", "unknown"}
    assert body["activation_verdict"]["status"] == "ready"  # mock is advisory, never gates


def test_thin_mock_bank_is_advisory_and_status_unchanged(monkeypatch):
    monkeypatch.setattr(cd, "_mock_readiness",
                        lambda sb, exam_id: {"status": "thin_bank", "detail": "thin"})
    client, _ = _client()
    body = _detail(client, "rdy").json()
    assert body["mock_readiness"]["status"] == "thin_bank"
    assert body["activation_verdict"]["status"] == "ready"  # advisory never blocks
    mock_chk = next(c for c in body["activation_checks"] if c["area"] == "mock_readiness")
    assert mock_chk["gate"] == "advisory"
    mock_item = next((i for i in body["action_queue"] if i["area"] == "mock_readiness"), None)
    assert mock_item is not None and mock_item["severity"] == "advisory"


# ── Action queue ordering + CTA routes ──────────────────────────────────────

def test_action_queue_ordered_blockers_then_actions_and_routes_valid():
    client, _ = _client()
    body = _detail(client, "b1").json()
    sev_rank = {"blocker": 0, "action": 1, "advisory": 2}
    ranks = [sev_rank[i["severity"]] for i in body["action_queue"]]
    assert ranks == sorted(ranks)
    assert any(i["severity"] == "blocker" for i in body["action_queue"])
    for i in body["action_queue"]:
        # design-lock Section 7.2: all CTAs use canonical /exams/:id route with tab param
        assert i["cta_route"].startswith(_CANONICAL_EXAMS_PREFIX), i["cta_route"]
        assert "tab=" in i["cta_route"], i["cta_route"]
        assert i["status"] == "open" and i["entity_id"] is None
    # publish is the outcome, not an action item
    assert all(i["area"] != "publish" for i in body["action_queue"])


def test_cta_routes_are_per_area_deep_links():
    """After the deep-link fix every CTA must have a per-area route with ?tab=
    (design-lock Section 7.2). Generic 'Open workspace' labels must not appear."""
    client, _ = _client()
    for eid in ["b1", "b2", "npyq", "pend"]:
        body = _detail(client, eid).json()
        for item in body["action_queue"]:
            assert item["cta_route"].startswith(_CANONICAL_EXAMS_PREFIX), (eid, item)
            assert "tab=" in item["cta_route"], (eid, item)
            assert item["cta_label"] != "Open workspace", (eid, item["area"])


def test_cta_setup_goes_to_setup_tab():
    client, _ = _client()
    body = _detail(client, "b1").json()
    setup_item = next(i for i in body["action_queue"] if i["area"] == "setup")
    assert "tab=setup" in setup_item["cta_route"]
    assert setup_item["cta_label"] == "Go to Setup"


def test_cta_syllabus_has_status_pending():
    client, _ = _client()
    body = _detail(client, "b1").json()
    syl = next(i for i in body["action_queue"] if i["area"] == "syllabus")
    assert "tab=syllabus" in syl["cta_route"]
    assert "status=pending" in syl["cta_route"]


def test_cta_topic_coverage_uses_syllabus_tab_pending_review():
    """topic_coverage CTA links to syllabus tab with status=pending_review (design-lock 7.2)."""
    client, _ = _client()
    body = _detail(client, "b1").json()
    tc = next(i for i in body["action_queue"] if i["area"] == "topic_coverage")
    assert "tab=syllabus" in tc["cta_route"]
    assert "status=pending_review" in tc["cta_route"]
    assert tc["cta_label"] == "Review unlocked rows"


def test_checks_hard_advisory_and_unknown_grounding():
    client, _ = _client()
    checks = {c["area"]: c for c in _detail(client, "rdy").json()["activation_checks"]}
    assert checks["setup"]["gate"] == "hard"
    assert checks["topic_coverage"]["gate"] == "hard"
    assert checks["publish"]["gate"] == "hard"
    assert checks["pyq"]["gate"] == "advisory"
    # all 9 areas present, stages reference them
    assert set(checks) == {"setup", "documents", "syllabus", "topic_coverage", "pyq",
                           "updates", "competition", "mock_readiness", "publish"}
    # An area with NO computable source renders unknown, never a fabricated state:
    # b1 has no phases, so mock readiness is uncomputable → unknown.
    b1_checks = {c["area"]: c for c in _detail(client, "b1").json()["activation_checks"]}
    assert b1_checks["mock_readiness"]["state"] == "unknown"


# ── Guards ──────────────────────────────────────────────────────────────────

def _walk(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def test_no_percentage_or_confidence_fields_anywhere():
    client, _ = _client()
    forbidden = {"score_percent", "confidence_score", "confidence_percent"}
    body = _detail(client, "rdy").json()
    assert not (set(_walk(body)) & forbidden)


def test_evidence_refs_are_kind_rowid_only():
    client, _ = _client()
    body = _detail(client, "pend").json()
    for ref in body["evidence_refs"]:
        assert set(ref.keys()) == {"kind", "row_id"}
    # exam identity exposes names, never the raw org id
    assert "conducting_organization_id" not in set(_walk(body))
    assert body["exam"]["organization_name"] is None or isinstance(body["exam"]["organization_name"], str)


def test_org_and_family_names_resolved():
    client, _ = _client()
    body = _detail(client, "rdy").json()
    assert body["exam"]["organization_name"] == "Staff Selection Commission"
    assert body["exam"]["family_name"] == "SSC Family"


# ── Fail-closed ─────────────────────────────────────────────────────────────

class _RaisingQuery:
    def __getattr__(self, _n):
        return lambda *a, **k: self

    def execute(self):
        raise RuntimeError("simulated DB failure")


class FailingSBStub(SBStub):
    def __init__(self, db, fail_table):
        super().__init__(db)
        self.fail_table = fail_table

    def table(self, name):
        if name == self.fail_table:
            return _RaisingQuery()
        return super().table(name)


def test_required_read_failure_raises():
    sb = FailingSBStub(_full_seed(), "exam_topic_coverage")
    with pytest.raises(DatabaseError):
        cd.build_console_detail(sb, "rdy")


def test_endpoint_returns_500_on_read_failure_not_fabricated_verdict():
    sb = FailingSBStub(_full_seed(), "exam_topic_coverage")
    client = TestClient(_build_app(sb), raise_server_exceptions=False)
    r = client.get("/api/admin/exam-intelligence/console/exams/rdy")
    assert r.status_code == 500


# ── Reason parity: every classifier flag has a matching check + action ──────

from app.exam_intelligence import work_queue as _wq  # noqa: E402
from tests.persona_questions._stub import _Query  # noqa: E402


def _causal_base():
    return {
        "exams": [{"id": "e", "slug": "e", "name": "E", "exam_type": "recruitment",
                   "is_active": True, "management_mode": "core", "cadence": "annual",
                   "exam_family_id": None, "conducting_organization_id": None}],
        "exam_phases": [{"id": "e-ph", "exam_id": "e"}],
        "exam_topic_coverage": [{"id": "e-cl", "exam_id": "e", "reviewer_status": "locked", "created_at": _RECENT}],
        "pyq_papers": [{"id": "e-pp", "exam_id": "e", "trust_status": "verified"}],
        "pyq_questions": [{"id": "e-q1", "pyq_paper_id": "e-pp", "reviewer_status": "verified", "created_at": _RECENT}],
        "pyq_question_topic_tags": [{"id": "e-t1", "question_id": "e-q1", "reviewer_status": "verified", "created_at": _RECENT}],
    }


def _list_status(db, eid):
    client = TestClient(_build_app(SBStub(db)))
    listing = client.get("/api/admin/exam-intelligence/console/exams?active_state=all&limit=100").json()
    return {r["id"]: r["status"] for r in listing["items"]}.get(eid)


def _assert_causal(db, area, kind, row_id):
    client, _ = _client(db=db)
    detail = _detail(client, "e").json()
    # detail status still matches the list, and is needs_action (advisory pending)
    assert detail["activation_verdict"]["status"] == _list_status(db, "e") == "needs_action"
    chk = next(c for c in detail["activation_checks"] if c["area"] == area)
    assert chk["state"] == "needs_action"
    item = next((i for i in detail["action_queue"] if i["area"] == area), None)
    assert item is not None
    assert detail["action_queue"]  # non-empty
    refs = {(r["kind"], r["row_id"]) for r in chk["evidence_refs"]}
    assert (kind, row_id) in refs


def test_reason_parity_pending_coverage():
    db = _causal_base()
    db["exam_topic_coverage"].append({"id": "e-cp", "exam_id": "e", "reviewer_status": "pending_review", "created_at": _RECENT})
    _assert_causal(db, "topic_coverage", "exam_topic_coverage", "e-cp")


def test_reason_parity_pending_question():
    db = _causal_base()
    db["pyq_questions"].append({"id": "e-q2", "pyq_paper_id": "e-pp", "reviewer_status": "pending", "created_at": _RECENT})
    _assert_causal(db, "pyq", "pyq_question", "e-q2")


def test_reason_parity_pending_topic_tag():
    db = _causal_base()
    db["pyq_questions"].append({"id": "e-q2", "pyq_paper_id": "e-pp", "reviewer_status": "verified", "created_at": _RECENT})
    db["pyq_question_topic_tags"].append({"id": "e-t2", "question_id": "e-q2", "reviewer_status": "pending", "created_at": _RECENT})
    _assert_causal(db, "pyq", "pyq_question_topic_tag", "e-t2")


def test_reason_parity_pending_option():
    db = _causal_base()
    db["pyq_options"] = [{"id": "e-o2", "question_id": "e-q1", "reviewer_status": "pending", "created_at": _RECENT}]
    _assert_causal(db, "pyq", "pyq_option", "e-o2")


def test_reason_parity_pending_policy_update():
    db = _causal_base()
    db["exam_policy_updates"] = [{"id": "e-u1", "exam_id": "e", "reviewer_status": "pending", "created_at": _RECENT}]
    _assert_causal(db, "updates", "exam_policy_updates", "e-u1")


# ── Strict updates / competition failure propagation ────────────────────────

def test_updates_read_failure_returns_500():
    sb = FailingSBStub(_full_seed(), "exam_policy_updates")
    client = TestClient(_build_app(sb), raise_server_exceptions=False)
    assert client.get("/api/admin/exam-intelligence/console/exams/rdy").status_code == 500


def test_competition_read_failure_returns_500():
    sb = FailingSBStub(_full_seed(), "exam_competition_metrics")
    client = TestClient(_build_app(sb), raise_server_exceptions=False)
    assert client.get("/api/admin/exam-intelligence/console/exams/rdy").status_code == 500


# ── Paging: later-page rows affect per-area state ───────────────────────────

class _RangeQuery(_Query):
    def __init__(self, name, db):
        super().__init__(name, db)
        self._range = None

    def range(self, start, end, **kw):
        self._range = (start, end)
        return self

    def execute(self):
        res = super().execute()
        if self._range is not None:
            s, e = self._range
            res.data = res.data[s:e + 1]
        return res


class RangeAwareSBStub(SBStub):
    def table(self, name):
        return _RangeQuery(name, self.db)


def test_pending_coverage_on_later_page_is_counted(monkeypatch):
    monkeypatch.setattr(_wq, "_PAGE_SIZE", 2)
    db = _causal_base()
    # locked + two filler locked rows + a pending row last (id order → page 2+)
    db["exam_topic_coverage"] = [
        {"id": "e-cl1", "exam_id": "e", "reviewer_status": "locked", "created_at": _RECENT},
        {"id": "e-cl2", "exam_id": "e", "reviewer_status": "locked", "created_at": _RECENT},
        {"id": "e-cp9", "exam_id": "e", "reviewer_status": "pending_review", "created_at": _RECENT},
    ]
    client = TestClient(_build_app(RangeAwareSBStub(db)))
    detail = client.get("/api/admin/exam-intelligence/console/exams/e").json()
    tc = next(c for c in detail["activation_checks"] if c["area"] == "topic_coverage")
    assert tc["state"] == "needs_action"  # pending row on page 2 not truncated
    assert ("exam_topic_coverage", "e-cp9") in {(r["kind"], r["row_id"]) for r in tc["evidence_refs"]}


def test_competition_selected_row_on_later_page(monkeypatch):
    monkeypatch.setattr(_wq, "_PAGE_SIZE", 2)
    db = _causal_base()
    db["exam_competition_metrics"] = [
        {"id": "cm1", "exam_id": "e", "reviewer_status": "reviewed", "created_at": "2026-01-01T00:00:00+00:00"},
        {"id": "cm2", "exam_id": "e", "reviewer_status": "reviewed", "created_at": "2026-02-01T00:00:00+00:00"},
        {"id": "cm3", "exam_id": "e", "reviewer_status": "locked", "created_at": "2026-03-01T00:00:00+00:00"},
    ]
    client = TestClient(_build_app(RangeAwareSBStub(db)))
    detail = client.get("/api/admin/exam-intelligence/console/exams/e").json()
    comp = next(c for c in detail["activation_checks"] if c["area"] == "competition")
    assert comp["state"] == "done"
    # locked (cm3, page 2) wins precedence over reviewed
    assert {(r["kind"], r["row_id"]) for r in comp["evidence_refs"]} == {("exam_competition_metrics", "cm3")}


# ── Blocked coverage preserves pending/stale reasons + evidence ─────────────

_STALE = (_FIXED_NOW - _td(days=90)).isoformat()  # anchored to _FIXED_NOW; well past the staleness boundary
_CLASSIFIER_AREAS = {"setup", "topic_coverage", "pyq", "syllabus", "updates"}


def _blocked_pending_db(stale=False):
    db = _causal_base()
    # No locked coverage (force blocked) + one pending coverage row.
    db["exam_topic_coverage"] = [
        {"id": "e-cp", "exam_id": "e", "reviewer_status": "pending_review",
         "created_at": _STALE if stale else _RECENT},
    ]
    return db


def test_blocked_coverage_with_pending_keeps_reasons_and_evidence():
    db = _blocked_pending_db()
    client, _ = _client(db=db)
    detail = _detail(client, "e").json()
    assert detail["activation_verdict"]["status"] == _list_status(db, "e") == "blocked"
    tc = next(c for c in detail["activation_checks"] if c["area"] == "topic_coverage")
    assert tc["state"] == "blocked" and tc["gate"] == "hard"
    assert tc["reasons"] == ["no_locked_coverage", "pending_review"]  # deterministic, no dup
    refs = {(r["kind"], r["row_id"]) for r in tc["evidence_refs"]}
    assert ("exam_topic_coverage", "e-cp") in refs
    item = next(i for i in detail["action_queue"] if i["area"] == "topic_coverage")
    assert {(r["kind"], r["row_id"]) for r in item["evidence_refs"]} == refs


def test_blocked_coverage_with_stale_pending_adds_stale_reason():
    db = _blocked_pending_db(stale=True)
    client, _ = _client(db=db)
    detail = _detail(client, "e").json()
    assert detail["activation_verdict"]["status"] == _list_status(db, "e") == "blocked"
    tc = next(c for c in detail["activation_checks"] if c["area"] == "topic_coverage")
    assert tc["reasons"] == ["no_locked_coverage", "pending_review", "stale_review_queue"]
    assert ("exam_topic_coverage", "e-cp") in {(r["kind"], r["row_id"]) for r in tc["evidence_refs"]}


def test_every_verdict_reason_has_a_classifier_owned_check():
    """Strengthened reason parity: each verdict reason token must be carried by
    at least one classifier-owned, non-publish check — not merely 'some
    explanation exists'."""
    client, _ = _client()
    for db in (_full_seed(), _blocked_pending_db(), _blocked_pending_db(stale=True)):
        c2, _ = _client(db=db)
        listing = c2.get("/api/admin/exam-intelligence/console/exams?active_state=all&limit=100").json()
        for row in listing["items"]:
            body = _detail(c2, row["id"]).json()
            owned = set()
            for chk in body["activation_checks"]:
                if chk["area"] in _CLASSIFIER_AREAS:
                    owned.update(chk["reasons"])
            for token in body["activation_verdict"]["reasons"]:
                assert token in owned, (row["id"], token, owned)
