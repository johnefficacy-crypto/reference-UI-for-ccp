---
owner: study-os
status: architecture decision + phased implementation plan (contract-first; PLANNED)
last_verified_against_code: 2026-07-11
source_of_truth: code
related_code:
  - app/backend/app/api/subject_practice.py
  - app/backend/app/study_os/subjects.py
  - app/backend/app/study_os/planner.py
  - app/backend/app/study_os/attempt_evidence.py
  - app/backend/app/study_os/mastery_engine/schemas.py
  - app/backend/app/study_os/mastery_engine/mastery_delta.py
  - app/backend/app/study_os/mock_blueprint_selection.py
  - app/frontend/src/pages/study/StudyHome.jsx
  - app/frontend/src/pages/study/Subjects.jsx
  - app/frontend/src/pages/study/mocks/components/questions/shared/MathRenderer.jsx
related_migrations:
  - app/supabase/migrations/029_exam_intelligence_taxonomy.sql
  - app/supabase/migrations/030_exam_registry_cycles_phases.sql
  - app/supabase/migrations/135_mock_engine_core.sql
  - app/supabase/migrations/161_mock_pipeline_gate.sql
review_cadence: per-sprint
supersedes_none: true
sibling_doc: docs/architecture/current-affairs-pipeline.md
---

# Subject Practice Framework — GA / Quant / Reasoning Expansion

**Status:** CONTRACT-FIRST / INCREMENTAL DELIVERY. Runtime policy, Quant Calculation Gym,
Reasoning timed practice, and GA current-affairs slices have shipped in code; validation and
remaining rollout gates live in
`docs/status/career-copilot-pr-plan.md` § Lane GQR and `docs/status/career-copilot-checklist.md`
§ GA / Quant / Reasoning Expansion.

**Companion contract:** the General Awareness current-affairs vertical is large enough and
AI-governance-sensitive enough to warrant its own contract —
`docs/architecture/current-affairs-pipeline.md`. This doc owns the cross-subject runtime policy,
Quant, and Reasoning; the companion owns GA current-affairs.

---

## 1. Locked product scope

### 1.1 General Awareness (v1 = current-affairs practice only)
The product subject remains **General Awareness**. The first supported capability is
**current-affairs practice only**, with learner modes `weekly_current_affairs` and
`monthly_current_affairs`. GA v1 explicitly excludes PYQ ingestion/projection, PYQ-based
prioritisation, static-GK practice, permanent topic mastery, long-term SRS, standalone sectional
mocks, permanent Mistake Book entries, and automatic publication of AI-generated questions. GA
performance may be retained historically for reports but **must not affect `user_topic_mastery`**
(consistent with the domain rule "eligibility/verified-only reads" and the mastery-live gate). Full
design in the companion contract.

**Amended 2026-09-07 — see §1.1.1.** This blanket exclusion is narrowed for the RBI Grade B
GA corpus: durable GA questions are eligible for PYQ tagging and projection; perishable ones
stay excluded exactly as written above.

#### 1.1.1 Amendment — RBI Grade B GA durable carve-out (2026-09-07)

§1.1 excludes General Awareness from PYQ ingestion, projection and permanent topic mastery
wholesale. That rule was written against GA-as-current-affairs and is too coarse for the RBI
Grade B GA corpus, which is not one population but **three**.

All 320 RBI Grade B GA questions (Q1–80 across 2023, 2024, 2025 and 2026; 2022 has no GA
section) were classified in `workbench/rbi-ga-classification.csv`:

| | count | |
|---|---:|---|
| **Durable, answer inside an existing finance subject** | 112 | banking 71, economics 24, capital-market 15, financial-awareness 1, pension-sector 1 |
| **Durable, non-finance** | 90 | static GK — geography, polity, history, science, international relations |
| **Perishable** | 118 | a dated instance, an as-of figure, or a per-edition datum |
| **Total** | **320** | |

A question is DURABLE when its answer stays true across years. The boundary rule is that a
durable *subject* with a dated *instance* is perishable: "which body regulates X" is durable,
"what did X reach in FY22" is not.

