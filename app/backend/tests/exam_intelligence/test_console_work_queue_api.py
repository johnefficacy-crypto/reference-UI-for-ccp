"""Endpoint + integration tests for the console work queue (Wave 4.6H).

Covers the canonical status model, base-filter parity with /exams, workflow
filters, deterministic sort, pagination, summary scoping, response guards, the
three-gate verified-PYQ definition, full child-read paging (signals on later
pages), required-read failure propagation, and bounded (no per-exam) reads.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import admin_exam_intelligence as admin_api
from app.core.auth import get_current_user
from app.core.errors import DatabaseError
from app.exam_intelligence import work_queue as wq
from tests.persona_questions._stub import SBStub, _Query


# ── Time pinning ─────────────────────────────────────────────────────────────
# Anchors work_queue._now() to 2026-06-23 UTC so staleness calculations are
# deterministic.  stale_cutoff = 2026-06-09; _RECENT (2026-06-16) is not
# stale; _STALE (2026-01-01) is stale.  Without this, _RECENT sits on the
# 14-day boundary and tests fail depending on the time of day tests run.
_FIXED_NOW = datetime(2026, 6, 23, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pin_now(monkeypatch):
    monkeypatch.setattr(wq, "_now", lambda: _FIXED_NOW)


# ── Harness ─────────────────────────────────────────────────────────────────

class CountingSBStub(SBStub):
    """Counts table() calls, to prove reads don't scale per-exam."""

    def __init__(self, db=None):
        super().__init__(db)
        self.table_calls = 0

    def table(self, name):
        self.table_calls += 1
        return super().table(name)


class _RangeQuery(_Query):
    """A _Query that honours .range() by slicing ordered results."""

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
            res.data = res.data[s : e + 1]
        return res


class RangeAwareSBStub(SBStub):
    """Honours .range() so real pagination can be exercised."""

    def table(self, name):
        return _RangeQuery(name, self.db)


class _RaisingQuery:
    """A query whose builder methods are no-ops and whose execute() raises."""

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        raise RuntimeError("simulated DB failure")


class FailingSBStub(SBStub):
    """Raises on execute() for one target table; normal elsewhere."""

    def __init__(self, db, fail_table):
        super().__init__(db)
        self.fail_table = fail_table

    def table(self, name):
        if name == self.fail_table:
            return _RaisingQuery()
        return super().table(name)


def _build_app(sb, role="super_admin"):
    app = FastAPI()
    app.include_router(admin_api.router, prefix="/api")
    admin_api.get_supabase_admin = lambda: sb  # type: ignore[assignment]
    user = {"id": "admin-1", "role": role,
            "permissions": ["exam_intelligence.review"] if role == "admin" else []}
    app.dependency_overrides[get_current_user] = lambda: user
    return app


# Derived from _FIXED_NOW, the same clock the code under test is pinned to.
# These must NOT use the real wall clock: _now() is monkeypatched to _FIXED_NOW,
# so a real-clock fixture drifts against a frozen stale_cutoff and eventually
# crosses it (on 2026-09-07, now-90d landed past the 2026-06-09 cutoff and every
# "stale" row read as fresh).
from datetime import timedelta as _td  # noqa: E402
_RECENT = (_FIXED_NOW - _td(days=2)).isoformat()    # well within the 14-day window
_STALE = (_FIXED_NOW - _td(days=90)).isoformat()    # well past it


