-- Migration 205: English Writing Practice (EWP-1) — schema, constraints, RLS.
--
-- Lands the full practice-runtime data model locked in
-- docs/architecture/english-writing-practice.md. Additive only; no runtime
-- behaviour, API, or mastery writes (those are EWP-2/EWP-2B).
--
-- Invariants enforced here (see architecture §§3,4,8,9,10,12):
--   * user_id / reviewer FKs -> public.profiles(id) (repo convention; profiles.id = auth.users.id).
--   * Append-only history tables get DATABASE-level immutability triggers, not
--     just RLS — RLS does not constrain service_role (§12.4).
--   * Append-only history uses ON DELETE NO ACTION (never CASCADE/SET NULL):
--     a cascade into an immutable child would fire its BEFORE DELETE trigger and
--     fail. History is RETAINED; profile/exam/topic deletion is blocked once
--     writing history exists. Account deletion must go through an explicit
--     anonymisation/retention routine (future EWP work), not FK cascade.
--   * Service-role-only tables get RLS enabled with NO client allow policy.
--   * Owner-readable tables get explicit owner-select policies (§12.1).
--   * The effective-evidence view is security_invoker + service_role-only so it
--     cannot bypass the zero-policy posture of user_topic_mastery_evidence.
--   * Locked value domains are CHECK constraints, not comments.
--   * Deterministic seed UUIDs via md5('ewp:...')::uuid — re-run safe, never gen_random_uuid() (§EWP-1).
--
-- EDITED AFTER LANDING — a deliberate, documented exception to the
-- migration-immutability rule in AGENTS.md, taken for the same class of reason
-- as migration 269. Read this before assuming the rule was ignored.
--
-- One value changed: the `grammar` topic id. Everything else in this file,
-- including every other deterministic id, is byte-for-byte what it was.
--
-- The topic loop below inserts only when the slug is absent. On the live
-- database `grammar` already existed when 205 ran, so its insert was skipped and
-- the row that stands carries c4b8ebe3-3173-4864-9e04-16ab99470c6e, not the
-- baked md5 value. Production is therefore unaffected by this edit twice over:
-- the row exists, and the guard still skips it.
--
-- A clean `supabase db reset` was affected. There, nothing pre-existed, the
-- baked value was inserted, and the resulting taxonomy disagreed with the live
-- one — so the writing-prompt seed, whose JSON carries the live id, would fail
-- `cms_bulk_upsert_writing_prompts` with `invalid_scope` against a fresh
-- database while succeeding against production. This edit makes a fresh
-- database reproduce production.
--
-- A forward migration cannot do this. Repairing it later means UPDATEing a
-- topic id that child microtopics, writing_prompts and evidence rows already
-- reference; the fix belongs where the id is minted.
--
-- Parentage is resolved by slug, not by id (see the microtopic loop), so no
-- other statement in this file depends on which of the two values is used.
--
-- Migration number: highest existing migration file is 204; this is 205, which
-- the repo `migration-numbers` check validates for filesystem contiguity. The
-- authoritative live value (`select max(version)::int + 1 from schema_migrations`)
-- remains an OPERATOR gate — verify on staging before apply and rename if the
-- live DB disagrees.

-- ---------------------------------------------------------------------------
-- 0. Helpers
-- ---------------------------------------------------------------------------

-- tier_rank: explicit evidence-tier ordering. NEVER compare evidence_tier text
-- lexically ('recognition' < 'production' is lexically true but semantically
-- production outranks recognition). §4.12.
CREATE OR REPLACE FUNCTION public.ewp_tier_rank(tier text)
RETURNS int
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT CASE tier
    WHEN 'recognition' THEN 1
    WHEN 'correction'  THEN 2
    WHEN 'production'  THEN 3
    WHEN 'retention'   THEN 4
    ELSE 0
  END
$$;

-- Shared immutability guard for append-only tables (§12.4). Attached BEFORE
-- UPDATE OR DELETE. Raises even for service_role, which bypasses RLS.
CREATE OR REPLACE FUNCTION public.ewp_forbid_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'append_only_violation: % on %.% is forbidden (immutable history row)',
    TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME;
END;
$$;

-- Session snapshot guard: projection_revision, feedback_release_policy, and
-- feedback_release_delay_seconds are pinned at creation and immutable (§4.3).
-- Lifecycle fields (status, timestamps, feedback_released_at, outcome) stay mutable.
CREATE OR REPLACE FUNCTION public.ewp_guard_session_snapshot()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.projection_revision IS DISTINCT FROM OLD.projection_revision
     OR NEW.feedback_release_policy IS DISTINCT FROM OLD.feedback_release_policy
     OR NEW.feedback_release_delay_seconds IS DISTINCT FROM OLD.feedback_release_delay_seconds THEN
    RAISE EXCEPTION 'session_snapshot_immutable: projection_revision / feedback_release_policy / feedback_release_delay_seconds cannot change after creation';
  END IF;
  RETURN NEW;
END;
$$;

-- ---------------------------------------------------------------------------
-- 1. writing_rubrics
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.writing_rubrics (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL,
  version     int  NOT NULL CHECK (version > 0),
  dimensions  jsonb NOT NULL,   -- array of {key,label,weight,max_score}
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (name, version)
);

-- ---------------------------------------------------------------------------
-- 2. writing_prompts
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.writing_prompts (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  exam_id                 uuid NOT NULL REFERENCES public.exams(id),
  exam_cycle_id           uuid REFERENCES public.exam_cycles(id),
  exam_phase_id           uuid REFERENCES public.exam_phases(id),
  subject_id              uuid NOT NULL REFERENCES public.subjects(id),
  topic_id                uuid NOT NULL REFERENCES public.topics(id),
  microtopic_id           uuid REFERENCES public.topics(id),   -- level='microtopic'
  exercise_type           text NOT NULL CHECK (exercise_type IN (
    'sentence_construction','sentence_correction','vocabulary_in_context','sentence_rewrite',
    'sentence_reconstruction','paragraph_writing','summary_writing','precis_practice',
    'essay_practice','letter_practice')),   -- §4.1a
  prompt_text             text NOT NULL,
  source_text             text,
  required_words          jsonb,
  required_sentence_count int CHECK (required_sentence_count IS NULL OR required_sentence_count > 0),
  difficulty_level        int NOT NULL CHECK (difficulty_level BETWEEN 1 AND 10),
  min_words               int CHECK (min_words IS NULL OR min_words >= 0),
  max_words               int,
  max_rewrite_attempts    int NOT NULL DEFAULT 3 CHECK (max_rewrite_attempts >= 0),
  rubric_id               uuid REFERENCES public.writing_rubrics(id),
  reviewer_status         text NOT NULL DEFAULT 'pending'
    CHECK (reviewer_status IN ('pending','verified','rejected','needs_correction')),
  is_active               boolean NOT NULL DEFAULT false,
  source_document_id      uuid REFERENCES public.document_assets(id),
  metadata                jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now(),
  CHECK (max_words IS NULL OR min_words IS NULL OR max_words >= min_words)
);
CREATE INDEX IF NOT EXISTS idx_writing_prompts_exam ON public.writing_prompts(exam_id);
CREATE INDEX IF NOT EXISTS idx_writing_prompts_active
  ON public.writing_prompts(exam_id, exercise_type) WHERE reviewer_status = 'verified' AND is_active = true;