**The carve-out.** RBI Grade B GA questions whose answer is durable are eligible for PYQ tagging
and projection — the 112 against the existing finance subjects, the 90 against the
`general-knowledge` subject. The 118 perishable questions **stay excluded** under the existing
§1.1 rule: they remain current-affairs practice only, contribute no permanent topic mastery, and
are never projected.

This narrows §1.1's exclusion from "GA" to "perishable GA". It does not reopen GA current-affairs
for mastery, and it does not extend to any other body's GA section — the classification is per
corpus, and no other GA corpus has been classified.

**Drift worth watching.** Durable-non-finance is not a static residue; it is growing:

| year | durable, non-finance |
|---|---:|
| 2023 | 17 |
| 2024 | 16 |
| 2025 | 28 |
| 2026 | 29 |

RBI GA is moving toward static GK at least as fast as toward banking. If the trend holds, the
general-knowledge subject carries more of this corpus each cycle than the finance subjects do,
and sizing decisions that assume GA ≈ banking will be wrong.

**The `general-knowledge` subject.** It exists as a **body-agnostic** subject — no `exams` key in
its metadata, unlike the exam-scoped subjects seeded in migration 269 — with **7 sections and 55
microtopics**. It was built **from the corpus, not from a textbook contents page**: every
microtopic has at least one real question behind it, and
`workbench/rbi-gk-microtopic-map.csv` carries the question → microtopic mapping.
Migration `273_general_knowledge_microtopics.sql` adds the 55 microtopics and documents six
sections; **International Relations was added as a seventh** once Miscellaneous reached 26
questions and international organisations, agreements and summits proved to be a coherent group
rather than leftovers.

> **Open provenance gap.** The International Relations section was applied live from a scratch
> script (`app/add_gk_ir.sql`, deleted in `4a62535`) and is not recorded by any numbered
> migration, so the live taxonomy is ahead of the migration history. Under the migration
> discipline in `CLAUDE.md` this needs a forward migration before the carve-out is loaded.
> `workbench/rbi-gk-microtopic-map.csv` and migration 273's header still describe the
> six-section shape.

Nothing here tags or projects a question. Tagging remains a separate, reviewed step, and the
verified-only read rule is unchanged: a carved-out GA question reaches a learner only after it
passes the same review lifecycle as any other PYQ.

---

### 1.2 Quantitative Aptitude
Quant supports normal objective practice, reusable reviewed **solution heuristics**, deterministic
**Calculation Gym** sessions, **separate accuracy / speed / calculation-efficiency signals**, and
planner recommendations from a **versioned deterministic policy** (shadow first).

### 1.3 Reasoning (v1 = text only)
Reasoning v1 supports **text-based questions and shared text/table stimuli only**.

**Deferred, as a conscious and documented coverage gap:** non-verbal reasoning, figure options,
option media, mirror/water images, paper folding & cutting, embedded-figure questions, and any
image-based scoring. This is a real omission — non-verbal is roughly 40–50 % of SSC General
Intelligence & Reasoning and a meaningful slice of RRB — so it is tracked as a named deferred slice
(`GQR-R2 non_verbal`, PLANNED) rather than left implicit. "Reasoning shipped" in v1 means
**text reasoning shipped**, not full SSC reasoning coverage.

---

## 2. Subject-runtime policy (the cross-subject prerequisite)

### 2.1 Problem (code-verified)
Today the subject surface hard-codes two runtimes:
- `study_os/subjects.py::_subject_practice` appends `english_writing` when `eng_available` and
  `topic_pyq` when projected PYQ topics exist — nothing else is expressible.
- `api/subject_practice.py::start_subject_practice` dispatches by `mode` through explicit
  `english_writing` / `topic_pyq` branches (server owns exam/prompt/topic resolution; the browser
  never selects content — keep this property).
- `study_os/planner.py` stamps `launch_type = "pyq_practice"` for `_LAUNCH_STAMP_TASK_TYPES =
  {retrieval_practice, revision}` and leaves other task types unstamped.

This assumes every practicable subject is either English-writing or PYQ-backed. GA current-affairs
and specialised Quant modes fit neither, so a server-owned runtime registry is a hard prerequisite.