class _Seed:
    def __init__(self):
        self.db = {t: [] for t in (
            "exams", "exam_phases", "exam_topic_coverage", "syllabus_topic_mentions",
            "exam_policy_updates", "pyq_papers", "pyq_questions",
            "pyq_question_topic_tags", "pyq_options", "organizations",
        )}

    def exam(self, eid, *, name, mode, phases=1, locked=1, reviewed=0,
             vpyq=0, q_no_tag=0, pending_pyq=0, pending_syl=0, stale_syl=0,
             active=True, org=None):
        self.db["exams"].append({
            "id": eid, "slug": eid, "name": name, "exam_type": "recruitment",
            "is_active": active, "exam_family_id": None, "management_mode": mode,
            "cadence": "annual", "conducting_organization_id": org,
        })
        for i in range(phases):
            self.db["exam_phases"].append({"id": f"{eid}-ph{i}", "exam_id": eid})
        for i in range(locked):
            self.db["exam_topic_coverage"].append(
                {"id": f"{eid}-cl{i}", "exam_id": eid, "reviewer_status": "locked",
                 "created_at": _RECENT})
        for i in range(reviewed):
            self.db["exam_topic_coverage"].append(
                {"id": f"{eid}-cr{i}", "exam_id": eid, "reviewer_status": "reviewed",
                 "created_at": _RECENT})
        # PYQ — one verified paper hosts all questions.
        if vpyq or q_no_tag or pending_pyq:
            self.db["pyq_papers"].append(
                {"id": f"{eid}-pp", "exam_id": eid, "trust_status": "verified"})
        for i in range(vpyq):  # full 3-gate verified question
            qid = f"{eid}-vq{i}"
            self.db["pyq_questions"].append(
                {"id": qid, "pyq_paper_id": f"{eid}-pp", "reviewer_status": "verified",
                 "created_at": _RECENT})
            self.db["pyq_question_topic_tags"].append(
                {"id": f"{qid}-t", "question_id": qid, "reviewer_status": "verified",
                 "created_at": _RECENT})
        for i in range(q_no_tag):  # verified question, NO verified tag → gate 3 fails
            self.db["pyq_questions"].append(
                {"id": f"{eid}-nq{i}", "pyq_paper_id": f"{eid}-pp",
                 "reviewer_status": "verified", "created_at": _RECENT})
        for i in range(pending_pyq):
            self.db["pyq_questions"].append(
                {"id": f"{eid}-pq{i}", "pyq_paper_id": f"{eid}-pp",
                 "reviewer_status": "pending", "created_at": _RECENT})
        for i in range(pending_syl):
            self.db["syllabus_topic_mentions"].append(
                {"id": f"{eid}-sp{i}", "exam_id": eid, "reviewer_status": "pending",
                 "created_at": _RECENT})
        for i in range(stale_syl):
            self.db["syllabus_topic_mentions"].append(
                {"id": f"{eid}-ss{i}", "exam_id": eid, "reviewer_status": "pending",
                 "created_at": _STALE})
        return self


def _seed():
    s = _Seed()
    s.db["organizations"].append({"id": "org1", "name": "Staff Selection Commission"})
    s.exam("b1", name="Blocked Setup", mode=None, phases=0, locked=0, org="org1")
    s.exam("b2", name="Blocked Coverage", mode="core", locked=0, reviewed=2)
    s.exam("rdy", name="Ready Exam", mode="core", locked=1, vpyq=1)
    s.exam("npyq", name="Needs Pyq", mode="light", locked=1, q_no_tag=1)  # verified q, no tag
    s.exam("pend", name="Pending Review", mode="core", locked=1, vpyq=1, pending_syl=1)
    s.exam("stale", name="Stale Review", mode="index_only", locked=1, vpyq=1, stale_syl=1)
    s.exam("arch", name="Archived", mode="archive", locked=1, vpyq=1)
    return s.db


def _client(role="super_admin", db=None, stub_cls=CountingSBStub):
    sb = stub_cls(db if db is not None else _seed())
    return TestClient(_build_app(sb, role=role)), sb


# ── Permission ──────────────────────────────────────────────────────────────

def test_permission_admin_ok_user_forbidden():
    ok, _ = _client(role="admin")
    assert ok.get("/api/admin/exam-intelligence/console/exams").status_code == 200
    assert ok.get("/api/admin/exam-intelligence/console/summary").status_code == 200
    denied, _ = _client(role="user")
    assert denied.get("/api/admin/exam-intelligence/console/exams").status_code == 403
    assert denied.get("/api/admin/exam-intelligence/console/summary").status_code == 403


# ── Status model + scope ────────────────────────────────────────────────────

def test_default_scope_excludes_archive_and_classifies():
    client, _ = _client()
    body = client.get("/api/admin/exam-intelligence/console/exams?limit=100").json()
    by_id = {r["id"]: r for r in body["items"]}
    assert "arch" not in by_id
    assert by_id["b1"]["status"] == "blocked" and by_id["b1"]["blocker_count"] == 2
    assert by_id["b2"]["status"] == "blocked" and "missing_coverage" in by_id["b2"]["flags"]
    assert by_id["rdy"]["status"] == "ready" and by_id["rdy"]["flags"] == []
    assert by_id["npyq"]["status"] == "needs_action" and "missing_pyq" in by_id["npyq"]["flags"]
    assert by_id["stale"]["flags"].count("stale_review_queue") == 1
    assert by_id["rdy"]["locked_coverage_count"] == 1
    assert by_id["rdy"]["verified_pyq_count"] == 1
    assert by_id["npyq"]["verified_pyq_count"] == 0 and by_id["npyq"]["total_pyq_count"] == 1


