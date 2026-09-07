-- Migration 273: General Knowledge microtopics from the RBI Grade B GA corpus.
--
-- Adds 55 microtopics under the six existing sections of the body-agnostic
-- `general-knowledge` subject. Creates no subject and no section — both already
-- exist; this migration only hangs leaves off them.
--
-- Derived from the corpus, not from a syllabus. The source is the 90 rows in
-- workbench/rbi-ga-classification.csv classified DURABLE with a blank
-- suggested_subject: General Awareness questions whose answer stays true over
-- years but sits outside the six finance subjects. Every microtopic below has
-- at least one question behind it, named in the trailing comment as
-- `<year> Q<number>` against review_out_rbi<year>/questions_export.json.
-- workbench/rbi-gk-microtopic-map.csv carries the full question_id -> microtopic
-- mapping. Nothing here tags a question; that is a separate, reviewed step.
--
--   History                          5 microtopics    6 questions
--   Geography                       12               20
--   Indian Polity and Constitution  10               11
--   Indian Economy                   8               12
--   General Science                  7               15
--   Miscellaneous                   13               26
--
-- Miscellaneous carries 26 questions because international relations, defence
-- and static trivia have no section of their own, not because it absorbs
-- leftovers. Each of its microtopics is a coherent group with corpus evidence.
--
-- metadata carries no `exams` key: general-knowledge is body-agnostic, unlike
-- the exam-scoped subjects seeded in migration 269. No `study_sources` are
-- written either — inventing references for corpus-derived microtopics would
-- put unverified claims in the taxonomy.
--
-- subject_id is read from the parent section row rather than hardcoded, so this
-- file cannot disagree with the live subject.
--
-- The join carries no `level` predicate. `topics.level` is constrained to
-- ('topic','microtopic','concept') by migration 029 — there is no 'section'
-- value — so the six rows called sections here are stored under one of those,
-- and asserting a level this file cannot verify would silently insert nothing.
-- Existence of the parent row is what the guard needs and all it checks.
--
-- GUARDED, for the reason documented at length in migration 269: the six
-- section rows were created outside the migration set and exist only in the
-- production database. Hardcoding a parent id that no migration creates is what
-- made 269 abort every clean `supabase db reset` and take every later migration
-- down with it. The WHERE clause below yields zero rows on a database without
-- those sections instead of raising `topics_parent_topic_id_fkey`.

BEGIN;

INSERT INTO public.topics
  (subject_id, parent_topic_id, slug, name, level, is_active, metadata)
SELECT
  p.subject_id,
  p.id,
  'gk-' || trim(both '-' from regexp_replace(lower(v.name), '[^a-z0-9]+', '-', 'g'))
        || '-' || left(md5(v.name), 8),
  v.name,
  'microtopic',
  true,
  '{"tier":"official"}'::jsonb
