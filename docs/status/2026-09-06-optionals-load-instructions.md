# Loading the UPSC Mains optional corpus — operator instructions

Six subject JSONs, 139 papers, 4,015 questions, plus `upsc-pubad-topicwise.json`
(1,799 questions, 1987–2025). Extracted 2026-09-06 from coaching compilations.

Read `docs/architecture/2026-09-05-mains-optionals-strategy.md` first — it holds
the shared-microtopic decision this load depends on. Tagging is **not** part of
this pass.

---

## Before anything is written

### 1. Confirm the source is what you think it is

Every row is from a coaching compiler, not UPSC. `verified_against_official` is
false on all of them.

The Prelims corpus had exactly this and it cost 102 wrong answer labels: seven
aggregator papers were loaded with `source_type='official'`, and nothing
downstream could tell. Set `source_type='aggregator'` on every paper and source
row. It is the field a future reader uses to decide how much to trust the
content, and it cannot be inferred later.

### 2. Subject identity, decided before the first row

One subject per optional. Slug `upsc-cse-mains-opt-<name>`, matching the
`upsc-cse-mains-gs*` convention — **not** `upsc-mains-*`, which is the retired
naming.

`upsc-mains-gs1` through `gs4` were four subject rows holding zero topics while
their `upsc-cse-mains-*` twins held all 444, with every Mains section pointing at
the empty ones. Nobody noticed for weeks. Verify immediately after creating each
subject:

```sql
SELECT sub.slug, s.section_label,
       (SELECT count(*) FROM public.topics t WHERE t.subject_id = sub.id) AS topics
FROM public.subjects sub
LEFT JOIN public.exam_phase_sections s ON s.subject_id = sub.id
WHERE sub.slug LIKE 'upsc-cse-mains-opt-%'
ORDER BY sub.slug;
```

`subject_group` — check `_GROUP_FAMILY` and `_SLUG_FAMILY` in
`study_os/subject_runtime_policy.py` before choosing. `gs` is deliberately
unmapped so PYQ-backed subjects keep the generic runtime, which is probably right
here too. The cautionary case is `upsc-csat`, which carries
`subject_group='aptitude'` matching nothing, so CSAT silently falls out of its own
readiness checks.

### 3. Phase and sections

Optionals sit on the Mains template phase
`626ec667-4bbf-4420-8715-48c5b83e0d11` — cycle-agnostic, the same phase GS I–IV
and Essay use.

Each optional has Paper I and Paper II with different syllabi. **Two sections
under one subject**, consistent with how GS I–IV are modelled. Not two subjects.

`exam_phases` has no unique constraint on name; five phases named "Prelims"
already exist for UPSC. Scope every query by phase id, never by name.

---

## Loading

### 4. Descriptive questions may not fit the v1 importer

`pyq_bulk_import` v1 requires `option_a`–`option_d` and `correct_option` on every
row (`exam_intelligence/pyq_bulk_import.py:292-345`). Optional questions have
none of those.

**Before writing a converter, find out how the 1,131 Mains GS questions were
loaded.** They are descriptive and they are in the database, so a path exists.
Whatever it is, follow it.

If nothing suitable exists, direct SQL is acceptable here — but then the two
importer gaps become your responsibility, because they were only found by
noticing them on the Prelims load:

- v1 sets neither `section_id` nor `pyq_questions.correct_option_id`
- three manually inserted 2018 questions ended up with NULL `section_id` and
  needed backfilling

Verify after every paper:

```sql
SELECT count(*) AS questions,
       count(section_id) AS with_section,
       count(DISTINCT section_id) AS distinct_sections
FROM public.pyq_questions WHERE pyq_paper_id = '<paper_id>';
```

### 5. Preserve what the extraction established, and mark what it inferred

These belong in `pyq_questions.metadata` at load time. Backfilling them means
re-reading sources you have already parsed past.