-- ---------------------------------------------------------------------------
-- 3. exam_descriptive_requirements
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.exam_descriptive_requirements (
  id                              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  exam_id                         uuid NOT NULL REFERENCES public.exams(id),
  exam_cycle_id                   uuid REFERENCES public.exam_cycles(id),
  exam_phase_id                   uuid REFERENCES public.exam_phases(id),
  stream_key                      text,
  language                        text NOT NULL DEFAULT 'english',
  exercise_type                   text NOT NULL CHECK (exercise_type IN (
    'sentence_construction','sentence_correction','vocabulary_in_context','sentence_rewrite',
    'sentence_reconstruction','paragraph_writing','summary_writing','precis_practice',
    'essay_practice','letter_practice')),   -- §4.1a
  paper_name                      text,
  marks                           numeric CHECK (marks IS NULL OR marks >= 0),
  duration_minutes                int CHECK (duration_minutes IS NULL OR duration_minutes > 0),
  minimum_words                   int CHECK (minimum_words IS NULL OR minimum_words >= 0),
  maximum_words                   int,
  required_sections               jsonb,
  format_rules                    jsonb,
  evaluation_dimensions           jsonb,
  feedback_release_policy         text NOT NULL
    CHECK (feedback_release_policy IN ('immediate','on_submit','on_evaluation_terminal','scheduled_after_submit')),
  feedback_release_delay_seconds  int,
  syllabus_document_id            uuid REFERENCES public.document_assets(id),
  notification_document_id        uuid REFERENCES public.document_assets(id),
  source_url                      text,
  source_locator                  jsonb,
  reviewer_status                 text NOT NULL DEFAULT 'pending'
    CHECK (reviewer_status IN ('pending','verified','rejected','needs_correction')),
  reviewed_by                     uuid REFERENCES public.profiles(id),
  reviewed_at                     timestamptz,
  reviewer_notes                  text,
  is_active                       boolean NOT NULL DEFAULT false,
  created_at                      timestamptz NOT NULL DEFAULT now(),
  updated_at                      timestamptz NOT NULL DEFAULT now(),
  CHECK (maximum_words IS NULL OR minimum_words IS NULL OR maximum_words >= minimum_words),
  -- NB: explicit IS NOT NULL — a bare `delay > 0` yields NULL for a NULL delay,
  -- and a CHECK only fails on FALSE, so scheduled_after_submit + NULL delay would
  -- otherwise slip through.
  CONSTRAINT exam_descriptive_requirements_feedback_delay_ck CHECK (
    (feedback_release_policy = 'scheduled_after_submit'
       AND feedback_release_delay_seconds IS NOT NULL AND feedback_release_delay_seconds > 0)
    OR
    (feedback_release_policy <> 'scheduled_after_submit' AND feedback_release_delay_seconds IS NULL)
  )
);
-- Null-safe idempotency key (§4.2).
CREATE UNIQUE INDEX IF NOT EXISTS uq_exam_descriptive_requirements
  ON public.exam_descriptive_requirements(
    exam_id,
    COALESCE(exam_cycle_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(exam_phase_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(stream_key, ''),
    language,
    exercise_type
  );

-- ---------------------------------------------------------------------------
-- 4. writing_sessions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.writing_sessions (
  id                              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id                         uuid NOT NULL REFERENCES public.profiles(id),
  study_task_id                   uuid REFERENCES public.study_tasks(id),
  prompt_id                       uuid NOT NULL REFERENCES public.writing_prompts(id),
  mode                            text NOT NULL CHECK (mode IN ('learning','exam')),
  status                          text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','evaluation_pending','rewrite_required','submitted','completed','evaluation_incomplete','abandoned')),
  projection_revision             int NOT NULL CHECK (projection_revision > 0),
  feedback_release_policy         text NOT NULL
    CHECK (feedback_release_policy IN ('immediate','on_submit','on_evaluation_terminal','scheduled_after_submit')),
  feedback_release_delay_seconds  int,
  feedback_released_at            timestamptz,
  evaluation_outcome              text
    CHECK (evaluation_outcome IS NULL OR evaluation_outcome IN ('unscored','deterministic_only','fully_evaluated')),
  started_at                      timestamptz NOT NULL DEFAULT now(),
  submitted_at                    timestamptz,
  completed_at                    timestamptz,
  -- Same null-safe policy/delay contract as the requirement snapshot it copies.
  CONSTRAINT writing_sessions_feedback_delay_ck CHECK (
    (feedback_release_policy = 'scheduled_after_submit'
       AND feedback_release_delay_seconds IS NOT NULL AND feedback_release_delay_seconds > 0)
    OR
    (feedback_release_policy <> 'scheduled_after_submit' AND feedback_release_delay_seconds IS NULL)
  )
);
CREATE INDEX IF NOT EXISTS idx_writing_sessions_user ON public.writing_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_writing_sessions_task ON public.writing_sessions(study_task_id);

DROP TRIGGER IF EXISTS ewp_session_snapshot_guard ON public.writing_sessions;
CREATE TRIGGER ewp_session_snapshot_guard
  BEFORE UPDATE ON public.writing_sessions
  FOR EACH ROW EXECUTE FUNCTION public.ewp_guard_session_snapshot();

-- ---------------------------------------------------------------------------
-- 5. writing_session_units
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.writing_session_units (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id             uuid NOT NULL REFERENCES public.writing_sessions(id),
  unit_number            int NOT NULL CHECK (unit_number > 0),
  practice_microtopic_id uuid REFERENCES public.topics(id),   -- level='microtopic'
  unit_constraints       jsonb NOT NULL DEFAULT '{}'::jsonb,
  status                 text NOT NULL DEFAULT 'not_started'
    CHECK (status IN ('not_started','draft','evaluation_pending','evaluation_failed','rewrite_required','ready','completed')),
  UNIQUE (session_id, unit_number)
);

-- ---------------------------------------------------------------------------
-- 6. writing_unit_versions (append-only)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.writing_unit_versions (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  unit_id           uuid NOT NULL REFERENCES public.writing_session_units(id),
  version_number    int NOT NULL CHECK (version_number > 0),
  answer_text       text NOT NULL,
  client_word_count int CHECK (client_word_count IS NULL OR client_word_count >= 0),
  server_word_count int CHECK (server_word_count IS NULL OR server_word_count >= 0),   -- computed at submit, in INSERT; never updated
  submission_kind   text NOT NULL DEFAULT 'user' CHECK (submission_kind IN ('user','blank')),
  content_hash      text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),   -- lowercase SHA-256 hex
  submitted_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (unit_id, version_number),
  -- A blank exam version is server-created: empty text, empty-string SHA-256,
  -- zero authoritative word count (§4.5).
  CONSTRAINT writing_unit_versions_blank_ck CHECK (
    submission_kind <> 'blank'
    OR (answer_text = ''
        AND content_hash = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
        AND server_word_count IS NOT NULL AND server_word_count = 0)
  )
);

-- ---------------------------------------------------------------------------
-- 7. writing_evaluations
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.writing_evaluations (
  id                              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  unit_version_id                 uuid NOT NULL REFERENCES public.writing_unit_versions(id),
  evaluation_revision             int NOT NULL DEFAULT 1 CHECK (evaluation_revision > 0),
  deterministic_evaluator_version text,
  language_evaluator_version      text,
  deterministic_status            text NOT NULL DEFAULT 'pending'
    CHECK (deterministic_status IN ('pending','completed','failed')),
  language_status                 text NOT NULL DEFAULT 'not_requested'
    CHECK (language_status IN ('not_requested','queued','running','completed','failed','needs_review')),
  human_review_status             text NOT NULL DEFAULT 'not_required'
    CHECK (human_review_status IN ('not_required','pending','in_review','completed')),
  overall_status                  text NOT NULL DEFAULT 'pending'
    CHECK (overall_status IN ('pending','partial','terminal_partial','completed','failed')),
  deterministic_result            jsonb,
  language_result                 jsonb,
  dimension_scores                jsonb,
  created_at                      timestamptz NOT NULL DEFAULT now(),
  updated_at                      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (unit_version_id, evaluation_revision)
);

-- ---------------------------------------------------------------------------
-- 8. writing_session_checks (append-only)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.writing_session_checks (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id       uuid NOT NULL REFERENCES public.writing_sessions(id),
  check_type       text NOT NULL,
  version_set_hash text NOT NULL CHECK (version_set_hash ~ '^[0-9a-f]{64}$'),
  passed           boolean NOT NULL,
  details          jsonb NOT NULL DEFAULT '{}'::jsonb,
  checker_version  text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_writing_session_checks_session ON public.writing_session_checks(session_id);

-- ---------------------------------------------------------------------------
-- 9. writing_issue_events (append-only). issue_type restricted to §5.1 enum.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.writing_issue_events (
  id                         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  evaluation_id              uuid NOT NULL REFERENCES public.writing_evaluations(id),
  issue_type                 text NOT NULL CHECK (issue_type IN (
    'sentence_fragment','run_on_sentence','subject_verb_agreement','tense','article',
    'preposition','pronoun_reference','modifier','spelling','punctuation','word_choice',
    'collocation','redundancy','informal_usage','cohesion','logical_order','off_topic',
    'word_limit','format_violation')),
  microtopic_id              uuid REFERENCES public.topics(id),   -- level='microtopic'
  lineage_id                 uuid NOT NULL,
  predecessor_issue_event_id uuid REFERENCES public.writing_issue_events(id),
  span_start_utf16           int CHECK (span_start_utf16 IS NULL OR span_start_utf16 >= 0),
  span_end_utf16             int CHECK (span_end_utf16 IS NULL OR span_end_utf16 >= 0),
  quoted_text                text,
  original_text              text,
  suggested_text             text,
  explanation                text,
  severity                   text NOT NULL CHECK (severity IN ('advisory','should_fix','must_fix')),
  affects_current_state      boolean NOT NULL DEFAULT true,
  created_at                 timestamptz NOT NULL DEFAULT now(),
  CHECK (span_start_utf16 IS NULL OR span_end_utf16 IS NULL OR span_end_utf16 >= span_start_utf16)
);
CREATE INDEX IF NOT EXISTS idx_writing_issue_events_eval ON public.writing_issue_events(evaluation_id);
CREATE INDEX IF NOT EXISTS idx_writing_issue_events_lineage ON public.writing_issue_events(lineage_id);

-- ---------------------------------------------------------------------------
-- 10. writing_issue_resolution_events (append-only)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.writing_issue_resolution_events (
  id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  issue_event_id           uuid NOT NULL REFERENCES public.writing_issue_events(id),
  resolving_version_id     uuid NOT NULL REFERENCES public.writing_unit_versions(id),
  resolving_evaluation_id  uuid NOT NULL REFERENCES public.writing_evaluations(id),
  successor_issue_event_id uuid REFERENCES public.writing_issue_events(id),
  outcome                  text NOT NULL CHECK (outcome IN ('resolved','persisted','regressed','uncertain')),
  evaluator_version        text NOT NULL,
  confidence               numeric CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  rationale                text,
  created_at               timestamptz NOT NULL DEFAULT now(),
  UNIQUE (issue_event_id, resolving_version_id, evaluator_version)
);

-- ---------------------------------------------------------------------------
-- 11. writing_issue_projections (append-only)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.writing_issue_projections (
  id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  issue_event_id           uuid NOT NULL REFERENCES public.writing_issue_events(id),
  projection_revision      int NOT NULL CHECK (projection_revision > 0),
  projection_kind          text NOT NULL DEFAULT 'automatic'
    CHECK (projection_kind IN ('automatic','review_override')),
  -- FK to writing_issue_review_events added by ALTER below (created after this).
  override_review_event_id uuid,
  canonical_error_type     text CHECK (canonical_error_type IS NULL OR canonical_error_type IN (
    'concept_gap','memory_gap','careless','speed_issue','misread_question',
    'option_trap','formula_confusion','time_management','unknown')),
  projection_confidence    numeric CHECK (projection_confidence IS NULL OR (projection_confidence >= 0 AND projection_confidence <= 1)),
  prior_occurrence_count   int CHECK (prior_occurrence_count IS NULL OR prior_occurrence_count >= 0),
  rationale                text,
  created_at               timestamptz NOT NULL DEFAULT now(),
  CHECK (
    (projection_kind = 'automatic' AND override_review_event_id IS NULL)
    OR
    (projection_kind = 'review_override' AND override_review_event_id IS NOT NULL)
  )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_writing_issue_projections_automatic
  ON public.writing_issue_projections(issue_event_id, projection_revision)
  WHERE projection_kind = 'automatic';
CREATE UNIQUE INDEX IF NOT EXISTS uq_writing_issue_projections_override
  ON public.writing_issue_projections(override_review_event_id)
  WHERE projection_kind = 'review_override';

-- ---------------------------------------------------------------------------
-- 12. writing_issue_review_events (append-only, service-role-only)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.writing_issue_review_events (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- Monotonic insertion ordinal. created_at (now()) is transaction-stable, and
  -- id is a random UUID, so neither alone gives a deterministic "latest" on a
  -- same-transaction tie. event_seq is the authoritative tiebreak (§4.10a).
  event_seq             bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
  issue_event_id        uuid NOT NULL REFERENCES public.writing_issue_events(id),
  decision              text NOT NULL CHECK (decision IN ('confirmed','invalidated','reclassified')),
  corrected_issue_type  text,
  reviewer_type         text NOT NULL CHECK (reviewer_type IN ('human','system')),
  reviewer_id           uuid REFERENCES public.profiles(id),
  evaluator_version     text,
  reason                text,
  created_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_writing_issue_review_events_issue ON public.writing_issue_review_events(issue_event_id, event_seq);

-- review_events integrity (§4.10): reclassified <=> corrected_issue_type set
-- (and in the issue-type enum); other decisions carry no corrected type.
ALTER TABLE public.writing_issue_review_events
  DROP CONSTRAINT IF EXISTS writing_issue_review_events_corrected_ck;
ALTER TABLE public.writing_issue_review_events
  ADD CONSTRAINT writing_issue_review_events_corrected_ck CHECK (
    (decision = 'reclassified'
       AND corrected_issue_type IS NOT NULL
       AND corrected_issue_type IN (
         'sentence_fragment','run_on_sentence','subject_verb_agreement','tense','article',
         'preposition','pronoun_reference','modifier','spelling','punctuation','word_choice',
         'collocation','redundancy','informal_usage','cohesion','logical_order','off_topic',
         'word_limit','format_violation'))
    OR
    (decision <> 'reclassified' AND corrected_issue_type IS NULL)
  );

ALTER TABLE public.writing_issue_projections
  DROP CONSTRAINT IF EXISTS writing_issue_projections_override_review_event_id_fkey;
ALTER TABLE public.writing_issue_projections
  ADD CONSTRAINT writing_issue_projections_override_review_event_id_fkey
  FOREIGN KEY (override_review_event_id)
  REFERENCES public.writing_issue_review_events(id);

-- Cross-table override integrity: a review_override projection must point at a
-- 'reclassified' review event for the SAME issue_event, and carry a non-null
-- canonical_error_type. Enforced by trigger (§4.11a).
CREATE OR REPLACE FUNCTION public.ewp_check_override_projection()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  r_decision text;
  r_issue    uuid;
BEGIN
  IF NEW.projection_kind = 'review_override' THEN
    SELECT decision, issue_event_id INTO r_decision, r_issue
      FROM public.writing_issue_review_events WHERE id = NEW.override_review_event_id;
    IF r_decision IS DISTINCT FROM 'reclassified' THEN
      RAISE EXCEPTION 'override_projection_invalid: review event % is not a reclassified decision', NEW.override_review_event_id;
    END IF;
    IF r_issue IS DISTINCT FROM NEW.issue_event_id THEN
      RAISE EXCEPTION 'override_projection_invalid: review event % targets a different issue_event', NEW.override_review_event_id;
    END IF;
    IF NEW.canonical_error_type IS NULL THEN
      RAISE EXCEPTION 'override_projection_invalid: review_override requires a non-null canonical_error_type';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS ewp_override_projection_guard ON public.writing_issue_projections;
CREATE TRIGGER ewp_override_projection_guard
  BEFORE INSERT ON public.writing_issue_projections
  FOR EACH ROW EXECUTE FUNCTION public.ewp_check_override_projection();

-- Effective review decision helper: latest by (created_at, event_seq) — NOT
-- timestamp alone (now() is transaction-stable, so same-txn events tie on
-- created_at) and NOT id (random UUID). One shared definition for the fold
-- view and owner RLS (§4.10a).
--
-- SECURITY DEFINER (repo pattern, cf. public.is_admin, migration 003): the
-- helper is called from authenticated owner RLS policies on issue/resolution/
-- projection tables, but it reads writing_issue_review_events, which is
-- RLS-on with NO authenticated policy. A SECURITY INVOKER function would run
-- as the caller, see zero review rows, and wrongly report "not invalidated" —
-- leaking withdrawn feedback (locked rule 22). As DEFINER it reads the review
-- history correctly.
--
-- It lives in a PRIVATE schema (ewp_private) that is NOT in PostgREST's
-- exposed-schema list, so it cannot be invoked as a REST RPC oracle to probe
-- another user's issue state — while RLS policies (evaluated in-database) can
-- still call it. Execution is revoked from PUBLIC/anon; only authenticated
-- (for RLS) and service_role (for the fold) may execute it, and only USAGE on
-- the private schema is granted to those roles.
CREATE SCHEMA IF NOT EXISTS ewp_private;
REVOKE ALL ON SCHEMA ewp_private FROM PUBLIC;
GRANT USAGE ON SCHEMA ewp_private TO authenticated, service_role;

DROP FUNCTION IF EXISTS public.ewp_issue_effectively_invalidated(uuid);
CREATE OR REPLACE FUNCTION ewp_private.ewp_issue_effectively_invalidated(p_issue uuid)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE((
    SELECT r.decision = 'invalidated'
    FROM public.writing_issue_review_events r
    WHERE r.issue_event_id = p_issue
    ORDER BY r.created_at DESC, r.event_seq DESC
    LIMIT 1
  ), false)
$$;
REVOKE ALL ON FUNCTION ewp_private.ewp_issue_effectively_invalidated(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ewp_private.ewp_issue_effectively_invalidated(uuid) TO authenticated, service_role;

-- ---------------------------------------------------------------------------
-- 13. user_topic_mastery_evidence (append-only, service-role-only)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.user_topic_mastery_evidence (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id                 uuid NOT NULL REFERENCES public.profiles(id),
  exam_id                 uuid REFERENCES public.exams(id),
  exam_phase_id           uuid REFERENCES public.exam_phases(id),
  topic_id                uuid NOT NULL REFERENCES public.topics(id),
  microtopic_id           uuid REFERENCES public.topics(id),   -- level='microtopic'
  source_type             text NOT NULL
    CHECK (source_type IN ('objective_mock','descriptive_mock','sentence_drill','paragraph_drill','human_review','mentor_review')),
  source_entity_id        uuid NOT NULL,
  evidence_tier           text NOT NULL CHECK (evidence_tier IN ('recognition','correction','production','retention')),
  score                   numeric CHECK (score IS NULL OR score >= 0),
  confidence              numeric CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  issue_projection_id     uuid REFERENCES public.writing_issue_projections(id),
  evidence_op             text NOT NULL DEFAULT 'assert' CHECK (evidence_op IN ('assert','retract','replace')),
  review_event_id         uuid REFERENCES public.writing_issue_review_events(id),
  supersedes_evidence_key text,
  evidence_key            text NOT NULL CHECK (evidence_key ~ '^[0-9a-f]{64}$'),   -- SHA-256 hex
  observed_at             timestamptz NOT NULL,
  metadata                jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (evidence_key),
  -- Composite target so a superseder must belong to the SAME user as its
  -- predecessor (the FK below references this).
  UNIQUE (user_id, evidence_key),
  -- Any superseding row (retract / replace / re-assert) carries a cause
  -- (review_event_id) and a predecessor (supersedes_evidence_key). Only an
  -- ORIGINAL assertion has neither (§4.12c).
  CONSTRAINT utme_op_cause_ck CHECK (
    (supersedes_evidence_key IS NULL AND evidence_op = 'assert')
    OR (supersedes_evidence_key IS NOT NULL AND review_event_id IS NOT NULL)
  ),
  -- A correction cannot supersede itself.
  CONSTRAINT utme_no_self_supersede_ck CHECK (supersedes_evidence_key IS DISTINCT FROM evidence_key)
);
CREATE INDEX IF NOT EXISTS idx_utme_user_microtopic ON public.user_topic_mastery_evidence(user_id, microtopic_id);
-- Linear supersession chain: at most one successor per predecessor (§4.12d).
CREATE UNIQUE INDEX IF NOT EXISTS uq_utme_one_successor
  ON public.user_topic_mastery_evidence(supersedes_evidence_key)
  WHERE supersedes_evidence_key IS NOT NULL;
-- Same-user self-FK: a superseder must reference a predecessor evidence_key
-- OWNED BY THE SAME USER. MATCH SIMPLE means the FK is skipped when
-- supersedes_evidence_key IS NULL (original assertions), which is correct.
ALTER TABLE public.user_topic_mastery_evidence
  DROP CONSTRAINT IF EXISTS utme_supersedes_fk;
ALTER TABLE public.user_topic_mastery_evidence
  ADD CONSTRAINT utme_supersedes_fk
  FOREIGN KEY (user_id, supersedes_evidence_key)
  REFERENCES public.user_topic_mastery_evidence(user_id, evidence_key);

-- Correction causal-chain integrity (§4.12c). A retract/replace must:
--   * cite a review event for the SAME issue as the predecessor's projection;
--   * supersede the currently EFFECTIVE tail (a row with no successor);
--   * (replace) carry a projection on the same issue.
-- Enforced by trigger because it spans evidence -> projection -> issue_event
-- and the review event.
CREATE OR REPLACE FUNCTION public.ewp_check_evidence_correction()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  pred_issue      uuid;
  pred_proj       uuid;
  root_proj       uuid;
  review_issue    uuid;
  review_decision text;
  cited_created   timestamptz;
  cited_seq       bigint;
  prev_decision   text;
  new_issue       uuid;
  new_kind        text;
  new_override    uuid;
  has_successor   boolean;
BEGIN
  -- Validate EVERY superseding row: retract, replace, AND re-assert
  -- (invalidated->confirmed / reclassified->confirmed emit correction-style
  -- 'assert' rows with a predecessor). §4.12c.
  IF NEW.supersedes_evidence_key IS NOT NULL THEN
    -- must supersede the effective tail (a row with no successor)
    SELECT EXISTS (
      SELECT 1 FROM public.user_topic_mastery_evidence s
      WHERE s.supersedes_evidence_key = NEW.supersedes_evidence_key
    ) INTO has_successor;
    IF has_successor THEN
      RAISE EXCEPTION 'evidence_correction_invalid: predecessor % already superseded (not the effective tail)', NEW.supersedes_evidence_key;
    END IF;

    -- predecessor MUST resolve to an issue via its projection; capture the
    -- predecessor's EXACT projection (retract must preserve it).
    SELECT p.issue_projection_id, ie.id INTO pred_proj, pred_issue
      FROM public.user_topic_mastery_evidence p
      JOIN public.writing_issue_projections pr ON pr.id = p.issue_projection_id
      JOIN public.writing_issue_events ie ON ie.id = pr.issue_event_id
      WHERE p.evidence_key = NEW.supersedes_evidence_key;
    IF pred_issue IS NULL THEN
      RAISE EXCEPTION 'evidence_correction_invalid: predecessor % has no issue projection to correct', NEW.supersedes_evidence_key;
    END IF;

    -- root of the supersession chain (the original assert) — re-assert must
    -- restore its EXACT automatic projection, not just any automatic one.
    WITH RECURSIVE chain AS (
      SELECT e.evidence_key, e.supersedes_evidence_key, e.issue_projection_id
        FROM public.user_topic_mastery_evidence e
        WHERE e.evidence_key = NEW.supersedes_evidence_key
      UNION ALL
      SELECT e.evidence_key, e.supersedes_evidence_key, e.issue_projection_id
        FROM public.user_topic_mastery_evidence e
        JOIN chain c ON e.evidence_key = c.supersedes_evidence_key
    )
    SELECT issue_projection_id INTO root_proj FROM chain WHERE supersedes_evidence_key IS NULL;

    -- citing review event: target issue, decision, and ordering position
    SELECT issue_event_id, decision, created_at, event_seq
      INTO review_issue, review_decision, cited_created, cited_seq
      FROM public.writing_issue_review_events WHERE id = NEW.review_event_id;
    IF review_issue IS DISTINCT FROM pred_issue THEN
      RAISE EXCEPTION 'evidence_correction_invalid: review event % targets a different issue than the predecessor', NEW.review_event_id;
    END IF;

    -- the cited review must be the LATEST for the issue (no stale corrections)
    IF EXISTS (
      SELECT 1 FROM public.writing_issue_review_events r2
      WHERE r2.issue_event_id = pred_issue
        AND (r2.created_at, r2.event_seq) > (cited_created, cited_seq)
    ) THEN
      RAISE EXCEPTION 'evidence_correction_invalid: review event % is not the latest for the issue (stale)', NEW.review_event_id;
    END IF;

    -- the decision must actually CHANGE the effective decision; an unchanged
    -- transition (confirmed->confirmed, invalidated->invalidated, ...) emits
    -- nothing (§4.10a). Previous effective = latest event strictly before the
    -- cited one, defaulting to 'confirmed' (active) when there is none.
    SELECT decision INTO prev_decision
      FROM public.writing_issue_review_events r2
      WHERE r2.issue_event_id = pred_issue
        AND (r2.created_at, r2.event_seq) < (cited_created, cited_seq)
      ORDER BY r2.created_at DESC, r2.event_seq DESC
      LIMIT 1;
    prev_decision := COALESCE(prev_decision, 'confirmed');
    IF review_decision = prev_decision THEN
      RAISE EXCEPTION 'evidence_correction_invalid: review decision % is unchanged from the previous effective decision (no correction)', review_decision;
    END IF;

    -- Locked review-decision -> evidence-op mapping (§4.12c):
    --   confirmed -> assert (re-assert), invalidated -> retract, reclassified -> replace.
    IF (review_decision = 'confirmed'    AND NEW.evidence_op <> 'assert')
       OR (review_decision = 'invalidated'  AND NEW.evidence_op <> 'retract')
       OR (review_decision = 'reclassified' AND NEW.evidence_op <> 'replace') THEN
      RAISE EXCEPTION 'evidence_correction_invalid: evidence_op % does not match review decision %', NEW.evidence_op, review_decision;
    END IF;

    -- EVERY superseding row must carry a projection on the predecessor's issue.
    IF NEW.issue_projection_id IS NULL THEN
      RAISE EXCEPTION 'evidence_correction_invalid: a superseding row must carry a projection on the predecessor issue';
    END IF;
    SELECT pr.issue_event_id, pr.projection_kind, pr.override_review_event_id
      INTO new_issue, new_kind, new_override
      FROM public.writing_issue_projections pr WHERE pr.id = NEW.issue_projection_id;
    IF new_issue IS DISTINCT FROM pred_issue THEN
      RAISE EXCEPTION 'evidence_correction_invalid: projection is on a different issue than the predecessor';
    END IF;

    -- EXACT op-specific projection identity:
    --   replace  -> the review-override projection created by the cited event;
    --   re-assert-> the EXACT original automatic projection at the chain root;
    --   retract  -> the predecessor's EXACT projection (preserve).
    IF NEW.evidence_op = 'replace' THEN
      IF new_kind IS DISTINCT FROM 'review_override' OR new_override IS DISTINCT FROM NEW.review_event_id THEN
        RAISE EXCEPTION 'evidence_correction_invalid: replace must carry the review_override projection created by the cited review event';
      END IF;
    ELSIF NEW.evidence_op = 'assert' THEN
      IF new_kind IS DISTINCT FROM 'automatic' OR NEW.issue_projection_id IS DISTINCT FROM root_proj THEN
        RAISE EXCEPTION 'evidence_correction_invalid: re-assert must restore the exact original automatic projection at the chain root';
      END IF;
    ELSIF NEW.evidence_op = 'retract' THEN
      IF NEW.issue_projection_id IS DISTINCT FROM pred_proj THEN
        RAISE EXCEPTION 'evidence_correction_invalid: retract must preserve the predecessor''s exact projection';
      END IF;
    END IF;
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS ewp_evidence_correction_guard ON public.user_topic_mastery_evidence;
CREATE TRIGGER ewp_evidence_correction_guard
  BEFORE INSERT ON public.user_topic_mastery_evidence
  FOR EACH ROW EXECUTE FUNCTION public.ewp_check_evidence_correction();

-- ---------------------------------------------------------------------------
-- 14. writing_evaluation_jobs (mutable queue)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.writing_evaluation_jobs (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  evaluation_id  uuid NOT NULL REFERENCES public.writing_evaluations(id),
  job_kind       text NOT NULL CHECK (job_kind IN ('language_evaluation','rubric_evaluation')),
  generation     int NOT NULL DEFAULT 1 CHECK (generation > 0),
  status         text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','done','failed')),
  attempts       int NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts   int NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
  scheduled_for  timestamptz,
  locked_at      timestamptz,
  claim_token    uuid,   -- lease/fencing (§8.3)
  last_error     text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (evaluation_id, job_kind, generation),
  CHECK (attempts <= max_attempts),
  -- A claimed (running) job must hold a lease + fencing token (§8.3).
  CONSTRAINT writing_evaluation_jobs_running_lease_ck CHECK (
    status <> 'running' OR (locked_at IS NOT NULL AND claim_token IS NOT NULL)
  )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_writing_evaluation_jobs_active
  ON public.writing_evaluation_jobs(evaluation_id, job_kind)
  WHERE status IN ('pending','running');

-- ---------------------------------------------------------------------------
-- 15. writing_mastery_shadow (append-only, service-role-only)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.writing_mastery_shadow (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             uuid NOT NULL REFERENCES public.profiles(id),
  exam_id             uuid REFERENCES public.exams(id),
  topic_id            uuid NOT NULL REFERENCES public.topics(id),
  microtopic_id       uuid REFERENCES public.topics(id),
  source_type         text NOT NULL
    CHECK (source_type IN ('objective_mock','descriptive_mock','sentence_drill','paragraph_drill','human_review','mentor_review')),
  source_entity_id    uuid NOT NULL,
  evaluation_id       uuid NOT NULL REFERENCES public.writing_evaluations(id),
  issue_projection_id uuid REFERENCES public.writing_issue_projections(id),
  evidence_tier       text NOT NULL CHECK (evidence_tier IN ('recognition','correction','production','retention')),
  score               numeric CHECK (score IS NULL OR score >= 0),
  confidence          numeric CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  delta_json          jsonb NOT NULL DEFAULT '{}'::jsonb,
  evidence_key        text NOT NULL CHECK (evidence_key ~ '^[0-9a-f]{64}$'),
  processed_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (evidence_key)
);

-- ---------------------------------------------------------------------------
-- 16. writing_mastery_outbox (mutable; drives post-commit mastery writes)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.writing_mastery_outbox (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_kind         text NOT NULL CHECK (source_kind IN ('evaluation','review_correction')),
  evaluation_id       uuid REFERENCES public.writing_evaluations(id),
  review_event_id     uuid REFERENCES public.writing_issue_review_events(id),
  evidence_op         text NOT NULL DEFAULT 'assert' CHECK (evidence_op IN ('assert','retract','replace')),
  user_id             uuid NOT NULL REFERENCES public.profiles(id),
  mastery_flag_state  text NOT NULL CHECK (mastery_flag_state IN ('shadow','live')),
  idempotency_key     text NOT NULL CHECK (idempotency_key ~ '^[0-9a-f]{64}$'),   -- SHA-256 hex
  status              text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','done','failed')),
  attempts            int NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts        int NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
  locked_at           timestamptz,
  last_error          text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  processed_at        timestamptz,
  CHECK (attempts <= max_attempts),
  CHECK (
    (source_kind = 'evaluation' AND evaluation_id IS NOT NULL AND review_event_id IS NULL)
    OR
    (source_kind = 'review_correction' AND review_event_id IS NOT NULL)
  ),
  -- A claimed (processing) outbox row must hold a lease (§8.3).
  CONSTRAINT writing_mastery_outbox_processing_lease_ck CHECK (
    status <> 'processing' OR locked_at IS NOT NULL
  ),
  UNIQUE (idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_writing_mastery_outbox_claim
  ON public.writing_mastery_outbox(status, locked_at) WHERE status IN ('pending','processing');

-- ---------------------------------------------------------------------------
-- 17. writing_issue_type_microtopic_map (backend-owned taxonomy resolution)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.writing_issue_type_microtopic_map (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  issue_type    text NOT NULL CHECK (issue_type IN (
    'sentence_fragment','run_on_sentence','subject_verb_agreement','tense','article',
    'preposition','pronoun_reference','modifier','spelling','punctuation','word_choice',
    'collocation','redundancy','informal_usage','cohesion','logical_order','off_topic',
    'word_limit','format_violation')),
  microtopic_id uuid NOT NULL REFERENCES public.topics(id),   -- English, level='microtopic'
  map_version   int NOT NULL DEFAULT 1 CHECK (map_version > 0),
  is_active     boolean NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (issue_type, map_version)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_issue_type_microtopic_active
  ON public.writing_issue_type_microtopic_map(issue_type) WHERE is_active = true;

-- ---------------------------------------------------------------------------
-- study_tasks: typed launch targets (never stored URLs) (§11.1)
-- ---------------------------------------------------------------------------
ALTER TABLE public.study_tasks
  ADD COLUMN IF NOT EXISTS launch_type      text,
  ADD COLUMN IF NOT EXISTS launch_entity_id uuid,
  ADD COLUMN IF NOT EXISTS launch_context   jsonb;

-- ---------------------------------------------------------------------------
-- Immutability triggers on append-only tables (§12.4)
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'writing_unit_versions',
    'writing_issue_events',
    'writing_issue_resolution_events',
    'writing_issue_projections',
    'writing_issue_review_events',
    'user_topic_mastery_evidence',
    'writing_mastery_shadow'
  ] LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS %I ON public.%I', 'ewp_immutable_' || t, t);
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON public.%I '
      || 'FOR EACH ROW EXECUTE FUNCTION public.ewp_forbid_mutation()',
      'ewp_immutable_' || t, t
    );
  END LOOP;
END;
$$;

-- ---------------------------------------------------------------------------
-- effective_user_topic_mastery_evidence: the ONLY planner/level source (§4.12d)
-- ---------------------------------------------------------------------------
-- security_invoker=true so it honours the underlying table's RLS (which has NO
-- client policy) instead of the view owner's privileges — a normal view would
-- leak every user's evidence to any grantee. Granted to service_role only.
--
-- Fold: keep assert/replace rows that have no same-user successor, are not
-- attached to a stale issue (affects_current_state=false), and whose issue is
-- not effectively invalidated (latest review decision). Retracted/replaced
-- assertions and stale/withdrawn evidence are excluded.
CREATE OR REPLACE VIEW public.effective_user_topic_mastery_evidence
WITH (security_invoker = true) AS
  SELECT e.*
  FROM public.user_topic_mastery_evidence e
  LEFT JOIN public.writing_issue_projections p ON p.id = e.issue_projection_id
  LEFT JOIN public.writing_issue_events ie ON ie.id = p.issue_event_id
  WHERE e.evidence_op IN ('assert','replace')
    AND NOT EXISTS (
      SELECT 1 FROM public.user_topic_mastery_evidence s
      WHERE s.supersedes_evidence_key = e.evidence_key
        AND s.user_id = e.user_id
    )
    AND (ie.id IS NULL OR ie.affects_current_state = true)
    AND (ie.id IS NULL OR NOT ewp_private.ewp_issue_effectively_invalidated(ie.id));

REVOKE ALL ON public.effective_user_topic_mastery_evidence FROM PUBLIC;
REVOKE ALL ON public.effective_user_topic_mastery_evidence FROM authenticated;
GRANT SELECT ON public.effective_user_topic_mastery_evidence TO service_role;

-- ---------------------------------------------------------------------------
-- RLS (§12)
-- ---------------------------------------------------------------------------
ALTER TABLE public.writing_sessions       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.writing_session_units  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.writing_unit_versions  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.writing_session_checks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.writing_evaluations    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.writing_issue_events   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.writing_issue_resolution_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.writing_issue_projections       ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.writing_prompts                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.exam_descriptive_requirements   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.writing_rubrics                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.writing_issue_type_microtopic_map ENABLE ROW LEVEL SECURITY;

-- Service-role-only (RLS on, NO client allow policy — deliberate, §12.2).
ALTER TABLE public.writing_issue_review_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_topic_mastery_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.writing_evaluation_jobs     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.writing_mastery_shadow      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.writing_mastery_outbox      ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS writing_sessions_owner_select ON public.writing_sessions;
CREATE POLICY writing_sessions_owner_select ON public.writing_sessions
  FOR SELECT USING (user_id = auth.uid());

DROP POLICY IF EXISTS writing_session_units_owner_select ON public.writing_session_units;
CREATE POLICY writing_session_units_owner_select ON public.writing_session_units
  FOR SELECT USING (EXISTS (
    SELECT 1 FROM public.writing_sessions s
    WHERE s.id = writing_session_units.session_id AND s.user_id = auth.uid()
  ));

DROP POLICY IF EXISTS writing_unit_versions_owner_select ON public.writing_unit_versions;
CREATE POLICY writing_unit_versions_owner_select ON public.writing_unit_versions
  FOR SELECT USING (EXISTS (
    SELECT 1 FROM public.writing_session_units u
    JOIN public.writing_sessions s ON s.id = u.session_id
    WHERE u.id = writing_unit_versions.unit_id AND s.user_id = auth.uid()
  ));

DROP POLICY IF EXISTS writing_session_checks_owner_select ON public.writing_session_checks;
CREATE POLICY writing_session_checks_owner_select ON public.writing_session_checks
  FOR SELECT USING (EXISTS (
    SELECT 1 FROM public.writing_sessions s
    WHERE s.id = writing_session_checks.session_id AND s.user_id = auth.uid()
  ));

DROP POLICY IF EXISTS writing_evaluations_owner_select ON public.writing_evaluations;
CREATE POLICY writing_evaluations_owner_select ON public.writing_evaluations
  FOR SELECT USING (EXISTS (
    SELECT 1
    FROM public.writing_unit_versions v
    JOIN public.writing_session_units u ON u.id = v.unit_id
    JOIN public.writing_sessions s ON s.id = u.session_id
    WHERE v.id = writing_evaluations.unit_version_id
      AND s.user_id = auth.uid()
      AND (s.mode = 'learning'
           OR (s.feedback_released_at IS NOT NULL AND s.feedback_released_at <= now()))
  ));

DROP POLICY IF EXISTS writing_issue_events_owner_select ON public.writing_issue_events;
CREATE POLICY writing_issue_events_owner_select ON public.writing_issue_events
  FOR SELECT USING (EXISTS (
    SELECT 1
    FROM public.writing_evaluations e
    JOIN public.writing_unit_versions v ON v.id = e.unit_version_id
    JOIN public.writing_session_units u ON u.id = v.unit_id
    JOIN public.writing_sessions s ON s.id = u.session_id
    WHERE e.id = writing_issue_events.evaluation_id
      AND s.user_id = auth.uid()
      AND (s.mode = 'learning'
           OR (s.feedback_released_at IS NOT NULL AND s.feedback_released_at <= now()))
  ) AND NOT ewp_private.ewp_issue_effectively_invalidated(writing_issue_events.id));

DROP POLICY IF EXISTS writing_issue_resolution_events_owner_select ON public.writing_issue_resolution_events;
CREATE POLICY writing_issue_resolution_events_owner_select ON public.writing_issue_resolution_events
  FOR SELECT USING (EXISTS (
    SELECT 1
    FROM public.writing_issue_events ie
    JOIN public.writing_evaluations e ON e.id = ie.evaluation_id
    JOIN public.writing_unit_versions v ON v.id = e.unit_version_id
    JOIN public.writing_session_units u ON u.id = v.unit_id
    JOIN public.writing_sessions s ON s.id = u.session_id
    WHERE ie.id = writing_issue_resolution_events.issue_event_id
      AND s.user_id = auth.uid()
      AND (s.mode = 'learning'
           OR (s.feedback_released_at IS NOT NULL AND s.feedback_released_at <= now()))
  ) AND NOT ewp_private.ewp_issue_effectively_invalidated(writing_issue_resolution_events.issue_event_id));

DROP POLICY IF EXISTS writing_issue_projections_owner_select ON public.writing_issue_projections;
CREATE POLICY writing_issue_projections_owner_select ON public.writing_issue_projections
  FOR SELECT USING (EXISTS (
    SELECT 1
    FROM public.writing_issue_events ie
    JOIN public.writing_evaluations e ON e.id = ie.evaluation_id
    JOIN public.writing_unit_versions v ON v.id = e.unit_version_id
    JOIN public.writing_session_units u ON u.id = v.unit_id
    JOIN public.writing_sessions s ON s.id = u.session_id
    WHERE ie.id = writing_issue_projections.issue_event_id
      AND s.user_id = auth.uid()
      AND (s.mode = 'learning'
           OR (s.feedback_released_at IS NOT NULL AND s.feedback_released_at <= now()))
  ) AND NOT ewp_private.ewp_issue_effectively_invalidated(writing_issue_projections.issue_event_id));

DROP POLICY IF EXISTS writing_prompts_public_read ON public.writing_prompts;
CREATE POLICY writing_prompts_public_read ON public.writing_prompts
  FOR SELECT USING (reviewer_status = 'verified' AND is_active = true);

DROP POLICY IF EXISTS exam_descriptive_requirements_public_read ON public.exam_descriptive_requirements;
CREATE POLICY exam_descriptive_requirements_public_read ON public.exam_descriptive_requirements
  FOR SELECT USING (reviewer_status = 'verified' AND is_active = true);

DROP POLICY IF EXISTS writing_rubrics_read ON public.writing_rubrics;
CREATE POLICY writing_rubrics_read ON public.writing_rubrics
  FOR SELECT USING (true);

DROP POLICY IF EXISTS issue_type_microtopic_map_read ON public.writing_issue_type_microtopic_map;
CREATE POLICY issue_type_microtopic_map_read ON public.writing_issue_type_microtopic_map
  FOR SELECT USING (is_active = true);

-- Service-role-only tables: no client allow policy is created, by design.

-- ---------------------------------------------------------------------------
-- Seed: full §3 English Language taxonomy + issue_type -> microtopic map.
-- Deterministic md5('ewp:...')::uuid ids; re-run safe. Mapping is validated
-- (English subject + level='microtopic' + active) and fails loudly otherwise.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  v_subject uuid := md5('ewp:subject:english-language')::uuid;
  v_parent  uuid;
  v_micro   uuid;
  i int;
  -- level-1 topics (§3). The last four are direct-leaf topics (no microtopics yet).
  topics2   text[][] := ARRAY[
    ARRAY['sentence-construction','Sentence Construction'],
    ARRAY['grammar','Grammar'],
    ARRAY['vocabulary-in-context','Vocabulary in Context'],
    ARRAY['paragraph-writing','Paragraph Writing'],
    ARRAY['precis-writing','Précis Writing'],
    ARRAY['essay-writing','Essay Writing'],
    ARRAY['letter-report-writing','Letter and Report Writing'],
    ARRAY['comprehension-summary','Comprehension and Summary']
  ];
  -- microtopics: parent_slug, slug, name. Includes all §3 leaves plus a few
  -- error-only leaves (sentence-structure, spelling, content-relevance,
  -- word-limit, format-rules) that the §5.1 error taxonomy needs but §3's
  -- learning hierarchy does not enumerate as skills.
  micros    text[][] := ARRAY[
    ARRAY['sentence-construction','simple-sentences','Simple Sentences'],
    ARRAY['sentence-construction','compound-sentences','Compound Sentences'],
    ARRAY['sentence-construction','complex-sentences','Complex Sentences'],
    ARRAY['sentence-construction','sentence-transformation','Sentence Transformation'],
    ARRAY['sentence-construction','sentence-structure','Sentence Structure'],
    ARRAY['grammar','subject-verb-agreement','Subject-Verb Agreement'],
    ARRAY['grammar','tense','Tense'],
    ARRAY['grammar','articles','Articles'],
    ARRAY['grammar','prepositions','Prepositions'],
    ARRAY['grammar','pronoun-reference','Pronoun Reference'],
    ARRAY['grammar','modifiers','Modifiers'],
    ARRAY['grammar','punctuation','Punctuation'],
    ARRAY['grammar','spelling','Spelling'],
    ARRAY['vocabulary-in-context','word-choice','Word Choice'],
    ARRAY['vocabulary-in-context','collocations','Collocations'],
    ARRAY['vocabulary-in-context','formal-vocabulary','Formal Vocabulary'],
    ARRAY['vocabulary-in-context','redundancy','Redundancy'],
    ARRAY['paragraph-writing','topic-sentence','Topic Sentence'],
    ARRAY['paragraph-writing','cohesion','Cohesion'],
    ARRAY['paragraph-writing','logical-order','Logical Order'],
    ARRAY['paragraph-writing','conclusion','Conclusion'],
    ARRAY['paragraph-writing','content-relevance','Content Relevance'],
    ARRAY['paragraph-writing','word-limit','Word Limit'],
    ARRAY['paragraph-writing','format-rules','Format Rules'],
    -- The four descriptive leaves get a microtopic child each so they are
    -- usable as canonical prompt/evidence microtopics (level='microtopic').
    ARRAY['precis-writing','precis-writing-general','Précis Writing'],
    ARRAY['essay-writing','essay-writing-general','Essay Writing'],
    ARRAY['letter-report-writing','letter-report-writing-general','Letter and Report Writing'],
    ARRAY['comprehension-summary','comprehension-summary-general','Comprehension and Summary']
  ];
  -- issue_type -> microtopic slug
  maps      text[][] := ARRAY[
    ARRAY['sentence_fragment','sentence-structure'],
    ARRAY['run_on_sentence','sentence-structure'],
    ARRAY['subject_verb_agreement','subject-verb-agreement'],
    ARRAY['tense','tense'],
    ARRAY['article','articles'],
    ARRAY['preposition','prepositions'],
    ARRAY['pronoun_reference','pronoun-reference'],
    ARRAY['modifier','modifiers'],
    ARRAY['spelling','spelling'],
    ARRAY['punctuation','punctuation'],
    ARRAY['word_choice','word-choice'],
    ARRAY['collocation','collocations'],
    ARRAY['redundancy','redundancy'],
    ARRAY['informal_usage','formal-vocabulary'],
    ARRAY['cohesion','cohesion'],
    ARRAY['logical_order','logical-order'],
    ARRAY['off_topic','content-relevance'],
    ARRAY['word_limit','word-limit'],
    ARRAY['format_violation','format-rules']
  ];
BEGIN
  INSERT INTO public.subjects (id, slug, name, subject_group, description, is_active)
  VALUES (v_subject, 'english-language', 'English Language', 'language',
          'English writing practice taxonomy (EWP).', true)
  ON CONFLICT (slug) DO NOTHING;
  SELECT id INTO v_subject FROM public.subjects WHERE slug = 'english-language';

  FOR i IN 1 .. array_length(topics2, 1) LOOP
    INSERT INTO public.topics (id, subject_id, parent_topic_id, slug, name, level, is_active)
    SELECT CASE topics2[i][1]
             -- See the EDITED AFTER LANDING note in the header.
             WHEN 'grammar' THEN 'c4b8ebe3-3173-4864-9e04-16ab99470c6e'::uuid
             ELSE md5('ewp:topic:' || topics2[i][1])::uuid
           END, v_subject, NULL,
           topics2[i][1], topics2[i][2], 'topic', true
    WHERE NOT EXISTS (
      SELECT 1 FROM public.topics
      WHERE subject_id = v_subject AND parent_topic_id IS NULL AND slug = topics2[i][1]
    );
  END LOOP;

  FOR i IN 1 .. array_length(micros, 1) LOOP
    SELECT id INTO v_parent FROM public.topics
      WHERE subject_id = v_subject AND parent_topic_id IS NULL AND slug = micros[i][1];
    IF v_parent IS NULL THEN
      RAISE EXCEPTION 'ewp seed: missing parent topic % for microtopic %', micros[i][1], micros[i][2];
    END IF;

    INSERT INTO public.topics (id, subject_id, parent_topic_id, slug, name, level, is_active)
    SELECT md5('ewp:microtopic:' || micros[i][2])::uuid, v_subject, v_parent,
           micros[i][2], micros[i][3], 'microtopic', true
    WHERE NOT EXISTS (
      SELECT 1 FROM public.topics
      WHERE subject_id = v_subject AND parent_topic_id = v_parent AND slug = micros[i][2]
    );
  END LOOP;

  FOR i IN 1 .. array_length(maps, 1) LOOP
    -- Validate: English subject + level='microtopic' + active before mapping.
    SELECT t.id INTO v_micro
      FROM public.topics t
      WHERE t.subject_id = v_subject AND t.slug = maps[i][2]
        AND t.level = 'microtopic' AND t.is_active = true;
    IF v_micro IS NULL THEN
      RAISE EXCEPTION 'ewp seed: no active English microtopic for slug % (issue_type %)',
        maps[i][2], maps[i][1];
    END IF;

    INSERT INTO public.writing_issue_type_microtopic_map (issue_type, microtopic_id, map_version, is_active)
    SELECT maps[i][1], v_micro, 1, true
    WHERE NOT EXISTS (
      SELECT 1 FROM public.writing_issue_type_microtopic_map
      WHERE issue_type = maps[i][1] AND map_version = 1
    );
  END LOOP;
END;
$$;

SELECT pg_notify('pgrst', 'reload schema');
