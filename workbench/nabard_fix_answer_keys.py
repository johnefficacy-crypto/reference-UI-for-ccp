#!/usr/bin/env python3
"""Diagnose and repair the NABARD answer keys on already-loaded PYQ rows.

WHAT ACTUALLY HAPPENED (corrected 2026-09-09 against live, not against the
design). Two separate columns carry the key:

  * ``pyq_options.is_correct`` — the generic CMS bulk import DOES carry this
    through. ``_IMPORT_CONFIG['pyq-questions']['inline']['allowed']`` is
    ``_OPTION_FIELDS`` (admin_exam_intel_cms.py:4659, :2002), and ``is_correct``
    is in that set, so every inline option row was inserted with the flag the
    load posted. An earlier read of mine said the endpoint "has no answer-key
    concept"; that is true of the *question* row, not of the option children.

  * ``pyq_questions.correct_option_id`` — genuinely unwritable through the CMS.
    It is absent from ``_QUESTION_FIELDS`` (:1980), so neither POST, nor PATCH,
    nor bulk import can set it. It is NOT architecturally always NULL: RBI 2022
    carries it on 115/115 questions, and migrations 228 and 268 are what set it
    on the older corpus. SQL is the only path.

So the repair is normally a ONE-column job. This tool reads live state first and
emits only the UPDATEs the data actually needs — rewriting ``is_correct`` on
rows that are already right is a needless write and a chance to be wrong where
the current state is correct.

Live state can come from either source:
  --from-export DIR   read review_out_*/questions_export.json + options_export.json,
                      which `scripts/pyq_question_review.py export` pulls from the
                      live CMS API. One small papers call is still made to map
                      paper_id -> paper_code (source_question_ref is NOT unique
                      across papers: the two Reasoning 2022 papers share 180 refs).
  (default)           page the CMS API directly: one call per question for its
                      options, which is ~1100 cold-start requests.

Every file read passes an explicit encoding: authored on Linux, run on Windows.

Usage:
    python3 workbench/nabard_fix_answer_keys.py --from-export review_out_nabard \
        --api-base https://... --token "%JWT%"
    python3 workbench/nabard_fix_answer_keys.py --from-export review_out_nabard \
        --emit-sql app/supabase/migrations/275_nabard_answer_keys.sql \
        --api-base https://... --token "%JWT%"
    python3 workbench/nabard_fix_answer_keys.py --verify NABARD-P1-REASONING-2021 \
        --api-base https://... --token "%JWT%"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nabard_load import (  # noqa: E402
    AuthExpired,
    ApiError,
    _request,
    build,
    existing_paper_codes,
    load_blocks,
)

QUESTIONS_PATH = "/api/admin/exam-intelligence-cms/pyq-questions"
OPTIONS_PATH = "/api/admin/exam-intelligence-cms/pyq-options"
REASON = "NABARD answer-key repair: mark the keyed option is_correct from the compendium answer key"


def expected_keys() -> dict[str, dict[str, str]]:
    """paper_code -> {source_question_ref: expected option_label (A..E)}."""
    out: dict[str, dict[str, str]] = {}
    for p in build(load_blocks())[0]:
        code = p["paper"]["paper_code"]
        out[code] = {}
        for q in p["questions"]:
            correct = [o["option_label"] for o in q["options"] if o["is_correct"]]
            if len(correct) == 1:
                out[code][q["source_question_ref"]] = correct[0]
    return out


# ── live-state sources ──────────────────────────────────────────────────────
# Both yield the same shape:
#   (paper_code, source_question_ref, question_id, correct_option_id, options)
# where options is [{id, option_label, is_correct}, ...].

def fetch_questions(api_base: str, token: str, paper_id: str) -> list[dict]:
    rows, offset, limit = [], 0, 200
    while True:
        page = _request("GET", f"{api_base}{QUESTIONS_PATH}"
                              f"?pyq_paper_id={paper_id}&limit={limit}&offset={offset}", token)
        items = page.get("items") or []
        rows.extend(items)
        if len(items) < limit:
            return rows
        offset += limit


def fetch_options(api_base: str, token: str, question_id: str) -> list[dict]:
    page = _request("GET", f"{api_base}{OPTIONS_PATH}?question_id={question_id}&limit=50", token)
    return page.get("items") or []


def state_from_api(api_base: str, token: str, codes: list[str], live: dict[str, str]):
    for code in codes:
        for q in fetch_questions(api_base, token, live[code]):
            yield (code, q.get("source_question_ref"), q["id"],
                   q.get("correct_option_id"), fetch_options(api_base, token, q["id"]))


def state_from_export(export_dir: Path, codes: list[str], live: dict[str, str]):
    qs = json.loads((export_dir / "questions_export.json").read_text(encoding="utf-8"))
    os_ = json.loads((export_dir / "options_export.json").read_text(encoding="utf-8"))
    by_q: dict[str, list[dict]] = {}
    for o in os_:
        by_q.setdefault(o["question_id"], []).append(o)
    code_of = {pid: code for code, pid in live.items()}
    seen_papers: set[str] = set()
    for q in qs:
        code = code_of.get(q.get("paper_id"))
        if code is None or code not in codes:
            continue
        seen_papers.add(code)
        yield (code, q.get("source_question_ref"), q["id"],
               q.get("correct_option_id"), by_q.get(q["id"], []))
    absent = sorted(set(codes) - seen_papers)
    if absent:
        print(f"note: {len(absent)} loaded paper(s) have no rows in the export "
              f"(export scope is narrower): {', '.join(absent[:4])}"
              f"{' …' if len(absent) > 4 else ''}\n", file=sys.stderr)


# ── collection ──────────────────────────────────────────────────────────────

class Plan:
    """What live state says the repair must change, and nothing more."""

    def __init__(self) -> None:
        self.pairs: list[tuple[str, str, str, str]] = []   # (qid, oid, code, ref)
        self.set_true: list[tuple[str, str, str]] = []     # (oid, code, ref)
        self.set_false: list[tuple[str, str, str]] = []    # (oid, code, ref)
        self.set_coid: list[tuple[str, str, str, str]] = []
        self.unmatched: list[str] = []
        self.per_paper: dict[str, dict[str, int]] = {}
        self.questions = 0
        self.options = 0
        self.coid_already = 0


def collect(rows, expected: dict[str, dict[str, str]]) -> Plan:
    plan = Plan()
    for code, ref, qid, coid, opts in rows:
        p = plan.per_paper.setdefault(
            code, {"q": 0, "true_ok": 0, "true_missing": 0, "true_wrong": 0,
                   "coid_ok": 0, "coid_missing": 0, "unmatched": 0})
        plan.questions += 1
        plan.options += len(opts)
        p["q"] += 1
        target = (expected.get(code) or {}).get(ref)
        if not target:
            p["unmatched"] += 1
            plan.unmatched.append(f"{code} {ref}: no expected key")
            continue
        keyed = [o for o in opts if (o.get("option_label") or "").upper() == target.upper()]
        if len(keyed) != 1:
            p["unmatched"] += 1
            plan.unmatched.append(f"{code} {ref}: no unique option labelled {target}")
            continue
        oid = keyed[0]["id"]
        plan.pairs.append((qid, oid, code, ref))
        if keyed[0].get("is_correct") is True:
            p["true_ok"] += 1
        else:
            p["true_missing"] += 1
            plan.set_true.append((oid, code, ref))
        for o in opts:
            if o.get("is_correct") is True and o["id"] != oid:
                p["true_wrong"] += 1
                plan.set_false.append((o["id"], code, ref))
        if coid == oid:
            p["coid_ok"] += 1
            plan.coid_already += 1
        else:
            p["coid_missing"] += 1
            plan.set_coid.append((qid, oid, code, ref))
    return plan


def report(plan: Plan) -> None:
    print(f"  {'paper_code':38} {'q':>4} {'is_correct ok':>14} {'missing':>8} "
          f"{'wrong-true':>11} {'coid ok':>8} {'coid null/wrong':>16} {'unmatched':>10}")
    for code in sorted(plan.per_paper):
        p = plan.per_paper[code]
        print(f"  {code:38} {p['q']:>4} {p['true_ok']:>14} {p['true_missing']:>8} "
              f"{p['true_wrong']:>11} {p['coid_ok']:>8} {p['coid_missing']:>16} "
              f"{p['unmatched']:>10}")
    print(f"\nquestions {plan.questions}   options {plan.options}   matched {len(plan.pairs)}")
    print(f"  is_correct already true on the keyed option : "
          f"{len(plan.pairs) - len(plan.set_true)}/{len(plan.pairs)}")
    print(f"  is_correct to set true                      : {len(plan.set_true)}")
    print(f"  is_correct to clear (true on a wrong option): {len(plan.set_false)}")
    print(f"  correct_option_id already correct           : {plan.coid_already}")
    print(f"  correct_option_id to set                    : {len(plan.set_coid)}")
    print(f"  unmatched                                   : {len(plan.unmatched)}")
    for u in plan.unmatched[:15]:
        print("  ⚠", u)
    if len(plan.unmatched) > 15:
        print(f"  … and {len(plan.unmatched) - 15} more")


def gather(api_base: str, token: str, only: str | None,
           export_dir: Path | None) -> tuple[Plan, dict[str, str]]:
    expected = expected_keys()
    live = existing_paper_codes(api_base, token)
    codes = sorted(c for c in expected if c in live)
    if only:
        if only not in expected:
            raise SystemExit(f"unknown paper_code {only!r}")
        if only not in live:
            raise SystemExit(f"{only} is not loaded on this exam")
        codes = [only]
    missing = [c for c in expected if c not in live]
    if missing and not only:
        print(f"note: {len(missing)} built paper(s) are not loaded and are skipped: "
              f"{', '.join(sorted(missing)[:4])}{' …' if len(missing) > 4 else ''}\n")
    rows = (state_from_export(export_dir, codes, live) if export_dir
            else state_from_api(api_base, token, codes, live))
    return collect(rows, expected), live


# ── actions ─────────────────────────────────────────────────────────────────

def diagnose(api_base: str, token: str, only: str | None, export_dir: Path | None) -> int:
    plan, _ = gather(api_base.rstrip("/"), token, only, export_dir)
    report(plan)
    if not plan.set_true and not plan.set_false and not plan.set_coid:
        print("\nNothing to repair: live already matches the compendium key.")
        return 0
    print("\nRun again with --emit-sql FILE to write the migration. Only the "
          "UPDATEs listed above as non-zero are emitted.")
    return 0


def apply_is_correct(api_base: str, token: str, plan: Plan) -> int:
    """PATCH the is_correct flags only. correct_option_id still needs SQL."""
    api_base = api_base.rstrip("/")
    done = failed = 0
    for oid, code, ref in plan.set_false + plan.set_true:
        want = (oid, code, ref) in plan.set_true
        try:
            _request("PATCH", f"{api_base}{OPTIONS_PATH}/{oid}", token,
                     {"reason": REASON, "payload": {"is_correct": want}})
            done += 1
        except AuthExpired as exc:
            print(f"\nTOKEN EXPIRED at {code} {ref}: {exc}", file=sys.stderr)
            print("Re-run with a fresh --token; rows already correct are skipped.",
                  file=sys.stderr)
            return 4
        except (ApiError, RuntimeError) as exc:
            failed += 1
            print(f"  ⚠ {code} {ref}: {exc}", file=sys.stderr)
    print(f"is_correct patched {done}, failed {failed}")
    print(f"correct_option_id still unset on {len(plan.set_coid)} question(s) — "
          f"no CMS endpoint writes it; use --emit-sql.")
    return 0 if not failed else 5


def verify(api_base: str, token: str, code: str) -> int:
    """Post-repair proof for one paper, read straight from the API."""
    api_base = api_base.rstrip("/")
    live = existing_paper_codes(api_base, token)
    if code not in live:
        print(f"{code} not loaded", file=sys.stderr)
        return 2
    qs = fetch_questions(api_base, token, live[code])
    bad, coid_set, coid_null = [], 0, 0
    for q in qs:
        opts = fetch_options(api_base, token, q["id"])
        t = [o for o in opts if o.get("is_correct")]
        if len(t) != 1:
            bad.append(f"{q.get('source_question_ref')}: {len(t)} correct")
            continue
        coid = q.get("correct_option_id")
        if coid is None:
            coid_null += 1
        elif coid == t[0]["id"]:
            coid_set += 1
        else:
            bad.append(f"{q.get('source_question_ref')}: correct_option_id points elsewhere")
    print(f"\nVERIFY {code}: {len(qs)} questions")
    print(f"  exactly one is_correct                    : {len(qs) - len(bad)}/{len(qs)}")
    print(f"  correct_option_id set and agreeing        : {coid_set}   still null: {coid_null}")
    for b in bad[:10]:
        print("  ⚠", b)
    return 0 if not bad and not coid_null else 5


def emit_sql(api_base: str, token: str, only: str | None,
             export_dir: Path | None, out: Path) -> int:
    """Write the repair as a migration in the shape of 268_answer_key_migration.sql.

    Data-driven: each of the three UPDATEs is emitted only if live state needs
    it. On this load that is expected to be correct_option_id alone — the bulk
    import carried is_correct through on the inline option rows.
    """
    plan, _ = gather(api_base.rstrip("/"), token, only, export_dir)
    report(plan)
    if not plan.pairs:
        print("nothing matched; not writing", file=sys.stderr)
        return 5
    if not plan.set_true and not plan.set_false and not plan.set_coid:
        print("\nLive already matches the compendium key. No migration written.")
        return 0

    src = f"the live export in {export_dir}" if export_dir else "the live CMS API"
    body = [
        "-- Answer keys for NABARD Grade A, Phase I and Phase II, 2020-2023.",
        "--",
        "-- Source: the answer key printed against each question in the compendium",
        "-- (docs/reference/pyq/NABARD-Grade-A-PYQ.pdf), carried through",
        "-- workbench/nabard_blocks.json as answer_label and matched to the loaded",
        f"-- option by (paper_code, source_question_ref, option_label) against {src}.",
        "--",
        "-- The load went through the generic CMS bulk import. That endpoint DOES",
        "-- carry pyq_options.is_correct on inline option children (_OPTION_FIELDS),",
        "-- but pyq_questions.correct_option_id is absent from _QUESTION_FIELDS and",
        "-- cannot be written by any CMS route, so it landed NULL on every row.",
        "-- Same shape as 268_answer_key_migration.sql, restricted to the columns",
        "-- live state actually shows wrong.",
        "--",
        f"-- {len(plan.pairs)} question(s) matched. "
        f"is_correct: {len(plan.pairs) - len(plan.set_true)} already true, "
        f"{len(plan.set_true)} to set, {len(plan.set_false)} to clear. "
        f"correct_option_id: {plan.coid_already} already correct, "
        f"{len(plan.set_coid)} to set."
        + (f" {len(plan.unmatched)} unmatched and deliberately absent." if plan.unmatched else ""),
        "",
        "BEGIN;",
        "",
    ]
    if plan.set_false:
        body += [
            f"-- {len(plan.set_false)} option(s) carry is_correct on the wrong label.",
            "UPDATE public.pyq_options o",
            "SET is_correct = false",
            "FROM (VALUES",
            ",\n".join(f"  ('{oid}'::uuid)" for oid, _, _ in plan.set_false),
            ") AS v(option_id)",
            "WHERE o.id = v.option_id;",
            "",
        ]
    if plan.set_true:
        body += [
            f"-- {len(plan.set_true)} keyed option(s) are missing is_correct. The other",
            f"-- {len(plan.pairs) - len(plan.set_true)} are already true and are not rewritten.",
            "UPDATE public.pyq_options o",
            "SET is_correct = true",
            "FROM (VALUES",
            ",\n".join(f"  ('{oid}'::uuid)" for oid, _, _ in plan.set_true),
            ") AS v(option_id)",
            "WHERE o.id = v.option_id;",
            "",
        ]
    if plan.set_coid:
        body += [
            f"-- {len(plan.set_coid)} question(s) need correct_option_id. This is the",
            "-- column no API path can write; it is why this migration exists.",
            "UPDATE public.pyq_questions q",
            "SET correct_option_id = v.option_id",
            "FROM (VALUES",
            ",\n".join(f"  ('{qid}'::uuid, '{oid}'::uuid)"
                       for qid, oid, _, _ in plan.set_coid),
            ") AS v(question_id, option_id)",
            "WHERE q.id = v.question_id;",
            "",
        ]
    body += ["COMMIT;", ""]
    out.write_text("\n".join(body), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-base", required=True)
    ap.add_argument("--token", required=True,
                    help="needed even with --from-export: paper_id -> paper_code")
    ap.add_argument("--paper", help="restrict to one paper_code")
    ap.add_argument("--from-export", metavar="DIR",
                    help="read live state from review_out_*/questions_export.json "
                         "+ options_export.json instead of paging the API")
    ap.add_argument("--apply", action="store_true",
                    help="PATCH the is_correct flags that live state shows wrong")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--verify", metavar="PAPER_CODE", help="post-repair proof for one paper")
    ap.add_argument("--emit-sql", metavar="FILE",
                    help="write a migration-268-shaped repair, only for what live needs")
    args = ap.parse_args()

    export_dir = Path(args.from_export) if args.from_export else None
    if export_dir and not (export_dir / "options_export.json").exists():
        print(f"{export_dir}/options_export.json not found", file=sys.stderr)
        return 2

    if args.verify:
        return verify(args.api_base, args.token, args.verify)
    if args.emit_sql:
        return emit_sql(args.api_base, args.token, args.paper, export_dir, Path(args.emit_sql))
    if args.apply:
        if not args.confirm:
            print("--apply needs --confirm", file=sys.stderr)
            return 2
        plan, _ = gather(args.api_base.rstrip("/"), args.token, args.paper, export_dir)
        report(plan)
        if not plan.set_true and not plan.set_false:
            print("\nNo is_correct write needed. correct_option_id needs --emit-sql.")
            return 0
        return apply_is_correct(args.api_base, args.token, plan)
    print("DIAGNOSE (no writes).\n")
    return diagnose(args.api_base, args.token, args.paper, export_dir)


if __name__ == "__main__":
    raise SystemExit(main())