# ── Three-gate verified_pyq_count (aggregate-level) ─────────────────────────

def _agg_one(db):
    sb = SBStub(db)
    exams = db["exams"]
    return wq.aggregate(sb, exams)[exams[0]["id"]]


def _pyq_db(question_status, tags, *, paper_trust="verified"):
    return {
        "exams": [{"id": "e", "slug": "e", "name": "E"}],
        "pyq_papers": [{"id": "p", "exam_id": "e", "trust_status": paper_trust}],
        "pyq_questions": [{"id": "q", "pyq_paper_id": "p",
                           "reviewer_status": question_status, "created_at": _RECENT}],
        "pyq_question_topic_tags": [
            {"id": f"t{i}", "question_id": "q", "reviewer_status": st, "created_at": _RECENT}
            for i, st in enumerate(tags)
        ],
    }


def test_verified_pyq_gate_no_tag_is_zero():
    assert _agg_one(_pyq_db("verified", []))["verified_pyq_count"] == 0


def test_verified_pyq_gate_pending_tag_is_zero():
    assert _agg_one(_pyq_db("verified", ["pending"]))["verified_pyq_count"] == 0


def test_verified_pyq_gate_all_three_is_one():
    assert _agg_one(_pyq_db("verified", ["verified"]))["verified_pyq_count"] == 1


def test_verified_pyq_two_verified_tags_count_one_distinct():
    assert _agg_one(_pyq_db("verified", ["verified", "verified"]))["verified_pyq_count"] == 1


def test_verified_pyq_unverified_paper_is_zero():
    assert _agg_one(_pyq_db("verified", ["verified"], paper_trust="pending"))["verified_pyq_count"] == 0


def test_verified_pyq_pending_question_is_zero():
    assert _agg_one(_pyq_db("pending", ["verified"]))["verified_pyq_count"] == 0


# ── Base-filter parity with /exams ──────────────────────────────────────────

def test_management_mode_null_sentinel_matches_unclassified():
    client, _ = _client()
    body = client.get("/api/admin/exam-intelligence/console/exams?management_mode=__null__&limit=100").json()
    assert [r["id"] for r in body["items"]] == ["b1"]


def test_management_mode_archive_includes_only_archive():
    client, _ = _client()
    body = client.get("/api/admin/exam-intelligence/console/exams?management_mode=archive&limit=100").json()
    assert [r["id"] for r in body["items"]] == ["arch"]


def test_q_filters_by_name():
    client, _ = _client()
    body = client.get("/api/admin/exam-intelligence/console/exams?q=ready&limit=100").json()
    assert [r["id"] for r in body["items"]] == ["rdy"]


# ── Workflow filters ────────────────────────────────────────────────────────

def test_workflow_primary_and_flag_filters():
    client, _ = _client()
    blocked = client.get("/api/admin/exam-intelligence/console/exams?workflow=blocked&limit=100").json()
    assert {r["id"] for r in blocked["items"]} == {"b1", "b2"}
    stale = client.get("/api/admin/exam-intelligence/console/exams?workflow=stale_review_queue&limit=100").json()
    assert {r["id"] for r in stale["items"]} == {"stale"}
    mpyq = client.get("/api/admin/exam-intelligence/console/exams?workflow=missing_pyq&limit=100").json()
    assert {r["id"] for r in mpyq["items"]} == {"b1", "b2", "npyq"}


def test_thin_mock_bank_workflow_is_rejected():
    client, _ = _client()
    assert client.get("/api/admin/exam-intelligence/console/exams?workflow=thin_mock_bank").status_code == 422


def test_unknown_workflow_and_sort_rejected():
    client, _ = _client()
    assert client.get("/api/admin/exam-intelligence/console/exams?workflow=on_fire").status_code == 422
    assert client.get("/api/admin/exam-intelligence/console/exams?sort=banana").status_code == 422


# ── Sort ────────────────────────────────────────────────────────────────────

