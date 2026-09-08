"""Validate the committed writing-prompt bank seed (Content Studio bulk-import).

Guards `app/supabase/seeds/writing_prompts/*.json` — UI-uploadable ROW ARRAYS —
against the same rules the merged backend enforces (migration 215 +
content_studio.py), and pins `build_seed.py` as the source of truth so the
generator and the committed JSON cannot drift.

Two layers:
  * offline (always runs): shape/keys/UUID parentage/required-word rules +
    generator-identity;
  * production-model parse (skipped if the backend package can't import here):
    parse every row through the authoritative `PromptBulkRow` Pydantic model.

The full round-trip through `cms_bulk_upsert_writing_prompts` on a disposable
Postgres (270 pending rows + idempotent re-import) is still pending as an
EWP_PG_DSN-gated behavior test and is tracked in the checklist/seed README.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import unicodedata
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[4]
_SEED_DIR = _REPO / "app" / "supabase" / "seeds" / "writing_prompts"

_EXERCISE_TYPES = {
    "sentence_construction", "sentence_correction", "vocabulary_in_context",
    "sentence_rewrite", "sentence_reconstruction", "paragraph_writing",
    "summary_writing", "precis_practice", "essay_practice", "letter_practice",
}
_ALLOWED_ROW_KEYS = {
    "external_key", "exercise_type", "topic_id", "microtopic_id", "prompt_text",
    "source_text", "required_words", "required_sentence_count", "difficulty_level",
    "min_words", "max_words", "max_rewrite_attempts", "rubric_id", "source_document_id",
}
_FORBIDDEN_ROW_KEYS = {"subject_id", "exam_id", "exam_cycle_id", "exam_phase_id", "metadata"}

_WORD_RE = re.compile(r"[^\W_]+(?:['\-][^\W_]+)*", re.UNICODE)
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_EXPECTED_COUNTS = {
    "01_sentence_construction.json": 50,
    "02_sentence_correction.json": 50,
    "03_grammar.json": 100,
    "04_vocabulary.json": 50,
    "05_paragraph.json": 20,
}

# Migration-205 taxonomy: topic slug -> its microtopic slugs (the parentage the
# backend's ewp_validate_prompt_scope enforces). Only the four topics the seed uses.
_TAXONOMY = {
    "sentence-construction": ["simple-sentences", "compound-sentences", "complex-sentences",
                              "sentence-transformation", "sentence-structure"],
    "grammar": ["subject-verb-agreement", "tense", "articles", "prepositions",
                "pronoun-reference", "modifiers", "punctuation", "spelling"],
    "vocabulary-in-context": ["word-choice", "collocations", "formal-vocabulary", "redundancy"],
    "paragraph-writing": ["topic-sentence", "cohesion", "logical-order", "conclusion"],
}


def _uuid(seed: str) -> str:
    h = hashlib.md5(seed.encode()).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# The live `grammar` topic predates migration 205. 205 seeds taxonomy
# insert-if-absent (ON CONFLICT DO NOTHING / WHERE NOT EXISTS on the slug), so on
# this database the baked md5 id was never inserted and the row that exists
# carries c4b8ebe3-3173-4864-9e04-16ab99470c6e. The live id is the source of
# truth: `cms_bulk_upsert_writing_prompts` resolves scope against the live row
# and fails `invalid_scope` on the baked one. Keep this map, the generator, the
# committed JSON and migration 205 in agreement — f93c32b changed the JSON alone
# and turned main red.
LIVE_TOPIC_ID = {"grammar": "c4b8ebe3-3173-4864-9e04-16ab99470c6e"}

_TOPIC_ID = {s: LIVE_TOPIC_ID.get(s, _uuid(f"ewp:topic:{s}")) for s in _TAXONOMY}
# topic_id -> {allowed microtopic_id, ...}
_TOPIC_TO_MICROS = {
    _TOPIC_ID[t]: {_uuid(f"ewp:microtopic:{m}") for m in micros}
    for t, micros in _TAXONOMY.items()
}


def _load(name):
    return json.loads((_SEED_DIR / name).read_text())


def test_committed_files_are_row_arrays_totalling_270():
    total = 0
    for name, expected in _EXPECTED_COUNTS.items():
        rows = _load(name)
        assert isinstance(rows, list), f"{name} must be a row ARRAY (UI-uploadable), not an envelope"
        assert len(rows) == expected, name
        total += len(rows)
    assert total == 270


def test_every_row_matches_the_backend_contract():
    seen_keys = set()
    for name in _EXPECTED_COUNTS:
        for r in _load(name):
            key = r.get("external_key")
            assert isinstance(key, str) and key.strip() and key not in seen_keys, f"{name}: bad/dup external_key {key!r}"
            seen_keys.add(key)

            assert not (_FORBIDDEN_ROW_KEYS & r.keys()), f"{key}: forbidden keys {_FORBIDDEN_ROW_KEYS & r.keys()}"
            assert set(r) <= _ALLOWED_ROW_KEYS, f"{key}: unknown keys {set(r) - _ALLOWED_ROW_KEYS}"

            assert r["exercise_type"] in _EXERCISE_TYPES, key
            assert r["topic_id"] in _TOPIC_TO_MICROS, f"{key}: unexpected topic_id"
            if "microtopic_id" in r:
                assert _UUID_RE.match(r["microtopic_id"]), key
                assert r["microtopic_id"] in _TOPIC_TO_MICROS[r["topic_id"]], (
                    f"{key}: microtopic is not a child of the stated topic"
                )

            assert isinstance(r["prompt_text"], str) and r["prompt_text"].strip(), key
            assert isinstance(r["difficulty_level"], int) and 1 <= r["difficulty_level"] <= 10, key

            for f in ("min_words", "max_words", "required_sentence_count", "max_rewrite_attempts"):
                if f in r:
                    assert isinstance(r[f], int) and not isinstance(r[f], bool), f"{key}: {f} not int"
            if "required_sentence_count" in r:
                assert r["required_sentence_count"] > 0, key
            if "min_words" in r and "max_words" in r:
                assert r["max_words"] >= r["min_words"], f"{key}: max<min"

            seen_words = set()
            for w in r.get("required_words", []) or []:
                e = unicodedata.normalize("NFC", w).strip()
                toks = _WORD_RE.findall(e)
                assert len(toks) == 1 and toks[0] == e, f"{key}: bad required word {w!r}"
                low = e.lower()
                assert low not in seen_words, f"{key}: case-insensitive duplicate required word {w!r}"
                seen_words.add(low)


def test_external_keys_are_namespaced():
    for name in _EXPECTED_COUNTS:
        for r in _load(name):
            assert r["external_key"].startswith("ewp-seed-"), r["external_key"]


def test_generator_output_matches_committed_json():
    """build_seed.py is the source of truth — regenerating must reproduce the
    committed artifacts byte-for-byte (no silent drift)."""
    spec = importlib.util.spec_from_file_location("build_seed", _SEED_DIR / "build_seed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name, _, builder in mod.BATCHES:
        expected = json.dumps(builder(), ensure_ascii=False, indent=2) + "\n"
        assert (_SEED_DIR / name).read_text() == expected, f"{name} is stale — rerun build_seed.py"


def _backend_importable():
    try:
        importlib.import_module("app.api.content_studio")
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _backend_importable(), reason="backend package not importable in this env")
def test_rows_parse_through_the_production_pydantic_model():
    from app.api.content_studio import WritingPromptBulkRow  # authoritative strict model

    for name in _EXPECTED_COUNTS:
        for r in _load(name):
            WritingPromptBulkRow(**r)  # extra='forbid' + strict types → raises on any drift
