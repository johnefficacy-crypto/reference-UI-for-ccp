#!/usr/bin/env python3
"""Diagnose and repair the NABARD answer keys on already-loaded PYQ rows.

Why this exists: the load posted every option with the right `is_correct`
(verified in the emitted payloads: 1055/1055 questions carry exactly one
`is_correct: true`), but the live rows came back with none set. This tool reads
the live state first and says what is actually stored, then repairs it in place
rather than deleting and reloading 1055 questions.

The repair is the sanctioned CMS write:
    PATCH /api/admin/exam-intelligence-cms/pyq-options/{id}
          {"reason": ..., "payload": {"is_correct": true}}
`update_pyq_option`'s own docstring calls this the "mark-correct toggle", and
`is_correct` is in _OPTION_FIELDS, so no reload is required for the key itself.

`correct_option_id` CANNOT be repaired through the API and is not attempted:
it is absent from _QUESTION_FIELDS, so neither POST /pyq-questions nor
PATCH /pyq-questions/{id} nor the bulk import can write it. It is also not
required downstream — pyq_mock_projection._eligible() demands exactly one
`is_correct` option and only cross-checks `correct_option_id` when it is
non-null, so NULL is the safe state and a wrong value would be worse.

Every file read passes an explicit encoding: authored on Linux, run on Windows.

Usage:
    python3 workbench/nabard_fix_answer_keys.py --api-base https://... --token "%JWT%"
    python3 workbench/nabard_fix_answer_keys.py --paper NABARD-P1-REASONING-2021 ^
        --api-base https://... --token "%JWT%"
    python3 workbench/nabard_fix_answer_keys.py --apply --confirm ^
        --api-base https://... --token "%JWT%"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nabard_load import (  # noqa: E402
    EXAM_ID,
    PAPERS_PATH,
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


def run(api_base: str, token: str, only: str | None, apply: bool) -> int:
    api_base = api_base.rstrip("/")
    expected = expected_keys()
    live = existing_paper_codes(api_base, token)
    codes = [c for c in expected if c in live]
    if only:
        if only not in expected:
            print(f"unknown paper_code {only!r}", file=sys.stderr)
            return 2
        if only not in live:
            print(f"{only} is not loaded on this exam", file=sys.stderr)
            return 2
        codes = [only]
    missing = [c for c in expected if c not in live]
    if missing and not only:
        print(f"note: {len(missing)} built paper(s) are not loaded and are skipped: "
              f"{', '.join(sorted(missing)[:4])}{' …' if len(missing) > 4 else ''}\n")

    tot_q = tot_opts = already = repaired = cleared = failed = no_match = 0
    still_bad: list[str] = []

    for code in sorted(codes):
        paper_id = live[code]
        want = expected[code]
        qs = fetch_questions(api_base, token, paper_id)
        p_already = p_repaired = p_failed = p_nomatch = 0
        for q in qs:
            ref = q.get("source_question_ref")
            opts = fetch_options(api_base, token, q["id"])
            tot_q += 1
            tot_opts += len(opts)
            target = want.get(ref)
            if not target:
                p_nomatch += 1
                no_match += 1
                continue
            true_now = [o for o in opts if o.get("is_correct")]
            keyed = [o for o in opts if (o.get("option_label") or "").upper() == target]
            if len(keyed) != 1:
                p_nomatch += 1
                no_match += 1
                still_bad.append(f"{code} {ref}: no unique option labelled {target}")
                continue
            wrong_true = [o for o in true_now if o["id"] != keyed[0]["id"]]
            if len(true_now) == 1 and not wrong_true:
                p_already += 1
                already += 1
                continue
            if not apply:
                p_repaired += 1
                repaired += 1
                continue
            try:
                for o in wrong_true:
                    _request("PATCH", f"{api_base}{OPTIONS_PATH}/{o['id']}", token,
                             {"reason": REASON, "payload": {"is_correct": False}})
                    cleared += 1
                _request("PATCH", f"{api_base}{OPTIONS_PATH}/{keyed[0]['id']}", token,
                         {"reason": REASON, "payload": {"is_correct": True}})
                p_repaired += 1
                repaired += 1
            except AuthExpired as exc:
                print(f"\nTOKEN EXPIRED at {code} {ref}: {exc}", file=sys.stderr)
                print("Re-run the same command with a fresh --token; questions already "
                      "keyed are reported as 'ok' and skipped.", file=sys.stderr)
                return 4
            except (ApiError, RuntimeError) as exc:
                p_failed += 1
                failed += 1
                still_bad.append(f"{code} {ref}: {exc}")
        verb = "would fix" if not apply else "fixed"
        print(f"  {code:38} {len(qs):>3}q  ok {p_already:>3}  {verb} {p_repaired:>3}"
              f"  unmatched {p_nomatch:>3}  failed {p_failed:>3}")

    print(f"\nquestions inspected {tot_q}   options inspected {tot_opts}")
    print(f"already keyed {already}   {'repaired' if apply else 'to repair'} {repaired}"
          f"   cleared-wrong {cleared}   unmatched {no_match}   failed {failed}")
    for s in still_bad[:15]:
        print("  ⚠", s)
    if len(still_bad) > 15:
        print(f"  … and {len(still_bad) - 15} more")
    return 0 if not failed and not no_match else 5


def verify(api_base: str, token: str, code: str) -> int:
    """Post-repair proof for one paper: exactly one is_correct per question."""
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
    print(f"  exactly one is_correct : {len(qs) - len(bad)}/{len(qs)}")
    print(f"  correct_option_id set  : {coid_set}   null: {coid_null}"
          f"   (null is expected — no CMS endpoint can write it)")
    for b in bad[:10]:
        print("  ⚠", b)
    return 0 if not bad else 5


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-base", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--paper", help="restrict to one paper_code")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--verify", metavar="PAPER_CODE", help="post-repair proof for one paper")
    args = ap.parse_args()

    if args.verify:
        return verify(args.api_base, args.token, args.verify)
    if args.apply and not args.confirm:
        print("--apply needs --confirm", file=sys.stderr)
        return 2
    if not args.apply:
        print("DIAGNOSE (no writes). Add --apply --confirm to repair.\n")
    return run(args.api_base, args.token, args.paper, args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
