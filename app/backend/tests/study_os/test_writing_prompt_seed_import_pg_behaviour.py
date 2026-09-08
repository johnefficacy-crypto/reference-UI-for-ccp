"""End-to-end import behaviour for the committed writing-prompt bank seed.

Round-trips the five real seed files
(`app/supabase/seeds/writing_prompts/01..05_*.json`) through the audited
`cms_bulk_upsert_writing_prompts` RPC (migration 215) on a real, ISOLATED
Postgres and proves the two contracts the README/checklist promise:

  - a full first import of all five batches lands **270** rows, EVERY one
    `reviewer_status='pending'` / `is_active=false` (no verified/active leak),
    with the per-batch created counts (50/50/100/50/20), and
  - a re-import of the identical files is **idempotent**: every row resolves to
    `unchanged` (0 created / 0 updated), no duplicates, still 270 pending/inactive.

The seed rows carry the taxonomy IDs the migration set produces —
deterministic `md5('ewp:topic:<slug>')` from 205 for every topic except
`grammar`, which migration 274 repairs to the live c4b8ebe3-... value. This test
is what holds the seed and the migration set in agreement: it applies migration
205 FRESH (which creates that taxonomy) → 213 → 214 → 215 → 274 against a
throwaway DB, so a seed file re-mapped without a matching migration fails here
with `invalid_scope`. Migration 214 DROPS
columns (destructive), so — like test_content_studio_ops_pg_behaviour — this
runs against an isolated database and leaves the shared EWP_PG_DSN DB untouched.

Runs in CI (backend job provides Postgres + EWP_PG_DSN); locally set EWP_PG_DSN
to a disposable superuser DB (with psql). Skips when no DB is configured.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.parse as _urlparse
from pathlib import Path

import pytest

_DSN = os.environ.get("EWP_PG_DSN")
_PSQL = shutil.which("psql")
_ROOT = Path(__file__).parents[3] / "supabase"
_MIG = _ROOT / "migrations"
_SEEDS = _ROOT / "seeds" / "writing_prompts"

pytestmark = pytest.mark.skipif(
    not (_DSN and _PSQL),
    reason="set EWP_PG_DSN to a disposable Postgres superuser DB (and have psql) to run",
)

_OWN_DB = "ewp_seed_it_" + re.sub(r"\W", "", os.environ.get("PYTEST_XDIST_WORKER", "main"))

_ACTOR = "00000000-0000-0000-0000-0000000000aa"
_REASON = "seed import smoke reason"  # >= 8 chars

# (file, exercise_type sanity, expected row count) — the README's contracted bank.
_BATCHES = [
    ("01_sentence_construction.json", 50),
    ("02_sentence_correction.json", 50),
    ("03_grammar.json", 100),
    ("04_vocabulary.json", 50),
    ("05_paragraph.json", 20),
]
_TOTAL = 270

_ENGLISH_ID = ""


def _swap_dbname(dsn: str, dbname: str) -> str:
    parts = _urlparse.urlsplit(dsn)
    if parts.scheme:
        return _urlparse.urlunsplit((parts.scheme, parts.netloc, "/" + dbname, parts.query, parts.fragment))
    if re.search(r"\bdbname=", dsn):
        return re.sub(r"\bdbname=\S+", "dbname=" + dbname, dsn)
    return dsn.rstrip() + " dbname=" + dbname


# Base tables 205/213/214/215 reference but do not create. Mirrors the bootstrap
# in test_content_studio_ops_pg_behaviour (subjects/topics carry the columns
# migration 205 populates and ewp_validate_prompt_scope inspects).
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
CREATE TABLE IF NOT EXISTS public.document_assets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scope text, document_kind text, status text,
  storage_bucket text, storage_path text);
CREATE TABLE IF NOT EXISTS public.study_tasks (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL, task_type text);
CREATE TABLE IF NOT EXISTS public.admin_audit_logs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  actor_id uuid, actor_email text, admin_user_id uuid,
  action text, entity_type text, entity_id text,
  old_value jsonb, new_value jsonb, notes text,
  created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS public.subjects (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), slug text NOT NULL UNIQUE, name text NOT NULL, subject_group text, default_difficulty_level text, description text, is_active boolean NOT NULL DEFAULT true, metadata jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS public.topics (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), subject_id uuid NOT NULL REFERENCES public.subjects(id) ON DELETE CASCADE, parent_topic_id uuid REFERENCES public.topics(id) ON DELETE CASCADE, slug text NOT NULL, name text NOT NULL, level text NOT NULL DEFAULT 'topic' CHECK (level IN ('topic','microtopic','concept')), default_difficulty_level text, description text, is_active boolean NOT NULL DEFAULT true, metadata jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(subject_id, parent_topic_id, slug));
"""

_ENGLISH = "(SELECT id FROM subjects WHERE slug='english-language')"