FROM (VALUES

  -- History
  ('05d91fb6-221d-4e09-8477-c3d0f8a50138',
   'Freedom-movement leaders and their offices'),   -- 2025 Q38, 2026 Q58
  ('05d91fb6-221d-4e09-8477-c3d0f8a50138',
   'National memorials and samadhis'),   -- 2026 Q8
  ('05d91fb6-221d-4e09-8477-c3d0f8a50138',
   'Ancient Indian texts and statecraft'),   -- 2026 Q21
  ('05d91fb6-221d-4e09-8477-c3d0f8a50138',
   'Medieval Indian empires'),   -- 2026 Q18
  ('05d91fb6-221d-4e09-8477-c3d0f8a50138',
   'Post-independence revolutions and their pioneers'),   -- 2026 Q75

  -- Geography
  ('c7f0e071-3a29-4165-8ae9-2e43d2199a86',
   'Mountain ranges and peaks'),   -- 2025 Q3, 2026 Q43
  ('c7f0e071-3a29-4165-8ae9-2e43d2199a86',
   'Countries, capitals and territorial extent'),   -- 2025 Q5, 2025 Q47, 2026 Q70
  ('c7f0e071-3a29-4165-8ae9-2e43d2199a86',
   'Rivers of the world'),   -- 2025 Q64
  ('c7f0e071-3a29-4165-8ae9-2e43d2199a86',
   'Major dams and river valley projects'),   -- 2026 Q54
  ('c7f0e071-3a29-4165-8ae9-2e43d2199a86',
   'Climate and ocean-atmosphere phenomena'),   -- 2023 Q28
  ('c7f0e071-3a29-4165-8ae9-2e43d2199a86',
   'Mineral resources and mining sites'),   -- 2024 Q59, 2026 Q33
  ('c7f0e071-3a29-4165-8ae9-2e43d2199a86',
   'Crops, cropping seasons and leading producer states'),   -- 2025 Q1, 2025 Q31
  ('c7f0e071-3a29-4165-8ae9-2e43d2199a86',
   'Geographical indication tags and crafts'),   -- 2025 Q36
  ('c7f0e071-3a29-4165-8ae9-2e43d2199a86',
   'Biodiversity and wildlife conservation'),   -- 2026 Q24, 2026 Q65
  ('c7f0e071-3a29-4165-8ae9-2e43d2199a86',
   'World Heritage and protected sites'),   -- 2025 Q80
  ('c7f0e071-3a29-4165-8ae9-2e43d2199a86',
   'Landmarks and monuments'),   -- 2026 Q25, 2026 Q42
  ('c7f0e071-3a29-4165-8ae9-2e43d2199a86',
   'Transport corridors and ports'),   -- 2023 Q6, 2026 Q50

  -- Indian Polity and Constitution
  ('e8948b78-aa3e-434b-9959-32c047b42be9',
   'Union-State relations and the legislative lists'),   -- 2023 Q76
  ('e8948b78-aa3e-434b-9959-32c047b42be9',
   'Schedules of the Constitution'),   -- 2024 Q63
  ('e8948b78-aa3e-434b-9959-32c047b42be9',
   'Fundamental Rights'),   -- 2026 Q71
  ('e8948b78-aa3e-434b-9959-32c047b42be9',
   'President and Vice-President of India'),   -- 2025 Q59
  ('e8948b78-aa3e-434b-9959-32c047b42be9',
   'Union ministries and departments'),   -- 2023 Q41, 2026 Q12
  ('e8948b78-aa3e-434b-9959-32c047b42be9',
   'Statutory commissions and information oversight'),   -- 2024 Q68
  ('e8948b78-aa3e-434b-9959-32c047b42be9',
   'Judiciary and judicial administration'),   -- 2026 Q72
  ('e8948b78-aa3e-434b-9959-32c047b42be9',
   'Civilian awards and the honours system'),   -- 2026 Q10
  ('e8948b78-aa3e-434b-9959-32c047b42be9',
   'Recent central legislation and statutory penalties'),   -- 2024 Q64
  ('e8948b78-aa3e-434b-9959-32c047b42be9',
   'Comparative government and world legislatures'),   -- 2024 Q67

  -- Indian Economy
  ('40066b89-bcfd-4ffc-8a68-73158a678dc9',
   'Urban development and governance missions'),   -- 2023 Q47, 2023 Q68, 2026 Q77
  ('40066b89-bcfd-4ffc-8a68-73158a678dc9',
   'Rural livelihood and skill development missions'),   -- 2024 Q7, 2025 Q22
  ('40066b89-bcfd-4ffc-8a68-73158a678dc9',
   'Rural road connectivity schemes'),   -- 2026 Q45
  ('40066b89-bcfd-4ffc-8a68-73158a678dc9',
   'Agriculture and rural energy schemes'),   -- 2023 Q67
  ('40066b89-bcfd-4ffc-8a68-73158a678dc9',
   'Self-help groups and rural women'),   -- 2025 Q72
  ('40066b89-bcfd-4ffc-8a68-73158a678dc9',
   'Multilateral development finance in India'),   -- 2023 Q77, 2025 Q66
  ('40066b89-bcfd-4ffc-8a68-73158a678dc9',
   'Biofuel and renewable energy projects'),   -- 2025 Q65
  ('40066b89-bcfd-4ffc-8a68-73158a678dc9',
   'Environmental clearance and industrial approvals'),   -- 2024 Q10

  -- General Science
  ('424cc8f1-b96f-4fef-b2db-76128e34c7d0',
   'Space missions and spacecraft'),   -- 2023 Q37, 2024 Q62
  ('424cc8f1-b96f-4fef-b2db-76128e34c7d0',
   'Solar system and planetary science'),   -- 2025 Q32
  ('424cc8f1-b96f-4fef-b2db-76128e34c7d0',
   'Astronomy and celestial nomenclature'),   -- 2024 Q73
  ('424cc8f1-b96f-4fef-b2db-76128e34c7d0',
   'Units and scales of measurement'),   -- 2023 Q59, 2024 Q78
  ('424cc8f1-b96f-4fef-b2db-76128e34c7d0',
   'Computing and artificial intelligence'),   -- 2024 Q80, 2026 Q32
  ('424cc8f1-b96f-4fef-b2db-76128e34c7d0',
   'Digital public platforms and e-governance technology'),   -- 2023 Q10, 2025 Q60, 2026 Q27, 2026 Q57
  ('424cc8f1-b96f-4fef-b2db-76128e34c7d0',
   'Scientific and research institutions'),   -- 2025 Q17, 2025 Q53, 2025 Q73

  -- Miscellaneous
  ('343ffe9a-9415-4efe-bb5d-d23f38e739b9',
   'International organisations and groupings'),   -- 2023 Q12, 2023 Q14, 2025 Q12, 2026 Q11, 2026 Q36
  ('343ffe9a-9415-4efe-bb5d-d23f38e739b9',
   'International agreements and declarations'),   -- 2023 Q38, 2024 Q58
  ('343ffe9a-9415-4efe-bb5d-d23f38e739b9',
   'Sustainable Development Goals'),   -- 2025 Q70
  ('343ffe9a-9415-4efe-bb5d-d23f38e739b9',
   'International summits and forums'),   -- 2025 Q69
  ('343ffe9a-9415-4efe-bb5d-d23f38e739b9',
   'World affairs and geopolitical terms'),   -- 2024 Q65, 2026 Q9
  ('343ffe9a-9415-4efe-bb5d-d23f38e739b9',
   'Defence forces, exercises and expeditions'),   -- 2025 Q6, 2025 Q55, 2025 Q71, 2026 Q26
  ('343ffe9a-9415-4efe-bb5d-d23f38e739b9',
   'Books and authors'),   -- 2024 Q74, 2025 Q76, 2026 Q30
  ('343ffe9a-9415-4efe-bb5d-d23f38e739b9',
   'Awards and honours'),   -- 2025 Q44, 2025 Q57
  ('343ffe9a-9415-4efe-bb5d-d23f38e739b9',
   'Sports terminology and Indian sporting history'),   -- 2023 Q65, 2024 Q53
  ('343ffe9a-9415-4efe-bb5d-d23f38e739b9',
   'World currencies'),   -- 2026 Q62
  ('343ffe9a-9415-4efe-bb5d-d23f38e739b9',
   'Business and startup terminology'),   -- 2024 Q66
  ('343ffe9a-9415-4efe-bb5d-d23f38e739b9',
   'Commemorative coins and numismatics'),   -- 2023 Q60
  ('343ffe9a-9415-4efe-bb5d-d23f38e739b9',
   'Climate awareness initiatives')   -- 2023 Q70
) AS v(parent, name)
JOIN public.topics p
  ON p.id = v.parent::uuid
ON CONFLICT DO NOTHING;

COMMIT;