def test_blockers_first_orders_blocked_then_needs_then_ready():
    client, _ = _client()
    items = client.get("/api/admin/exam-intelligence/console/exams?sort=blockers_first&limit=100").json()["items"]
    ranks = [wq.STATUS_RANK[r["status"]] for r in items]
    assert ranks == sorted(ranks)
    assert items[0]["id"] == "b1" and items[1]["id"] == "b2"  # 2 blockers before 1


def test_name_sort_is_alphabetical():
    client, _ = _client()
    items = client.get("/api/admin/exam-intelligence/console/exams?sort=name&limit=100").json()["items"]
    assert [r["name"] for r in items] == sorted(r["name"] for r in items)


def test_management_lane_sort_ranks_lanes():
    client, _ = _client()
    items = client.get("/api/admin/exam-intelligence/console/exams?sort=management_lane&limit=100").json()["items"]
    lane_ranks = [wq._LANE_RANK.get(r["management_mode"], wq._LANE_RANK[None]) for r in items]
    assert lane_ranks == sorted(lane_ranks)


# ── Pagination (after filter+sort) ──────────────────────────────────────────

def test_pagination_applies_after_filter_and_sort():
    client, _ = _client()
    p0 = client.get("/api/admin/exam-intelligence/console/exams?sort=name&limit=3&offset=0").json()
    p1 = client.get("/api/admin/exam-intelligence/console/exams?sort=name&limit=3&offset=3").json()
    assert p0["total_count"] == 6 and p1["total_count"] == 6
    assert p0["count"] == 3 and p0["has_next"] is True
    full = client.get("/api/admin/exam-intelligence/console/exams?sort=name&limit=100").json()["items"]
    assert [r["id"] for r in p0["items"]] == [r["id"] for r in full[:3]]
    assert [r["id"] for r in p1["items"]] == [r["id"] for r in full[3:6]]


# ── Summary ─────────────────────────────────────────────────────────────────

def test_summary_five_counts_primaries_sum_to_total():
    client, _ = _client()
    s = client.get("/api/admin/exam-intelligence/console/summary").json()
    assert s["blocked"] + s["needs_action"] + s["ready"] == s["total_count"] == 6
    assert s["blocked"] == 2 and s["ready"] == 1 and s["needs_action"] == 3
    assert s["pending_review"] == 2  # pend + stale
    assert s["stale_review_queue"] == 1
    assert "thin_mock_bank" not in s
    assert "stale_official_intelligence" not in s
    assert "generated_at" in s


def test_summary_shares_scope_with_list_under_filters():
    client, _ = _client()
    s = client.get("/api/admin/exam-intelligence/console/summary?q=ready").json()
    lst = client.get("/api/admin/exam-intelligence/console/exams?q=ready&limit=100").json()
    assert s["total_count"] == lst["total_count"] == 1
    assert s["ready"] == 1 and s["blocked"] == 0 and s["needs_action"] == 0


# ── Response guards ─────────────────────────────────────────────────────────

def _walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_keys(v)


def test_no_forbidden_fields_in_responses():
    client, _ = _client()
    forbidden = {"score_percent", "confidence_score", "confidence_percent",
                 "conducting_organization_id", "state", "jurisdiction", "reviewer_status"}
    for path in ("/api/admin/exam-intelligence/console/exams?limit=100",
                 "/api/admin/exam-intelligence/console/summary"):
        keys = set(_walk_keys(client.get(path).json()))
        assert not (keys & forbidden), keys & forbidden


def test_organization_name_exposed_not_raw_id():
    client, _ = _client()
    body = client.get("/api/admin/exam-intelligence/console/exams?limit=100").json()
    assert next(r for r in body["items"] if r["id"] == "b1")["organization_name"] == "Staff Selection Commission"
    assert next(r for r in body["items"] if r["id"] == "rdy")["organization_name"] is None


# ── Full child-read paging: signals on later pages ──────────────────────────

