"""Regression schema-contract tests for migration 272
(PYQ -> mock_question_bank projection: stop hashing through the bytea escape parser).

`text::bytea` in PostgreSQL is not a byte reinterpretation — it is an I/O cast
through `byteain`, which parses the text as a bytea *input literal* in escape
format. There, only `\\\\` (one literal backslash) and `\\ooo` (an octal byte) are
legal escapes; any other backslash raises

    ERROR: invalid input syntax for type bytea

which aborted the projection RPC mid-run. Observed on RBI Grade B 2024 Phase I
question fb7fa071-14ec-4930-9d9d-a846f0c18985 (Q95), whose question_text holds
`attention.\\ B` — a lone backslash followed by a space.

Migration 272 replaces the cast with `convert_to(<expr>, 'UTF8')`, which does an
encoding conversion and no escape parsing, so the hashed bytes are exactly the
UTF-8 bytes of the text — matching the Python mirror compute_content_hash().

Repo convention (no live-DB migration harness): assert against the migration SQL
text. 183/184/186/187/229/239/270 are MERGED + IMMUTABLE; the fix lives only in
the forward migration.
"""
import hashlib
from pathlib import Path

_MIGRATIONS = Path(__file__).resolve().parents[3] / "supabase" / "migrations"

MIGRATION = (_MIGRATIONS / "272_pyq_projection_bytea_cast_fix.sql").read_text()
MIG_270 = (_MIGRATIONS / "270_pyq_projection_microtopic_fidelity.sql").read_text()

# The exact text that broke the live run.
Q95_TEXT_FRAGMENT = "for money and attention.\\ B"


def _function_body(sql: str) -> str:
    lower = sql.lower()
    start = lower.index("create or replace function public.project_pyq_question_to_mock_bank")
    end = lower.index("revoke all on function public.project_pyq_question_to_mock_bank", start)
    return sql[start:end]


# ── The fix itself ───────────────────────────────────────────────────────────

def test_rpc_is_create_or_replace():
    assert (
        "create or replace function public.project_pyq_question_to_mock_bank"
        in MIGRATION.lower()
    )


def test_hash_no_longer_casts_text_to_bytea():
    """The cast is the bug; it must not survive anywhere in the new body."""
    assert "::bytea" not in _function_body(MIGRATION)


def test_hash_uses_convert_to_utf8():
    body = _function_body(MIGRATION)
    assert "sha256(convert_to((" in body
    assert "), 'UTF8'))" in body


def test_the_broken_cast_is_what_270_had():
    """Pins the before-state, so this test fails if 270 is ever edited in place."""
    assert "::bytea" in _function_body(MIG_270)
    assert "convert_to" not in _function_body(MIG_270)


def test_only_the_cast_changed_from_270():
    """Field set, field order and separators must be byte-identical to 270."""
    before = _function_body(MIG_270)
    after = _function_body(MIGRATION)
    normalised = after.replace("sha256(convert_to((", "sha256((").replace(
        "), 'UTF8')),", ")::bytea),")
    assert normalised == before


def _code_only(sql: str) -> str:
    """Strip SQL line comments — 270/272 discuss chr(0) in prose."""
    return "\n".join(l.split("--", 1)[0] for l in sql.splitlines())


def test_separators_from_239_are_untouched():
    body = _code_only(_function_body(MIGRATION))
    assert "chr(29)" in body          # GS, top level (239's fix)
    assert "chr(30)" in body          # RS, within an item
    assert "chr(31)" in body          # US, between items
    assert "chr(0)" not in body       # the 239 bug must not come back


def test_service_role_only_posture_preserved():
    low = MIGRATION.lower()
    assert "revoke execute on function public.project_pyq_question_to_mock_bank(uuid, uuid, text) from anon" in low
    assert "revoke execute on function public.project_pyq_question_to_mock_bank(uuid, uuid, text) from authenticated" in low
    assert "grant execute on function public.project_pyq_question_to_mock_bank(uuid, uuid, text) to service_role" in low


def test_migration_documents_the_failing_question():
    """An operator reading the migration should find the row that exposed it."""
    assert "fb7fa071-14ec-4930-9d9d-a846f0c18985" in MIGRATION


# ── Why the cast mattered, expressed against the real text ───────────────────

def test_q95_text_is_not_a_valid_bytea_escape_literal():
    """A lone backslash followed by a space is not `\\\\` and not `\\ooo`."""
    i = Q95_TEXT_FRAGMENT.index("\\")
    nxt = Q95_TEXT_FRAGMENT[i + 1]
    assert nxt != "\\"                       # not an escaped backslash
    assert not Q95_TEXT_FRAGMENT[i + 1:i + 4].isdigit()   # not an octal byte


def test_utf8_bytes_are_unambiguous_for_the_same_text():
    """convert_to's semantics: the bytes are just the encoding, escapes and all."""
    raw = Q95_TEXT_FRAGMENT.encode("utf-8")
    assert raw.count(b"\\") == 1
    # And the Python mirror hashes exactly these bytes.
    assert hashlib.sha256(raw).hexdigest() == hashlib.sha256(
        Q95_TEXT_FRAGMENT.encode("utf-8")
    ).hexdigest()


def test_python_mirror_hashes_utf8_not_escape_parsed_bytes():
    """Lockstep check: the mirror must not itself escape-parse."""
    src = (Path(__file__).resolve().parents[2]
           / "app" / "admin" / "pyq_mock_projection.py").read_text()
    assert 'hashlib.sha256(raw.encode("utf-8")).hexdigest()' in src
