"""Behavioural integration test for migration 214 (writing_prompt content scoping).

Applies migrations 205 -> 214 to a real Postgres and proves the content-scoping
revision:

  - the dual-authority exam-scope columns (`exam_id`, `exam_cycle_id`,
    `exam_phase_id`) are DROPPED from `writing_prompts`,
  - a legacy exam-scoped prompt is BACKFILLED into a `writing_prompt_targets`
    row BEFORE the columns are dropped, and re-applying 214 does NOT duplicate
    it (idempotency — the migration is applied a second time in-test),
  - the exactly-one-scope CHECK (one of {global, family, exam, phase}) rejects a
    0-scope and a 2-scope insert, and accepts an explicit is_global row,
  - the null-safe unique identity (incl. is_global) rejects a duplicate
    `(prompt, same scope)` and a duplicate global row,
  - `ON DELETE CASCADE` from `writing_prompts` removes target rows,
  - DEFAULT-DENY (P0-1): target deletion, exam deletion (FK cascade removing the
    only exam target), and pending_review-only targets all leave the prompt with
    NO active target (unassigned, never global),
  - legacy-cycle QUARANTINE (P0-2): cycle-only / exam+cycle / exam+cycle+phase
    legacy rows become pending_review quarantine targets carrying the cycle in
    metadata, and re-apply does not duplicate them,
  - activation-gate (P0-4): 214 deactivates every active writing_prompts row,
  - RLS is enabled on `writing_prompt_targets` with NO anon/authenticated policy.

Runs in CI (the backend job provides Postgres + EWP_PG_DSN); locally set
EWP_PG_DSN to a disposable superuser DB. Skips when no DB is configured.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import urllib.parse as _urlparse
from pathlib import Path

import pytest

_DSN = os.environ.get("EWP_PG_DSN")
_PSQL = shutil.which("psql")
_MIG = Path(__file__).parents[3] / "supabase/migrations"

pytestmark = pytest.mark.skipif(
    not (_DSN and _PSQL),
    reason="set EWP_PG_DSN to a disposable Postgres superuser DB (and have psql) to run",
)

# Migration 214 DROPS columns from writing_prompts (destructive). It must NOT run
# against the shared EWP_PG_DSN database: co-tenant tests (e.g.
# test_writing_schema_integration) re-apply migration 205 to that same DB, and
# 205 recreates an index on the now-dropped `exam_id`, which would then fail.
# So this suite applies everything to an ISOLATED throwaway database, created and
# dropped in the module fixture, leaving the shared DB untouched.
#
# The name is per-xdist-worker: under `pytest -n auto --dist load` individual
# tests of this module can land on different workers, each running the module
# fixture, so a FIXED name would collide (one worker's `DROP DATABASE ... FORCE`
# terminates another worker's live connections mid-run and deadlocks). Suffixing
# with the worker id gives each worker its own DB.
_OWN_DB = "wpt_targets_it_" + re.sub(
    r"\W", "", os.environ.get("PYTEST_XDIST_WORKER", "main")
)


def _swap_dbname(dsn: str, dbname: str) -> str:
    """Return `dsn` with its database name replaced (URL or libpq key=value form)."""
    parts = _urlparse.urlsplit(dsn)
    if parts.scheme:  # postgresql://user:pass@host:port/<db>?...
        return _urlparse.urlunsplit(
            (parts.scheme, parts.netloc, "/" + dbname, parts.query, parts.fragment)
        )
    if re.search(r"\bdbname=", dsn):
        return re.sub(r"\bdbname=\S+", "dbname=" + dbname, dsn)
    return dsn.rstrip() + " dbname=" + dbname

_EXAM = "00000000-0000-0000-0000-0000000000e1"
_EXAM2 = "00000000-0000-0000-0000-0000000000e2"
_FAMILY = "00000000-0000-0000-0000-0000000000f1"
_PHASE = "00000000-0000-0000-0000-0000000000c1"
_CYCLE = "00000000-0000-0000-0000-0000000000a1"
_LEGACY_PROMPT = "00000000-0000-0000-0000-0000000000d1"
# Legacy-cycle quarantine fixtures (P0-2): each carries exam_cycle_id and must be
# backfilled as a pending_review quarantine target (NOT an evergreen active one),
# with the cycle preserved in metadata. NOTE: migration 205 declares
# writing_prompts.exam_id NOT NULL, so a literal exam-LESS "cycle-only" row
# cannot exist in the legacy schema — every legacy-cycle row necessarily carries
# an exam_id. We therefore cover the two shapes that CAN exist: exam+cycle and
# exam+cycle+phase. (The migration still keeps a defensive is_global fallback for
# the theoretical exam-less case; it is unreachable under the 205 schema.)
_LEGACY_EXAM_CYCLE = "00000000-0000-0000-0000-0000000000d3"        # exam + cycle
_LEGACY_EXAM_CYCLE_PHASE = "00000000-0000-0000-0000-0000000000d4"  # exam + cycle + phase

# Base tables migration 205/214 reference but do not create (real schema builds
# them in migration 030). exam_families is required by 214's FK.
_BOOTSTRAP = r"""
DO $$ BEGIN CREATE ROLE authenticated LOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE service_role LOGIN BYPASSRLS; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE anon LOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
GRANT USAGE ON SCHEMA public TO authenticated, service_role, anon;
CREATE SCHEMA IF NOT EXISTS auth;
CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $fn$
  SELECT NULLIF(current_setting('ewp.uid', true), '')::uuid $fn$;