- **`question_number_inferred: true`** on the PSIR rows whose Q1–Q8 numbering was
  reconstructed from the mark pattern. Those numbers are not printed in the
  source. They were validated against the article's own syllabus-mapping tables,
  which is real evidence — but a derived value must not read as an observed one.
  This is the same rule as `year = NULL` on IRDAI.
- **`marks`** per question, and **`marks_inferred: null`** where the extraction
  declined to guess. 102 rows carry `structure_anomaly`; keep that key.
- **`duplicate_in_source: true`** on the four Sociology 2014 Paper-2 rows where
  Q5 and Q6 share sub-parts. Corrupt at source, not in extraction.
- **`source_page`** — every correctness question ends in "check against the
  source", and a page number makes that a lookup rather than a search.
- **`source_question_ref`**, prefixed by paper from the start. It is the
  unique-per-paper dedup key; unprefixed refs collided during the Mains frontload
  and produced a generic "Internal server error".

`metadata` is a **whole-column replace** on the CMS patch route. Read the row,
merge, then write — otherwise the patch drops every other key and nothing errors.

### 6. Coverage is uneven, and that must survive into the data

Geography has 6 years, History 9, against a 10-year target. The PDFs do not
contain the earlier years.

Record per-subject year coverage on the subject or paper metadata. Any high-yield
ranking computed over 6 papers is thinner than one over 13, and a reader of the
output has no way to know unless the data says so.

---

## After loading

### 7. Verify each paper before moving to the next

```sql
SELECT p.year, p.paper_code, count(q.id) AS questions,
       count(q.section_id) AS with_section,
       count(*) FILTER (WHERE q.metadata ? 'marks') AS with_marks
FROM public.pyq_papers p
JOIN public.pyq_questions q ON q.pyq_paper_id = p.id
WHERE p.exam_phase_id = '626ec667-4bbf-4420-8715-48c5b83e0d11'
  AND p.pyq_source_id = '<this subject source id>'
GROUP BY 1,2 ORDER BY 1;
```

Counts must match the extraction report per paper. A mismatch means questions
crossed a paper boundary — the NABARD extraction caught 19 questions silently
filed under the wrong year that way, and they were valid, well-formed and
undetectable except by reconciling against an independent count.

### 8. Everything stays pending

`trust_status` on papers and `reviewer_status` on questions both stay `pending`.
Do not promote either as part of the load.

Paper-level and question-level are separate gates: promoting the paper does not
verify its questions, and the 2024 Prelims paper was fully tagged,
difficulty-judged and structurally valid while carrying ten wrong answers.

`review_pyq_paper()` has no question-count guard on the DB path, which is how a
paper with zero questions was once marked verified.

### 9. Do not tag in this pass

Tagging needs the overlap mapping from the strategy doc §3 — every optional theme
classified `shared`, `concept-child` or `new` against the existing GS
microtopics. Tagging first to a parallel tree and reconciling later is much more
work than mapping first.

---

## Not in scope for this load

- `upsc-maths-ocr-RAW.json` — 265 OCR pages, marked not corpus-ready. Leave it.
- Mathematics and Commerce & Accountancy — sources still missing.
- Hindi — dropped per instruction.

---

## Operator notes that cost time on this corpus

- **The Supabase SQL editor auto-commits per execution.** An explicit `begin;`
  opens a nested transaction that is discarded and every statement inside
  silently rolls back reporting success. Use `psql -f`, or paste without a
  wrapper.
- **"Success. No rows returned"** is what an UPDATE prints whether it changed
  eighty rows or none. Always follow with a SELECT.
- **Joining `pyq_options` or `pyq_question_topic_tags` multiplies question
  counts.** Use `count(DISTINCT q.id)`.
- **The editor truncates long UUIDs pasted into a query** and appends its own
  `limit 100`. Put multi-id filters in a subquery.
- **Rebuild `$hdr` after every token refresh** — a hashtable captures the value
  at creation. The JWT expires roughly hourly.
- **The API is free-tier Render** and cold-starts past the 60s client timeout.
  Warm it before any batch run.

See `docs/runbooks/POWERSHELL-SOP.md` for the full set.
