-- Migration 274: repair the EWP `grammar` topic id to the live taxonomy value.
--
-- Supersedes an in-place edit of migration 205 that landed in PR #1081 and is
-- reverted by this PR. That edit was wrong for a reason worth recording: 205 is
-- already applied, so editing it made the file stop describing what actually ran
-- — the exact divergence class the change existed to remove.
--
-- What is true:
--   * On the live database `grammar` predates 205. 205's topic loop inserts only
--     `WHERE NOT EXISTS (… slug = 'grammar')`, so its insert was skipped and the
--     row that stands carries c4b8ebe3-3173-4864-9e04-16ab99470c6e.
--   * On a database where nothing pre-existed — a clean `supabase db reset`, the
--     CI `ewp_it` service database, a developer's local stack, and any staging
--     database first built from the migration set — 205 DID insert `grammar`
--     with md5('ewp:topic:grammar')::uuid = 54adbabc-f126-83ae-65c2-b04704228ee0.
--
-- The second case is why the 205 edit could not stand. Editing the file changes
-- nothing on a database where 205 already ran: those rows keep the md5 id, the
-- seed's `topic_id` keeps pointing at a row that is not there, and
-- `cms_bulk_upsert_writing_prompts` keeps failing `invalid_scope` — silently,
-- because the file now claims a value that database was never given.
--
-- Migration 269's EDITED AFTER LANDING precedent does not cover it. 269 rests on
-- two properties this case does not have: "A forward migration cannot repair
-- that: on a clean database this file itself raises, so nothing after it is
-- reached" (269 was unfixable forward — this is not), and "Against production,
-- where all twelve rows are present and this migration has already run, it is a
-- no-op twice over" (269's edit provably changed nothing wherever it had already
-- run — the 205 edit does not). Editing an applied migration is defensible only
-- when the edit is a no-op on every database that applied it.
--
-- So: 205 stays as the record of what ran, and the repair is here, where it can
-- actually reach the databases that need it.
--
-- Idempotent and safe on every shape:
--   * live id already present, md5 row absent  -> no-op (production)
--   * md5 row present, live id absent          -> repaired
--   * neither present                          -> no-op (no EWP taxonomy)
--   * both present                             -> RAISE; two top-level `grammar`
--     rows is an anomaly this must not resolve by guessing. UNIQUE(subject_id,
--     parent_topic_id, slug) does not collide on a NULL parent, so the shape is
--     reachable and is called out rather than absorbed.
--
-- The id is a primary key that 51 foreign-key columns reference and none of them
-- declares ON UPDATE CASCADE, so it cannot simply be UPDATEd. The repair copies
-- the row to the live id, repoints every single-column FK that references
-- public.topics(id) — discovered from pg_constraint rather than hardcoded, so a
-- referrer added after this migration is still caught — and only then deletes the
-- old row. Repointing before the delete is what keeps `topics.parent_topic_id`'s
-- ON DELETE CASCADE from taking the eight grammar microtopics with it.

BEGIN;

DO $$
DECLARE
  v_old      uuid := md5('ewp:topic:grammar')::uuid;   -- 54adbabc-f126-83ae-65c2-b04704228ee0
  v_new      uuid := 'c4b8ebe3-3173-4864-9e04-16ab99470c6e'::uuid;
  v_subject  uuid;
  v_has_old  boolean;
  v_has_new  boolean;
  v_stale    bigint;
  r          record;
BEGIN
  -- Migration 029 creates these. Ordered runs always reach it first, but a
  -- missing-relation error here would abort the whole run — 269's failure mode —
  -- so check rather than assume.
  IF to_regclass('public.subjects') IS NULL OR to_regclass('public.topics') IS NULL THEN
    RAISE NOTICE 'ewp grammar repair: taxonomy tables absent; nothing to do';
    RETURN;
  END IF;

  SELECT id INTO v_subject FROM public.subjects WHERE slug = 'english-language';
  IF v_subject IS NULL THEN
    RAISE NOTICE 'ewp grammar repair: no english-language subject; nothing to do';
    RETURN;
  END IF;

  SELECT EXISTS (
    SELECT 1 FROM public.topics
    WHERE id = v_old AND subject_id = v_subject
      AND parent_topic_id IS NULL AND slug = 'grammar'
  ) INTO v_has_old;

  SELECT EXISTS (
    SELECT 1 FROM public.topics
    WHERE id = v_new AND subject_id = v_subject
      AND parent_topic_id IS NULL AND slug = 'grammar'
  ) INTO v_has_new;

  -- The live id belongs to something that is not the English `grammar` topic.
  IF NOT v_has_new AND EXISTS (SELECT 1 FROM public.topics WHERE id = v_new) THEN
    RAISE EXCEPTION
      'ewp grammar repair: % is already taken by a different topic', v_new;
  END IF;

  IF v_has_old AND v_has_new THEN
    RAISE EXCEPTION
      'ewp grammar repair: both % and % exist as top-level english-language '
      '"grammar" topics; resolve by hand', v_old, v_new;
  END IF;

  IF v_has_new THEN
    RAISE NOTICE 'ewp grammar repair: already on the live id; nothing to do';
    RETURN;
  END IF;

  IF NOT v_has_old THEN
    RAISE NOTICE 'ewp grammar repair: no md5-id grammar topic; nothing to do';
    RETURN;
  END IF;

  -- Copy every column, whatever `topics` looks like by now, under the live id.
  CREATE TEMP TABLE _ewp_grammar_repair ON COMMIT DROP AS
    SELECT * FROM public.topics WHERE id = v_old;
  UPDATE _ewp_grammar_repair SET id = v_new;
  INSERT INTO public.topics SELECT * FROM _ewp_grammar_repair;

  -- Repoint every single-column FK that references topics(id). A composite FK
  -- would need a different statement, so refuse rather than half-repoint.
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE contype = 'f' AND confrelid = 'public.topics'::regclass
      AND array_length(conkey, 1) > 1
  ) THEN
    RAISE EXCEPTION
      'ewp grammar repair: a composite foreign key references public.topics; '
      'repoint it by hand before re-running';
  END IF;

  FOR r IN
    SELECT c.conrelid::regclass AS tbl, a.attname AS col
    FROM pg_constraint c
    JOIN unnest(c.conkey) AS k(attnum) ON true
    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
    WHERE c.contype = 'f' AND c.confrelid = 'public.topics'::regclass
  LOOP
    EXECUTE format('UPDATE %s SET %I = $1 WHERE %I = $2', r.tbl, r.col, r.col)
      USING v_new, v_old;
  END LOOP;

  -- Nothing may still point at the old id: the DELETE below would cascade.
  v_stale := 0;
  FOR r IN
    SELECT c.conrelid::regclass AS tbl, a.attname AS col
    FROM pg_constraint c
    JOIN unnest(c.conkey) AS k(attnum) ON true
    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
    WHERE c.contype = 'f' AND c.confrelid = 'public.topics'::regclass
  LOOP
    EXECUTE format('SELECT count(*) FROM %s WHERE %I = $1', r.tbl, r.col)
      INTO v_stale USING v_old;
    IF v_stale > 0 THEN
      RAISE EXCEPTION 'ewp grammar repair: % rows still reference % via %.%',
        v_stale, v_old, r.tbl, r.col;
    END IF;
  END LOOP;

  DELETE FROM public.topics WHERE id = v_old;

  RAISE NOTICE 'ewp grammar repair: % -> %', v_old, v_new;
END $$;

COMMIT;