### 2.2 `SubjectRuntimePolicy` registry
Introduce a **server-owned** registry (Python, not a table in v1 — it is code-governed config,
mirroring `_LAUNCH_STAMP_TASK_TYPES`) keyed by subject family:

```text
SubjectRuntimePolicy
- subject_family            # english | quant | reasoning | general_awareness
- supported_modes           # ordered list, exposed to the hub
- inventory_resolver        # server fn: resolves the eligible content pool
- attempt_kind              # which attempt shell/table a mode lands in
- mastery_enabled           # bool — GA is always false
- correction_enabled        # bool — GA is always false in v1
- retry_policy              # none | ephemeral_ca | normal_srs
- planner_resolver          # server fn: task_type + signals -> runnable launch
```

Initial policies:

```text
english             : objective_practice, english_writing_session
quant               : topic_practice, timed_practice, heuristic_drill, calculation_gym
reasoning           : topic_practice, timed_practice, reasoning_set
general_awareness   : weekly_current_affairs, monthly_current_affairs
```

Responsibilities:
- `subjects.py` exposes **only** modes allowed by policy (removes the `eng_available` / PYQ
  hard-coding; the hub already renders `practice.modes` generically, so no hub rewrite is needed).
- `subject_practice.py` resolves launch handlers through the registry rather than an `if mode ==`
  ladder. Server continues to own exam, bundle, inventory and question selection; the browser
  submits only the selected `mode`.
- `StudyHome.jsx` replaces the single hard-coded `LaunchWritingPracticeButton` branch in
  `NextActionCard` with one typed generic launcher keyed off the server-stamped `launch_type`.
- `planner.py` resolves a runnable launch from subject policy (`planner_resolver`) instead of
  stamping broad retrieval/revision task types as `pyq_practice`. The existing stamp mechanism is
  reused and generalised — not reinvented.

Compatibility: English and PYQ behaviour must be preserved by policy entries + regression tests;
this PR ships no behavioural change for existing subjects.

---

## 3. Quant

### 3.1 Heuristic authority
Reusable, reviewed solution heuristics — subject/topic-scoped canonical content governed in Content
Studio (see `content-studio.md`, which already lists "quant/reasoning drills" as canonical content).

```text
quant_heuristics
- id
- topic_id / microtopic_id
- heuristic_code
- name
- heuristic_type            # shortcut | standard_method | trap | estimation
- applicability_rule        # STRUCTURED condition where possible (jsonb), not free text alone
- formula_latex             # rendered via existing KaTeX path
- standard_method
- shortcut_method
- worked_example
- common_traps
- reviewer_status           # pending -> verified | rejected | needs_correction
- is_active

quant_question_heuristics
- question_id
- heuristic_id
- relevance
- reviewer_status
```

`applicability_rule` is a structured condition so heuristic selection is not purely free-text
matching. **`expected_time_saving_pct` is intentionally excluded from the v1 schema** — it would be
an unvalidated editorial estimate; a reviewed target-time may be added later from real attempt data.

Question feedback may show: standard method → faster method → validity conditions → common trap →
(when reviewed data exists) target solve time. Math already renders in question stem and options via
`MathRenderer`/KaTeX (`$…$`/`$$…$$`); no new rendering work is required for Quant text/options.

### 3.2 Calculation Gym (deterministic, no LLM)
A rapid-fire retrieval sub-runtime for calculation fluency. Initial skills: tables, squares, cubes,
square/cube roots, fraction↔percentage conversion, ratio simplification, approximation, common
multiplication patterns.

```json
{ "mode": "calculation_gym", "skill": "squares", "question_count": 20, "duration_sec": 180 }
```

The server owns range, **random seed**, question generation, expected answers, and session limits.
Generated questions **and the seed are frozen** so a session is reproducible. No LLM.

### 3.3 Quant performance signal (sibling to mastery, NOT a mastery tier)
The unified attempt-evidence contract already carries what is needed:
`mastery_engine/schemas.py::AttemptQuestionAnalytics` has `expected_time_sec`, `actual_time_sec`,
`is_correct`, `attempted`, `topic_id`, `microtopic_id`. Derive a **sibling** signal — never a new
`user_topic_mastery` writer:

