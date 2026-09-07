# Writing-prompt bank seed (Content Studio)

Authored seed for the English writing-prompt bank — **270 prompts** across the
five checklist targets (`docs/status/career-copilot-checklist.md` → "Prompt bank
seed"). This directory is **repo-authored content**: it has NOT been imported,
reviewed, or activated on any database.

| Batch file (row array) | Exercise type | Topic | Count |
|---|---|---|---|
| `01_sentence_construction.json` | `sentence_construction` | sentence-construction | 50 |
| `02_sentence_correction.json` | `sentence_correction` | sentence-construction | 50 |
| `03_grammar.json` | `sentence_correction` | grammar | 100 |
| `04_vocabulary.json` | `vocabulary_in_context` | vocabulary-in-context | 50 |
| `05_paragraph.json` | `paragraph_writing` | paragraph-writing | 20 |
| **Total** | | | **270** |

## Status vocabulary (do not collapse these)

- **Repo-authored** — present here (this PR). ✅
- **Imported** — bulk-imported → rows exist `reviewer_status='pending'`, `is_active=false`. ⛔ pending operator.
- **Verified** — a reviewer accepted them in Content Studio → Review Queue. ⛔.
- **Active** — `is_active=true`, gated behind the activation resolver AND the runtime blockers below. ⛔.

## File format — row arrays for the Bulk Import UI

Each `NN_*.json` is a **JSON array of prompt rows**, which is exactly what the
Content Studio Bulk Import UI parses (`PromptBulkImport.jsx`:
`Array.isArray(data) ? data : [data]`). The operator supplies **`subject_id` and
`reason` in the form fields** — the rows do NOT carry `subject_id` or exam
columns (prompts are subject-scoped). On import each prompt lands
`reviewer_status='pending'` / `is_active=false` via the audited
`cms_bulk_upsert_writing_prompts` RPC (migration 215). We do not seed with raw
`INSERT` — that would bypass the review lifecycle + audit.

For a direct API `curl` (which needs the `{reason, subject_id, rows}` envelope
instead of a bare array), wrap a file:

```bash
python3 to_api_envelope.py 03_grammar.json --subject-id "$SUBJECT_ID" \
    --reason "Seed import: grammar batch" > /tmp/env.json
curl -X POST "$API/api/admin/content-studio/writing-prompts/bulk" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" --data @/tmp/env.json
```

## Taxonomy IDs are NOT guaranteed portable — run the preflight

Rows bake the deterministic migration-205 IDs
(`md5('ewp:subject|topic|microtopic:<slug>')`). **This is only correct when
migration 205 created the English taxonomy fresh.** Migration 205 inserts those
IDs with insert-if-absent semantics (`ON CONFLICT (slug) DO NOTHING`), so on a
database where `english-language` (or a topic slug) pre-existed with a different
UUID, the live IDs differ and `cms_bulk_upsert_writing_prompts` fails
`invalid_scope` on the first row.

**Mandatory before import:** run the preflight, which proves every baked
subject/topic/microtopic ID is the live, active, correctly-parented row — and
fails otherwise. Re-map the IDs to the live values if it fails.

**Re-map at import, never in the repo.** The re-map applies to the operator's
copy of the rows being POSTed. The committed JSON stays baked, because it is the
canonical artifact every test and every migration-built database (CI included)
resolves against — `grammar` there is `md5('ewp:topic:grammar')`
(`54adbabc-…`), whatever a given live database happens to carry. `f93c32b`
re-mapped the committed `03_grammar.json` to a live value and turned `main` red
for everyone: two seed tests plus both PG-gated import tests. A live/baked
divergence that preflight reports is resolved on the live side — an operator
re-map now, or a forward migration that aligns the live taxonomy — not by
editing this file.

```bash
EWP_PG_DSN=postgres://... python3 preflight_ids.py
```

## Runtime blockers — these types must NOT be activated yet (CODE, not operator)

Authoring/importing/reviewing is fine, but **activation of the affected types is
blocked on runtime work that does not exist yet** (tracked as CODE blockers in
`career-copilot-checklist.md`, not reducible to an operator import/review step):

- **A correct answer cannot be told from an unrelated one.** All 50
  sentence-correction, 100 grammar, and the source-bearing vocabulary rows carry
  the sentence-to-fix in `source_text`, and correctness depends on
  meaning-preserving correction.

  The *delivery* half of this is DONE, and the earlier wording here was stale:
  `ewp_claim_evaluation_job` returns `prompt_text`/`source_text` from the
  immutable session snapshot (migration
  `222_ewp_prompt_snapshot_and_exam_derivation.sql:251-252`), and
  `evaluation_worker.py:245-252` threads both into `evaluate_language`.

  What remains is the JUDGEMENT half. `compute_source_comparison`
  (`language_evaluator.py:79-114`) is deterministic by design and returns
  `source_comparison_uncertain` for *every* non-trivial changed answer — meaning
  preservation is not deterministically decidable, and the similarity thresholds
  that would guess at it were rejected as gameable
  (`docs/architecture/ewp-semantic-evaluator-adapter.md` §2.2). So a
  meaning-preserving correction and a clean but unrelated sentence reach the
  identical fail-closed outcome: human review, zero mastery. Nothing wrongly
  passes — but nothing rightly passes either.

  The semantic adapter is the layer that separates them, and it runs in SHADOW:
  its verdict reaches telemetry only, never the canonical outcome. Both facts are
  pinned by
  `tests/study_os/test_writing_semantic_adapter.py::test_deterministic_layer_cannot_separate_a_correction_from_an_unrelated_sentence`
  and `::test_shadow_adapter_separates_them_but_cannot_change_the_canonical_outcome`.

  **Blocker:** `FF_WRITING_LLM_EVAL` must reach LIVE — gated on the §5.2
  promotion evidence — before these types activate. Until then activation stays
  fail-closed at the gate (`exercise_type_not_runtime_ready` /
  `semantic_evaluator_not_live`).
- **No paragraph rubric.** All 20 `paragraph_writing` rows omit `rubric_id` and no
  writing rubric is seeded, so the Stage-3 evaluator persists an empty
  `rubric_dimensions=[]` instead of grammar/cohesion/content/organisation.
  **Blocker:** seed + wire a paragraph rubric (and set `rubric_id`) before these
  activate.
- **Paragraph runtime (EWP-6)** remains blocked pending §16 approval — paragraph
  prompts stay inactive/unassignable regardless.

## Editorial answer-key / rationale fixtures

`answer_keys/` holds human-authored **reference answers + rationales** for every
*correction-type* prompt (one entry per source-bearing row: all 50
sentence-correction, 100 grammar, and the 36 source-bearing vocabulary rows —
186 total). These are **editorial documentation, not runtime data** — a
reviewer/evaluator reference for "what a good answer looks like, and why". They
are keyed by `external_key` and the echoed `source_text` byte-matches the seed
row. See `answer_keys/README.md` (scope, shape, and the sync check). The
open-ended production prompts (`01` sentence-construction, `05` paragraph, and
the 14 `Use the word "…"` / `Write one sentence using …` vocabulary rows) have no
single canonical answer and are intentionally excluded.

## Migration-history note

The duplicate migration 219 on `main` (two files at version 219) is a real
defect, but it is **out of scope for this seed** and is being handled in a
dedicated migration-repair PR with live `schema_migrations` evidence (a filename
rename cannot tell which 219 body each environment actually applied). This PR
touches no migrations.

## Regenerating & tests

`build_seed.py` is the source of truth; it validates every row against the
backend rules and fails loudly. `tests/study_os/test_writing_prompt_seed.py`
guards the committed JSON (arrays, microtopic→topic parentage, required-word
tokenizer + case-insensitive uniqueness, generator↔committed byte-identity, and
— in the backend CI — a parse through the real `WritingPromptBulkRow` model). A
full RPC round-trip (270 pending + idempotent re-import) belongs in an
EWP_PG_DSN-gated behavior test (see the checklist "remaining" note).

```bash
python3 build_seed.py            # regenerate + validate (270 rows)
```
