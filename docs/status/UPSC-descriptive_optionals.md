# UPSC descriptive practice — discussion summary

## Corpus position

UPSC Mains holds **1,131 descriptive questions** already tagged to microtopics across GS1–GS4, plus **100 Essay PYQs** tagged against a separate 15-theme taxonomy. None of it is practisable.

## Why it cannot project

`pyq_mock_projection.py:389` blocks any question whose `question_type` is not `mcq`, returning `not_mcq`. Reviewing or tagging descriptive questions changes nothing — the mock path is closed to them by design.

## What exists instead

The `writing_practice` subsystem is fully built and running:

- `writing_prompts`, `writing_sessions`, `writing_session_units`, `writing_unit_versions`, `writing_evaluations`, `writing_rubrics`, issue events and lineage, `writing_mastery_outbox`
- `writing:evaluate` job live in production (`notifications/scheduler.py:253,371`)
- Migration 205 landed the schema; 207/209/213/214/215/218/222/226/234/236/238/240 built out the chain

Content state: **271 prompts**, of which 270 were imported this session and 1 pre-existed. All `pending`, `is_active=false`.

## The gap

**There is no link from `pyq_questions` to `writing_prompts`.** Migration 214 dropped `writing_prompts.exam_id` and moved exam applicability into `writing_prompt_targets` (`is_global` / `exam_family_id` / `exam_id` / `exam_phase_id`), so a bridge writes two rows, not one. Migration 222 exists because 214's drop broke `ewp_claim_evaluation_job`'s exam derivation — that seam has failed once.

## Why UPSC is harder than RBI descriptive

| | RBI/NABARD descriptive | UPSC Mains |
|---|---|---|
| answer shape | bounded, factual | argumentative, open |
| model answer | authoritative | one of many valid |
| evaluation | content coverage | structure, dimensions, directive verb |
| evidence base | stable | decays with current affairs |

Two excellent Mains answers can share almost no content. "Critically examine" demands something different from "discuss." A fixed model answer decays as current affairs move.

## Evaluation is blocked, and the block is circular

`compute_source_comparison` (`language_evaluator.py:79-114`) resolves only three cases — missing source, empty answer, answer identical to source. Everything else returns `source_comparison_uncertain` and routes to human review. Verified: a meaning-preserving correction, an aggressive-but-correct rewrite, a clean unrelated sentence and an unrelated ungrammatical sentence all produce **identical** result objects.

The semantic adapter exists but runs SHADOW only. `docs/architecture/ewp-semantic-evaluator-adapter.md` §5.2 blocks LIVE until, per exercise type: 500 human-labelled answers, false-positive ≤5%, false-negative ≤10%, operator sign-off. `writing_language_evaluator_runs` is **0** — no shadow data exists.

The circularity: evidence needs submissions, submissions need active prompt types, activation needs LIVE, LIVE needs evidence.

**Also relevant to UPSC specifically:** §8 places Stage-3 rubric evaluation explicitly out of scope for that doc, gated on EWP-6 §16, and no paragraph rubric is seeded — so Stage-3 persists `rubric_dimensions=[]`. Rubric evaluation is precisely what Mains answers need.

## Competitor reconnaissance

AnswerWriting.com and EdutorAI both ship AI grading that produces a mark out of 10 or 15 — LIVE mode by your architecture's definition, without the gate. AnswerWriting accepts handwritten upload (PDF or 15 images with OCR), which matches how the exam is actually written. Its AI-generated daily questions do not read like real UPSC questions.

## Decisions taken

- **Do not adopt competitor-style AI marking.** Their evaluator is not trusted; the numeric score is undefended.
- **Component coverage over marks.** If AI is used, extract expected components and show covered/missed — do not produce a number.
- **Self-evaluation as the primary loop** — aspirant writes, deterministic checks run, model answer revealed, aspirant self-scores against a fixed binary checklist, self-score feeds mastery. This sidesteps §5.2 entirely and generates human labels as a by-product.
- **RBI/NABARD first, UPSC second.** Bounded answers validate the loop before the hard judgement problem.
- **UPSC Mains explicitly out of the first scope.**
- **Handwritten input: undecided**, left open rather than rejected.

## Conclusion

UPSC descriptive practice is a **feature needing scoping, not a task needing finishing**. The subsystem exists; the bridge, the model-answer source, and the evaluation approach do not.

Two things remain unanswered and both should precede code:

1. **What do aspirants actually do today for descriptive practice?** Both candidate designs assume they are already writing. 30 minutes with two or three aspirants settles it.
2. **Self-scoring against a fixed checklist, or AI-extracted component coverage?** Different products, different gate exposure. The design doc should argue for one rather than assume.

The differentiator available to you and not to competitors: practice driven by verified PYQ frequency and per-microtopic mastery — "this appeared in three of five papers and your mastery is 40" — rather than by generated questions.