```text
quant_performance_signals
- user_id, exam_id, topic_id, microtopic_id
- signal_type
- sample_count
- accuracy_pct
- median_time_ratio      # actual_time_sec / expected_time_sec
- p75_time_ratio
- confidence
- policy_version
- computed_at
- input_fingerprint
```

Exclude unanswered questions, missing/zero expected time, zero-duration responses, extreme dwell
outliers, and untrusted/incomplete attempts. Initial recommendation labels: `insufficient_evidence,
concept_gap, application_gap, speed_gap, calculation_gap, stable`.

**Thresholds are shadow defaults, not product truth:** centrally versioned (`policy_version`),
covered by deterministic tests, evaluated in shadow, recalibrated from real attempt distributions.

**Do not touch the existing time weighting in the same PR.** A 5 % over-time penalty already exists
in `mastery_engine/mastery_delta.py:60-61` (`weight *= 0.95` when `actual > expected`); the legacy
`mastery.py::recompute_topic_mastery` applies none. These are the two unreconciled mastery writers
that are the mock engine's central pre-live blocker. The new signal is derived independently first;
any change to the existing weighting is a **separate governed decision** after comparison, and must
account for **both** writers.

Planner mapping (after shadow validation): `concept_gap → concept learning`, `application_gap →
guided topic practice`, `speed_gap → heuristic drill`, `calculation_gap → calculation gym`,
`stable → normal revision`.

---

## 4. Reasoning (text runtime)

Reuse the existing objective runtime and shared **text/table** stimuli (`pyq_stimuli` already
supports `passage | caselet | table` and links N-to-M via `pyq_question_stimuli`). Initial coverage:
analogy/classification, number/alphabet series, coding-decoding, blood relations, directions,
ranking/ordering, syllogism, statement/conclusion, statement/assumption, logical sequence,
**text-based seating arrangements**, **text-based puzzles**.

Launcher modes: `topic_practice`, `timed_practice`, `reasoning_set`. `error_correction` and
`revision` are **planner intents that resolve to one of these modes** — they do not become separate
attempt engines. No option-image schema or non-verbal dependency is introduced (see §1.3 deferral).

---

## 5. Planner and downstream

The planner consumes three distinct signal classes and must not conflate them:
- **mastery** — concept understanding (existing `user_topic_mastery`, shadow-gated).
- **error evidence** — misconception, careless, option trap, misread.
- **performance signal** — Quant speed / calculation efficiency (§3.3), never folded into mastery.

Routing:
- **GA is calendar-driven** — an eligible-but-incomplete weekly/monthly bundle emits a
  current-affairs task; GA completion/accuracy appears in reports labelled *current-affairs practice
  performance*, never as enduring subject mastery.
- **Quant is evidence-driven** — mastery + errors + performance signal → runtime recommendation.
- **Reasoning** uses mastery + error evidence through the normal objective-practice path.

Error-vocabulary reconciliation: the two divergent correction vocabularies
(`mastery_engine/correction_tasks.py` vs `mocks.py`) remain the pre-live blocker; the Quant planner
activation PR must consume a single reconciled categorizer, not add a third.

---

## 6. Admin placement (no new surface)
No new top-level admin destination. The no-new-surface rule (locked 2026-06-21) holds. Quant
heuristics author inside **Content Studio** as canonical content. GA current-affairs review is an
embedded Content Studio work queue (companion doc). Tabs / drill-in views only — no sidebar
destination is added.

---

## 7. Corrections folded in from cross-examination (record)
1. The launcher/hub are already generic + server-owned; PR-1 is "runtime registry + adapters,"
   not a rewrite.
2. The planner already stamps `pyq_practice` generically; extend the map.
3. Math already renders in stem/options (KaTeX); no new Quant rendering needed.
4. Time weighting lives in `mastery_engine/mastery_delta.py`, not `mastery.py`; two unreconciled
   writers exist — handle both, separately from the new signal.
5. Non-verbal reasoning is a large, explicitly-named deferred gap (§1.3), not silently out of scope.
6. Attempt analytics already carry expected+actual time — the Quant signal is derivable without new
   attempt columns.
