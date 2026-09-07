#!/usr/bin/env python3
"""MANDATORY import preflight: prove the seed's baked taxonomy IDs are the LIVE
english-language subject/topic/microtopic rows on the target database — with the
required active/level/parentage — BEFORE any batch is POSTed.

Why this exists: migration 205 inserts the deterministic
`md5('ewp:subject|topic|microtopic:<slug>')` IDs only when the slug is ABSENT
(`ON CONFLICT (slug) DO NOTHING` / insert-if-not-exists). On a database where the
English taxonomy predated 205 (e.g. a dev seed that created `english-language`
with a different UUID), the live IDs differ from the baked ones and
`cms_bulk_upsert_writing_prompts` fails `invalid_scope` on the first row. The
"deterministic IDs resolve anywhere" assumption is therefore NOT safe — verify.

This script recomputes each baked ID's slug, looks up the live row by slug, and
fails (non-zero exit) if any subject/topic/microtopic is missing, inactive, the
wrong level, mis-parented, or carries a different id than the seed baked in. If
it fails, resolve the live IDs (re-map the JSON `topic_id`/`microtopic_id` and the
form `subject_id` to the live values) before importing.

A REPORTED MISMATCH IS NOT A BUG IN THIS SCRIPT, AND IT IS NOT FIXED BY EDITING
THE COMMITTED SEED. The check compares the baked ID (recomputed from the slug)
against the live row, so on a database whose English taxonomy predates 205 it
will keep reporting the `english-language` subject and the `grammar` topic as
divergent no matter what the JSON says — it is reporting TAXONOMY divergence,
not file state. That is the design.

The re-map above belongs to the IMPORT STEP, on the operator's copy of the rows,
not to the repository. Commit `f93c32b` re-mapped `03_grammar.json`'s `topic_id`
to the live value in the repo and broke CI for everyone: the committed seed is
the canonical baked artifact, and every test plus every migration-built database
(CI included) resolves `grammar` to `md5('ewp:topic:grammar')`. Editing the
canonical file to match one environment cannot be right for all of them. Re-map
at import; leave the committed file baked.

Usage:
  EWP_PG_DSN=postgres://... python3 preflight_ids.py
"""
from __future__ import annotations

import hashlib
import os
import sys

SLUGS_TOPIC = ["sentence-construction", "grammar", "vocabulary-in-context", "paragraph-writing"]
MICRO_PARENT = {
    "simple-sentences": "sentence-construction", "compound-sentences": "sentence-construction",
    "complex-sentences": "sentence-construction", "sentence-transformation": "sentence-construction",
    "sentence-structure": "sentence-construction",
    "subject-verb-agreement": "grammar", "tense": "grammar", "articles": "grammar",
    "prepositions": "grammar", "pronoun-reference": "grammar", "modifiers": "grammar",
    "punctuation": "grammar", "spelling": "grammar",
    "word-choice": "vocabulary-in-context", "collocations": "vocabulary-in-context",
    "formal-vocabulary": "vocabulary-in-context", "redundancy": "vocabulary-in-context",
    "topic-sentence": "paragraph-writing", "cohesion": "paragraph-writing",
    "logical-order": "paragraph-writing", "conclusion": "paragraph-writing",
}


def _uuid(seed: str) -> str:
    h = hashlib.md5(seed.encode()).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def main():
    dsn = os.environ.get("EWP_PG_DSN")
    if not dsn:
        print("Set EWP_PG_DSN to the target database.", file=sys.stderr)
        return 2
    try:
        import psycopg  # psycopg3
        conn = psycopg.connect(dsn)
    except Exception:  # pragma: no cover - operator env
        import psycopg2  # type: ignore
        conn = psycopg2.connect(dsn)

    failures = []
    with conn, conn.cursor() as cur:
        cur.execute("SELECT id, is_active FROM public.subjects WHERE slug='english-language'")
        subj = cur.fetchone()
        baked_subject = _uuid("ewp:subject:english-language")
        if not subj:
            failures.append("subject english-language: MISSING")
            subject_id = None
        else:
            subject_id = str(subj[0])
            if not subj[1]:
                failures.append("subject english-language: inactive")
            if subject_id != baked_subject:
                failures.append(f"subject english-language: live id {subject_id} != baked {baked_subject}")

        for slug in SLUGS_TOPIC:
            cur.execute(
                "SELECT id, is_active, level FROM public.topics "
                "WHERE subject_id=%s AND parent_topic_id IS NULL AND slug=%s",
                (subject_id, slug),
            )
            row = cur.fetchone()
            baked = _uuid(f"ewp:topic:{slug}")
            if not row:
                failures.append(f"topic {slug}: MISSING"); continue
            if str(row[0]) != baked:
                failures.append(f"topic {slug}: live id {row[0]} != baked {baked}")
            if not row[1] or row[2] != "topic":
                failures.append(f"topic {slug}: inactive or wrong level ({row[2]})")

        for slug, parent in MICRO_PARENT.items():
            cur.execute(
                "SELECT t.id, t.is_active, t.level, p.slug FROM public.topics t "
                "JOIN public.topics p ON p.id = t.parent_topic_id "
                "WHERE t.slug=%s AND t.level='microtopic'",
                (slug,),
            )
            row = cur.fetchone()
            baked = _uuid(f"ewp:microtopic:{slug}")
            if not row:
                failures.append(f"microtopic {slug}: MISSING"); continue
            if str(row[0]) != baked:
                failures.append(f"microtopic {slug}: live id {row[0]} != baked {baked}")
            if not row[1] or row[2] != "microtopic":
                failures.append(f"microtopic {slug}: inactive or wrong level")
            if row[3] != parent:
                failures.append(f"microtopic {slug}: parent {row[3]} != expected {parent}")

    if failures:
        print("PREFLIGHT FAILED — do NOT import; resolve live IDs first:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PREFLIGHT OK — all baked subject/topic/microtopic IDs match the live active taxonomy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