def _psql(sql: str) -> None:
    proc = subprocess.run([_PSQL, _DSN, "-v", "ON_ERROR_STOP=1", "-X", "-q", "-c", sql],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f"unexpected failure:\n{proc.stderr}"


def _psql_file(path: Path) -> None:
    proc = subprocess.run([_PSQL, _DSN, "-v", "ON_ERROR_STOP=1", "-X", "-q", "-f", str(path)],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f"failed applying {path.name}:\n{proc.stderr}"


def _scalar(sql: str) -> str:
    proc = subprocess.run([_PSQL, _DSN, "-t", "-A", "-X", "-q", "-c", sql], capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _admin_psql(sql: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_PSQL, _swap_dbname(_DSN, "postgres"), "-v", "ON_ERROR_STOP=1", "-X", "-q", "-c", sql],
        capture_output=True, text=True, timeout=180)


def _q(v) -> str:
    return "'" + json.dumps(v).replace("'", "''") + "'::jsonb"


def _import(rows: list) -> tuple[int, int, int]:
    """Run the seed rows through the audited bulk RPC; return (created, updated, unchanged)."""
    out = _scalar(
        f"SELECT (r->>'created')||' '||(r->>'updated')||' '||(r->>'unchanged') "
        f"FROM cms_bulk_upsert_writing_prompts('{_ENGLISH_ID}'::uuid,{_q(rows)},"
        f"'{_REASON}','{_ACTOR}'::uuid,'op@x') r;")
    c, u, n = out.split()
    return int(c), int(u), int(n)


def _load(fname: str) -> list:
    return json.loads((_SEEDS / fname).read_text())


@pytest.fixture(scope="module", autouse=True)
def _apply():
    global _DSN, _ENGLISH_ID
    pre = _admin_psql(f"DROP DATABASE IF EXISTS {_OWN_DB} WITH (FORCE)")
    assert pre.returncode == 0, f"pre-drop failed:\n{pre.stderr}"
    created = _admin_psql(f"CREATE DATABASE {_OWN_DB}")
    assert created.returncode == 0, f"create failed:\n{created.stderr}"

    _DSN = _swap_dbname(_DSN, _OWN_DB)
    try:
        _psql(_BOOTSTRAP)
        # 205 creates the English taxonomy FRESH with the deterministic md5 IDs.
        # 274 then repairs `grammar` to the live c4b8ebe3-... id the seed rows
        # carry. Both are required: without 274 this database keeps the md5 id and
        # every 03_grammar.json row fails `invalid_scope`, which is precisely the
        # production/fresh-database divergence this suite exists to catch.
        _psql_file(_MIG / "205_english_writing_practice_schema.sql")
        _psql_file(_MIG / "213_english_writing_practice_error_lab_read_model.sql")
        _psql_file(_MIG / "214_writing_prompt_content_scoping.sql")
        _psql_file(_MIG / "215_writing_prompt_content_studio_ops.sql")
        _psql_file(_MIG / "274_ewp_grammar_topic_id_repair.sql")
        _ENGLISH_ID = _scalar(f"SELECT {_ENGLISH};")
        assert re.fullmatch(r"[0-9a-f-]{36}", _ENGLISH_ID), f"english subject missing: {_ENGLISH_ID!r}"
        yield
    finally:
        _admin_psql(f"DROP DATABASE IF EXISTS {_OWN_DB} WITH (FORCE)")


def _count(where: str = "TRUE") -> int:
    return int(_scalar(
        f"SELECT count(*) FROM writing_prompts WHERE subject_id='{_ENGLISH_ID}' AND {where};"))


@pytest.fixture(scope="module")
def first_import(_apply) -> dict:
    """Perform the FIRST import of every batch exactly once and return the
    per-file (created, updated, unchanged) result. Both the first-import and the
    idempotency test depend on this, so neither relies on pytest function order:
    whichever runs first triggers the single import against the module DB."""
    return {fname: _import(_load(fname)) for fname, _ in _BATCHES}


def test_seed_files_are_committed_and_sized_as_contracted():
    # Guards the round-trip below against a silently truncated batch file.
    for fname, want in _BATCHES:
        rows = _load(fname)
        assert isinstance(rows, list) and len(rows) == want, f"{fname}: expected {want}, got {len(rows)}"
    assert sum(w for _, w in _BATCHES) == _TOTAL


def test_full_import_lands_270_pending_inactive(first_import):
    # First import of every batch: all created, per the contracted counts.
    created_total = 0
    for fname, want in _BATCHES:
        c, u, n = first_import[fname]
        assert (c, u, n) == (want, 0, 0), f"{fname}: expected {want} created, got {(c, u, n)}"
        created_total += c
    assert created_total == _TOTAL

    assert _count() == _TOTAL, "exactly 270 seed rows must exist after import"
    # No verified/active leak — every imported row is pending + inactive.
    assert _count("reviewer_status='pending' AND is_active=false") == _TOTAL
    assert _count("reviewer_status<>'pending'") == 0
    assert _count("is_active=true") == 0
    # Every row carries its bulk-import identity (subject-scoped external_key).
    assert _count("metadata->>'external_key' IS NOT NULL") == _TOTAL
    assert int(_scalar(
        f"SELECT count(DISTINCT metadata->>'external_key') FROM writing_prompts "
        f"WHERE subject_id='{_ENGLISH_ID}';")) == _TOTAL, "external_key must be unique per row"


def test_reimport_is_idempotent(first_import):
    # `first_import` guarantees the first import ran (fixture, not function order).
    # Re-running the identical files resolves every row to `unchanged`: no create,
    # no update, no duplicate — still 270 pending/inactive.
    for fname, want in _BATCHES:
        c, u, n = _import(_load(fname))
        assert (c, u, n) == (0, 0, want), f"{fname}: re-import must be all-unchanged, got {(c, u, n)}"

    assert _count() == _TOTAL, "re-import must not duplicate rows"
    assert _count("reviewer_status='pending' AND is_active=false") == _TOTAL