CREATE TABLE IF NOT EXISTS public.profiles (id uuid PRIMARY KEY DEFAULT gen_random_uuid());
CREATE TABLE IF NOT EXISTS public.exams (id uuid PRIMARY KEY DEFAULT gen_random_uuid());
CREATE TABLE IF NOT EXISTS public.exam_families (id uuid PRIMARY KEY DEFAULT gen_random_uuid());
CREATE TABLE IF NOT EXISTS public.exam_cycles (id uuid PRIMARY KEY DEFAULT gen_random_uuid());
CREATE TABLE IF NOT EXISTS public.exam_phases (id uuid PRIMARY KEY DEFAULT gen_random_uuid());
CREATE TABLE IF NOT EXISTS public.document_assets (id uuid PRIMARY KEY DEFAULT gen_random_uuid());
CREATE TABLE IF NOT EXISTS public.study_tasks (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL, task_type text);
CREATE TABLE IF NOT EXISTS public.subjects (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), slug text NOT NULL UNIQUE, name text NOT NULL, subject_group text, default_difficulty_level text, description text, is_active boolean NOT NULL DEFAULT true, metadata jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS public.topics (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), subject_id uuid NOT NULL REFERENCES public.subjects(id) ON DELETE CASCADE, parent_topic_id uuid REFERENCES public.topics(id) ON DELETE CASCADE, slug text NOT NULL, name text NOT NULL, level text NOT NULL DEFAULT 'topic' CHECK (level IN ('topic','microtopic','concept')), default_difficulty_level text, description text, is_active boolean NOT NULL DEFAULT true, metadata jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(subject_id, parent_topic_id, slug));
"""

# Seeded AFTER migration 205 (needs subjects/topics) and BEFORE migration 214
# (needs the still-present exam_id/exam_cycle_id/exam_phase_id columns).
#
# NOTE (P0-3 fix): migration 205 ALREADY seeds the `english-language` subject and
# a top-level `grammar` topic (parent_topic_id=NULL, deterministic
# md5('ewp:topic:grammar')::uuid, which migration 274 then repairs to the live
# c4b8ebe3-... on databases that got the md5 one). Nothing here depends on
# which: the topic is resolved by slug below. This seed MUST NOT insert a
# second top-level
# `grammar` — the UNIQUE(subject_id, parent_topic_id, slug) does NOT collide on a
# NULL parent, so a duplicate insert would slip through and make
# `SELECT id FROM topics WHERE slug='grammar'` return >1 row (subquery error).
# So we reference the 205-seeded rows and keep every topic subquery deterministic
# (parent_topic_id IS NULL + ORDER BY created_at LIMIT 1).
_GRAMMAR = ("(SELECT id FROM topics WHERE slug='grammar' "
            "AND parent_topic_id IS NULL ORDER BY created_at LIMIT 1)")
_ENGLISH = "(SELECT id FROM subjects WHERE slug='english-language')"
_SEED_LEGACY = f"""
INSERT INTO exams(id) VALUES ('{_EXAM}'),('{_EXAM2}') ON CONFLICT DO NOTHING;
INSERT INTO exam_families(id) VALUES ('{_FAMILY}') ON CONFLICT DO NOTHING;
INSERT INTO exam_phases(id) VALUES ('{_PHASE}') ON CONFLICT DO NOTHING;
INSERT INTO exam_cycles(id) VALUES ('{_CYCLE}') ON CONFLICT DO NOTHING;
-- subject + grammar topic already seeded by migration 205; do NOT re-insert.
INSERT INTO writing_prompts(id,exam_id,subject_id,topic_id,exercise_type,prompt_text,difficulty_level,reviewer_status,is_active)
  SELECT '{_LEGACY_PROMPT}','{_EXAM}', {_ENGLISH}, {_GRAMMAR},
    'sentence_construction','write a sentence',1,'verified',true
  WHERE NOT EXISTS (SELECT 1 FROM writing_prompts WHERE id='{_LEGACY_PROMPT}');