def _paging_db():
    # One exam; locked coverage, verified tag, and stale row each land on page 2
    # when page size is 2 (ids ordered).
    return {
        "exams": [{"id": "e", "slug": "e", "name": "E", "exam_type": "recruitment",
                   "is_active": True, "management_mode": "core", "cadence": "annual",
                   "exam_family_id": None, "conducting_organization_id": None}],
        "exam_phases": [{"id": "ph", "exam_id": "e"}],
        "exam_topic_coverage": [
            {"id": "c1", "exam_id": "e", "reviewer_status": "draft", "created_at": _RECENT},
            {"id": "c2", "exam_id": "e", "reviewer_status": "draft", "created_at": _RECENT},
            {"id": "c3", "exam_id": "e", "reviewer_status": "locked", "created_at": _RECENT},
        ],
        "pyq_papers": [{"id": "p", "exam_id": "e", "trust_status": "verified"}],
        "pyq_questions": [{"id": "q", "pyq_paper_id": "p", "reviewer_status": "verified",
                           "created_at": _RECENT}],
        "pyq_question_topic_tags": [
            {"id": "t1", "question_id": "q", "reviewer_status": "pending", "created_at": _RECENT},
            {"id": "t2", "question_id": "q", "reviewer_status": "pending", "created_at": _RECENT},
            {"id": "t3", "question_id": "q", "reviewer_status": "verified", "created_at": _RECENT},
        ],
        "syllabus_topic_mentions": [
            {"id": "s1", "exam_id": "e", "reviewer_status": "pending", "created_at": _RECENT},
            {"id": "s2", "exam_id": "e", "reviewer_status": "pending", "created_at": _RECENT},
            {"id": "s3", "exam_id": "e", "reviewer_status": "pending", "created_at": _STALE},
        ],
    }


def test_signals_on_later_pages_are_not_truncated(monkeypatch):
    monkeypatch.setattr(wq, "_PAGE_SIZE", 2)
    sb = RangeAwareSBStub(_paging_db())
    rows = wq.build_classified_rows(sb, {
        "q_sanitized": "", "exam_type": None, "active_state": "active",
        "management_mode": None, "cadence": None, "exam_family_id": None,
    })
    r = rows[0]
    assert r["locked_coverage_count"] == 1          # c3 (page 2) counted
    assert "missing_coverage" not in r["flags"]
    assert r["verified_pyq_count"] == 1             # t3 verified tag (page 2) counted
    assert r["total_pyq_count"] == 1                # no duplicate questions across pages
    assert "stale_review_queue" in r["flags"]       # s3 (page 2) counted


def test_paging_produces_no_duplicate_rows(monkeypatch):
    monkeypatch.setattr(wq, "_PAGE_SIZE", 2)
    sb = RangeAwareSBStub(_paging_db())
    # 3 pending syllabus rows + 2 pending PYQ tags, each spanning >1 page.
    # Correct (de-duplicated) total is 5; duplication across pages would give 10.
    agg = wq.aggregate(sb, sb.db["exams"])["e"]
    assert agg["pending_review_count"] == 5


# ── Required-read failure propagation ───────────────────────────────────────

_BASE = {"q_sanitized": "", "exam_type": None, "active_state": "active",
         "management_mode": None, "cadence": None, "exam_family_id": None}


@pytest.mark.parametrize("fail_table", ["exams", "exam_topic_coverage", "pyq_papers", "organizations"])
def test_required_read_failure_raises_databaseerror(fail_table):
    db = _seed()
    sb = FailingSBStub(db, fail_table)
    with pytest.raises(DatabaseError):
        wq.build_classified_rows(sb, _BASE)


@pytest.mark.parametrize("fail_table", ["exams", "exam_topic_coverage", "pyq_papers", "organizations"])
def test_endpoint_returns_500_not_fabricated_truth_on_failure(fail_table):
    sb = FailingSBStub(_seed(), fail_table)
    app = _build_app(sb)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/api/admin/exam-intelligence/console/exams?limit=100")
    assert r.status_code == 500
    # No fabricated empty/blocked 200 body.
    assert "items" not in r.json() if r.headers.get("content-type", "").startswith("application/json") else True


# ── Bounded reads: no N+1 within one chunk/page ─────────────────────────────

def _seed_n(n):
    s = _Seed()
    for i in range(n):
        s.exam(f"e{i}", name=f"Exam {i}", mode="core", locked=1, vpyq=1)
    return s.db


def test_reads_do_not_scale_per_exam_within_one_chunk():
    _, small = _client(db=_seed_n(2))
    small_client = TestClient(_build_app(small))
    small_client.get("/api/admin/exam-intelligence/console/exams?limit=100")

    _, big = _client(db=_seed_n(50))
    big_client = TestClient(_build_app(big))
    big_client.get("/api/admin/exam-intelligence/console/exams?limit=100")

    # 25x the exams, same chunk/page structure → identical DB round-trips.
    assert small.table_calls == big.table_calls