-- P0-2 legacy-cycle rows (must be quarantined pending_review, cycle kept).
INSERT INTO writing_prompts(id,exam_id,exam_cycle_id,subject_id,topic_id,exercise_type,prompt_text,difficulty_level,reviewer_status,is_active)
  SELECT '{_LEGACY_EXAM_CYCLE}','{_EXAM}','{_CYCLE}', {_ENGLISH}, {_GRAMMAR},
    'sentence_construction','exam+cycle',1,'verified',true
  WHERE NOT EXISTS (SELECT 1 FROM writing_prompts WHERE id='{_LEGACY_EXAM_CYCLE}');
INSERT INTO writing_prompts(id,exam_id,exam_cycle_id,exam_phase_id,subject_id,topic_id,exercise_type,prompt_text,difficulty_level,reviewer_status,is_active)
  SELECT '{_LEGACY_EXAM_CYCLE_PHASE}','{_EXAM}','{_CYCLE}','{_PHASE}', {_ENGLISH}, {_GRAMMAR},
    'sentence_construction','exam+cycle+phase',1,'verified',true
  WHERE NOT EXISTS (SELECT 1 FROM writing_prompts WHERE id='{_LEGACY_EXAM_CYCLE_PHASE}');
"""


def _psql(sql: str) -> None:
    proc = subprocess.run([_PSQL, _DSN, "-v", "ON_ERROR_STOP=1", "-X", "-q", "-c", sql],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f"unexpected failure:\n{proc.stderr}"


def _psql_file(path: Path) -> None:
    proc = subprocess.run([_PSQL, _DSN, "-v", "ON_ERROR_STOP=1", "-X", "-q", "-f", str(path)],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f"failed applying {path.name}:\n{proc.stderr}"


def _psql_try(sql: str) -> subprocess.CompletedProcess:
    """Run `sql`; return the completed process so the caller can assert failure."""
    return subprocess.run([_PSQL, _DSN, "-v", "ON_ERROR_STOP=1", "-X", "-q", "-c", sql],
                          capture_output=True, text=True, timeout=180)


def _scalar(sql: str) -> str:
    proc = subprocess.run([_PSQL, _DSN, "-t", "-A", "-X", "-q", "-c", sql], capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    out = re.sub(r"\s*(?:INSERT|UPDATE|DELETE)\s+\d+\s+\d+\s*$", "", out)
    return out.strip()


def _new_prompt() -> str:
    """A subject-scoped prompt (no exam columns — they no longer exist)."""
    return _scalar(f"""
    INSERT INTO writing_prompts(subject_id,topic_id,exercise_type,prompt_text,difficulty_level)
    SELECT {_ENGLISH}, {_GRAMMAR},
           'sentence_construction','p',1
    RETURNING id;""")


def _admin_psql(sql: str) -> subprocess.CompletedProcess:
    """Run maintenance SQL (CREATE/DROP DATABASE) against the `postgres` db."""
    return subprocess.run(
        [_PSQL, _swap_dbname(_DSN, "postgres"), "-v", "ON_ERROR_STOP=1", "-X", "-q", "-c", sql],
        capture_output=True, text=True, timeout=180,
    )


@pytest.fixture(scope="module", autouse=True)
def _apply():
    global _DSN
    # Isolated throwaway DB so 214's destructive column-drop never touches the
    # shared EWP_PG_DSN database that co-tenant tests re-apply 205 to.
    pre = _admin_psql(f"DROP DATABASE IF EXISTS {_OWN_DB} WITH (FORCE)")
    assert pre.returncode == 0, f"pre-drop of {_OWN_DB} failed:\n{pre.stderr}"
    created = _admin_psql(f"CREATE DATABASE {_OWN_DB}")
    assert created.returncode == 0, f"create of {_OWN_DB} failed:\n{created.stderr}"

    _DSN = _swap_dbname(_DSN, _OWN_DB)  # helpers below now target the throwaway DB
    try:
        _psql(_BOOTSTRAP)
        _psql_file(_MIG / "205_english_writing_practice_schema.sql")
        _psql(_SEED_LEGACY)
        # 213 (Error Lab) then 214 — the real OPERATOR apply order; 213 is not
        # required for these assertions but keeps the sequence faithful.
        _psql_file(_MIG / "213_english_writing_practice_error_lab_read_model.sql")
        _psql_file(_MIG / "214_writing_prompt_content_scoping.sql")
        yield
    finally:
        _admin_psql(f"DROP DATABASE IF EXISTS {_OWN_DB} WITH (FORCE)")


def _columns(table: str) -> set[str]:
    raw = _scalar(f"""SELECT COALESCE(string_agg(column_name, ','), '')
      FROM information_schema.columns
      WHERE table_schema='public' AND table_name='{table}'""")
    return set(raw.split(",")) if raw else set()


def test_exam_scope_columns_are_dropped():
    cols = _columns("writing_prompts")
    assert "exam_id" not in cols
    assert "exam_cycle_id" not in cols
    assert "exam_phase_id" not in cols
    # canonical subject-scoped identity survives
    assert {"subject_id", "topic_id", "microtopic_id"} <= cols


def test_legacy_exam_prompt_was_backfilled_exam_scoped():
    n = _scalar(f"""SELECT count(*) FROM writing_prompt_targets
      WHERE prompt_id='{_LEGACY_PROMPT}' AND exam_id='{_EXAM}'
        AND exam_family_id IS NULL AND exam_phase_id IS NULL
        AND applicability_status='active' AND source_basis='legacy_backfill'""")
    assert n == "1", f"expected exactly one backfilled exam-scoped target, got {n}"


def test_reapplying_214_is_idempotent_no_duplicate_backfill():
    before = _scalar(f"SELECT count(*) FROM writing_prompt_targets WHERE prompt_id='{_LEGACY_PROMPT}'")
    _psql_file(_MIG / "214_writing_prompt_content_scoping.sql")  # second apply
    after = _scalar(f"SELECT count(*) FROM writing_prompt_targets WHERE prompt_id='{_LEGACY_PROMPT}'")
    assert before == after == "1", f"re-apply must not duplicate ({before} -> {after})"


def test_exactly_one_scope_check_rejects_zero_scopes():
    pid = _new_prompt()
    proc = _psql_try(f"INSERT INTO writing_prompt_targets(prompt_id) VALUES ('{pid}');")
    assert proc.returncode != 0, "0-scope insert must be rejected"
    assert "writing_prompt_targets_scope_exactly_one" in proc.stderr, proc.stderr


def test_exactly_one_scope_check_rejects_two_scopes():
    pid = _new_prompt()
    proc = _psql_try(
        f"INSERT INTO writing_prompt_targets(prompt_id,exam_id,exam_family_id) "
        f"VALUES ('{pid}','{_EXAM}','{_FAMILY}');")
    assert proc.returncode != 0, "2-scope insert must be rejected"
    assert "writing_prompt_targets_scope_exactly_one" in proc.stderr, proc.stderr


def test_unique_identity_rejects_duplicate_same_scope():
    pid = _new_prompt()
    _psql(f"INSERT INTO writing_prompt_targets(prompt_id,exam_id) VALUES ('{pid}','{_EXAM}');")
    proc = _psql_try(f"INSERT INTO writing_prompt_targets(prompt_id,exam_id) VALUES ('{pid}','{_EXAM}');")
    assert proc.returncode != 0, "duplicate (prompt, same scope) must be rejected"
    assert "uq_writing_prompt_targets_scope" in proc.stderr or "duplicate key" in proc.stderr.lower(), proc.stderr
    # A DIFFERENT scope for the same prompt is allowed (many-to-many).
    _psql(f"INSERT INTO writing_prompt_targets(prompt_id,exam_id) VALUES ('{pid}','{_EXAM2}');")


def test_prompt_delete_cascades_targets():
    pid = _new_prompt()
    _psql(f"INSERT INTO writing_prompt_targets(prompt_id,exam_id) VALUES ('{pid}','{_EXAM}');")
    assert _scalar(f"SELECT count(*) FROM writing_prompt_targets WHERE prompt_id='{pid}'") == "1"
    _psql(f"DELETE FROM writing_prompts WHERE id='{pid}';")
    assert _scalar(f"SELECT count(*) FROM writing_prompt_targets WHERE prompt_id='{pid}'") == "0"


def test_exam_delete_cascades_exam_scoped_targets():
    # exam_id FK is declared ON DELETE CASCADE, so removing an exam removes its
    # exam-scoped target rows (the prompt itself, being subject-scoped, remains).
    pid = _new_prompt()
    ex = _scalar("INSERT INTO exams DEFAULT VALUES RETURNING id;")
    _psql(f"INSERT INTO writing_prompt_targets(prompt_id,exam_id) VALUES ('{pid}','{ex}');")
    _psql(f"DELETE FROM exams WHERE id='{ex}';")
    assert _scalar(f"SELECT count(*) FROM writing_prompt_targets WHERE prompt_id='{pid}' AND exam_id='{ex}'") == "0"
    assert _scalar(f"SELECT count(*) FROM writing_prompts WHERE id='{pid}'") == "1"


def test_rls_enabled_with_no_client_policy():
    enabled = _scalar("""SELECT relrowsecurity FROM pg_class
      WHERE oid = 'public.writing_prompt_targets'::regclass""")
    assert enabled == "t", "RLS must be ENABLED on writing_prompt_targets"
    npol = _scalar("SELECT count(*) FROM pg_policies WHERE tablename='writing_prompt_targets'")
    assert npol == "0", f"service-role-managed table must have NO client policy, found {npol}"


# ---------------------------------------------------------------------------
# P0-1 — DEFAULT-DENY applicability + explicit is_global capability.
# ---------------------------------------------------------------------------
def _active_target_count(pid: str) -> str:
    return _scalar(
        f"SELECT count(*) FROM writing_prompt_targets "
        f"WHERE prompt_id='{pid}' AND applicability_status='active'")


def test_explicit_global_row_is_accepted():
    pid = _new_prompt()
    _psql(f"INSERT INTO writing_prompt_targets(prompt_id,is_global) VALUES ('{pid}',true);")
    assert _scalar(
        f"SELECT count(*) FROM writing_prompt_targets "
        f"WHERE prompt_id='{pid}' AND is_global=true "
        f"AND exam_id IS NULL AND exam_family_id IS NULL AND exam_phase_id IS NULL") == "1"


def test_check_rejects_global_combined_with_a_scope():
    pid = _new_prompt()
    proc = _psql_try(
        f"INSERT INTO writing_prompt_targets(prompt_id,is_global,exam_id) "
        f"VALUES ('{pid}',true,'{_EXAM}');")
    assert proc.returncode != 0, "is_global + a scope must be rejected (exactly one)"
    assert "writing_prompt_targets_scope_exactly_one" in proc.stderr, proc.stderr


def test_unique_identity_rejects_duplicate_global():
    pid = _new_prompt()
    _psql(f"INSERT INTO writing_prompt_targets(prompt_id,is_global) VALUES ('{pid}',true);")
    proc = _psql_try(f"INSERT INTO writing_prompt_targets(prompt_id,is_global) VALUES ('{pid}',true);")
    assert proc.returncode != 0, "duplicate global row must be rejected"
    assert "uq_writing_prompt_targets_scope" in proc.stderr or "duplicate key" in proc.stderr.lower(), proc.stderr


def test_default_deny_target_deletion_leaves_unassigned_not_global():
    # An active target makes the prompt applicable; deleting it must leave the
    # prompt with NO active target (unassigned) — NEVER an implicit global.
    pid = _new_prompt()
    tid = _scalar(
        f"INSERT INTO writing_prompt_targets(prompt_id,exam_id) "
        f"VALUES ('{pid}','{_EXAM}') RETURNING id;")
    assert _active_target_count(pid) == "1"
    _psql(f"DELETE FROM writing_prompt_targets WHERE id='{tid}';")
    assert _active_target_count(pid) == "0", "no active target ⇒ unassigned, not global"


def test_default_deny_exam_deletion_removes_only_active_target():
    # The FK cascade removing the sole exam target must leave the prompt
    # unassigned (default-deny), not fall back to global.
    pid = _new_prompt()
    ex = _scalar("INSERT INTO exams DEFAULT VALUES RETURNING id;")
    _psql(f"INSERT INTO writing_prompt_targets(prompt_id,exam_id) VALUES ('{pid}','{ex}');")
    assert _active_target_count(pid) == "1"
    _psql(f"DELETE FROM exams WHERE id='{ex}';")
    assert _active_target_count(pid) == "0", "exam-cascade delete ⇒ unassigned, not global"
    assert _scalar(f"SELECT count(*) FROM writing_prompts WHERE id='{pid}'") == "1"


def test_pending_review_only_confers_no_applicability():
    # A prompt whose ONLY target is pending_review has NO active target and is
    # therefore not applicable (default-deny). pending_review is inert.
    pid = _new_prompt()
    _psql(f"INSERT INTO writing_prompt_targets(prompt_id,exam_id,applicability_status) "
          f"VALUES ('{pid}','{_EXAM}','pending_review');")
    assert _active_target_count(pid) == "0", "pending_review-only ⇒ not applicable"


# ---------------------------------------------------------------------------
# P0-2 — legacy exam_cycle scope is QUARANTINED (pending_review), not dropped.
# ---------------------------------------------------------------------------
def test_legacy_exam_cycle_quarantined_pending_review_exam_scoped():
    n = _scalar(f"""SELECT count(*) FROM writing_prompt_targets
      WHERE prompt_id='{_LEGACY_EXAM_CYCLE}'
        AND applicability_status='pending_review'
        AND source_basis='legacy_cycle_quarantine'
        AND exam_id='{_EXAM}' AND exam_phase_id IS NULL AND is_global=false
        AND metadata->>'legacy_exam_cycle_id'='{_CYCLE}'
        AND metadata->>'legacy_exam_id'='{_EXAM}'""")
    assert n == "1", f"exam+cycle legacy row must be a single exam-scoped quarantine, got {n}"
    assert _active_target_count(_LEGACY_EXAM_CYCLE) == "0", "quarantine confers no applicability"


def test_legacy_exam_cycle_phase_quarantined_pending_review_phase_scoped():
    n = _scalar(f"""SELECT count(*) FROM writing_prompt_targets
      WHERE prompt_id='{_LEGACY_EXAM_CYCLE_PHASE}'
        AND applicability_status='pending_review'
        AND source_basis='legacy_cycle_quarantine'
        AND exam_phase_id='{_PHASE}' AND exam_id IS NULL AND is_global=false
        AND metadata->>'legacy_exam_cycle_id'='{_CYCLE}'
        AND metadata->>'legacy_exam_phase_id'='{_PHASE}'
        AND metadata->>'legacy_exam_id'='{_EXAM}'""")
    assert n == "1", f"exam+cycle+phase legacy row must be a single phase-scoped quarantine, got {n}"
    assert _active_target_count(_LEGACY_EXAM_CYCLE_PHASE) == "0"


def test_quarantine_is_idempotent_no_duplicate():
    before = _scalar(
        f"SELECT count(*) FROM writing_prompt_targets "
        f"WHERE source_basis='legacy_cycle_quarantine'")
    _psql_file(_MIG / "214_writing_prompt_content_scoping.sql")  # re-apply
    after = _scalar(
        f"SELECT count(*) FROM writing_prompt_targets "
        f"WHERE source_basis='legacy_cycle_quarantine'")
    assert before == after == "2", f"quarantine re-apply must not duplicate ({before} -> {after})"


# ---------------------------------------------------------------------------
# P0-4 — activation gate: 214 deactivates every active writing_prompts row.
# ---------------------------------------------------------------------------
def test_activation_gate_deactivated_seeded_active_prompts():
    # The seeded legacy prompts were is_active=true; migration 214 must have
    # flipped them to false (fail-closed until the resolver/enforcement PR).
    seeded = "','".join(
        [_LEGACY_PROMPT, _LEGACY_EXAM_CYCLE, _LEGACY_EXAM_CYCLE_PHASE])
    n = _scalar(
        f"SELECT count(*) FROM writing_prompts "
        f"WHERE id IN ('{seeded}') AND is_active=true")
    assert n == "0", f"214 must deactivate all active writing_prompts, found {n} still active"
