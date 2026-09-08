#!/usr/bin/env python3
"""Build (and optionally POST) the NABARD Grade A PYQ load from nabard_blocks.json.

Source  : workbench/nabard_blocks.json (49 blocks, 1173 questions, reconciled)
In scope: 1054 questions across 41 papers.
Excluded: General Awareness (100, subject-practice-framework.md §1.1) and the
          Phase 3 descriptive blocks (18, no options and no answer key).

Load path is the audited CMS bulk import, never raw SQL:
    POST /api/admin/exam-intelligence-cms/bulk-import  {reason, entity, rows}
`pyq-papers` forces trust_status='pending'; `pyq-questions` forces
reviewer_status='pending' and accepts an inline `options` array. Nothing here
promotes or projects anything.

Usage:
    python3 workbench/nabard_load.py                    # dry run + count table
    python3 workbench/nabard_load.py --emit OUTDIR      # also write the payloads
    python3 workbench/nabard_load.py --apply --confirm ^
        --api-base https://... --token "%JWT%"          # actually load

Every file read here passes an explicit encoding: this is authored on Linux and
run on Windows, where the default is cp1252 and the compendium's text raises
UnicodeDecodeError without it.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BLOCKS = ROOT / "workbench/nabard_blocks.json"

EXAM_ID = "fc164f14-4c83-4cd9-9960-ef1a57cc979c"          # national-nabard-grade-a
PHASE_ID = {
    1: "667205e4-77f4-4682-9d96-dece310d102d",            # Phase I
    2: "f10847c0-7024-40db-9042-8a55559e40bf",            # Phase II
}
# Block subject (nabard_extract canon) -> (subject_id, ref prefix)
SUBJECT = {
    "Quantitative Aptitude": ("55555555-5555-5555-5555-555555555551", "QA"),
    "English":               ("55555555-5555-5555-5555-555555555552", "ENG"),
    "Reasoning":             ("55555555-5555-5555-5555-555555555553", "REA"),
    "Computer Knowledge":    ("0f2ac69b-bd39-422e-bb0d-a81ac4ad3057", "CK"),
    "Decision Making":       ("1f91467a-69bd-4515-b125-d60c4e7ca4a0", "DM"),
    "ESI":                   ("1a874661-957e-4c81-a90f-b0634834d889", "ESI"),
    "ARD":                   ("dd3d3bf2-9da6-4525-a19f-48bf047adb35", "ARD"),
}
EXCLUDED_SUBJECTS = {"General Awareness"}   # §1.1
EXCLUDED_PHASES = {3}                       # descriptive, no options/answers

# 2|ARD|2023 is printed as one combined ARD/ESI paper. It loads as ONE paper row
# with metadata.subject_combined true and NO per-question subject attribution --
# subject resolution is deferred to tagging.
COMBINED_KEYS = {"2|ARD|2023"}
COMBINED_PREFIX = "ARDESI"

SOURCE_TYPE = "coaching"   # a coaching compendium PDF, not an official release
REASON = "NABARD Grade A PYQ frontload from the reconciled compendium extraction"

BULK_PATH = "/api/admin/exam-intelligence-cms/bulk-import"
PAPERS_PATH = "/api/admin/exam-intelligence-cms/pyq-papers"
# Free-tier Render cold-starts; the first call can idle for well over a minute.
HTTP_TIMEOUT_S = 180
# Bounded retry, on connect/read timeout ONLY. A 4xx/5xx is never retried here:
# a bulk-import POST is not idempotent, and a retry after a partial write would
# duplicate rows. Timeouts are retried because the request may not have landed;
# the paper_code skip below is what makes that safe.
MAX_TIMEOUT_RETRIES = 3
RETRY_BACKOFF_S = (5, 15, 30)


def load_blocks() -> list[dict]:
    return json.loads(BLOCKS.read_text(encoding="utf-8"))["blocks"]


def in_scope(b: dict) -> bool:
    return b["phase"] not in EXCLUDED_PHASES and b["subject"] not in EXCLUDED_SUBJECTS


def build(blocks: list[dict]) -> tuple[list[dict], list[str]]:
    """Return one entry per paper: {paper, questions, block_count, key}. Plus warnings."""
    papers: list[dict] = []
    warnings: list[str] = []
    seen_codes: set[str] = set()

    for b in blocks:
        if not in_scope(b):
            continue
        combined = b["key"] in COMBINED_KEYS
        if combined:
            subject_id, prefix = None, COMBINED_PREFIX
        else:
            if b["subject"] not in SUBJECT:
                warnings.append(f"{b['key']}: no subject mapping for {b['subject']!r} — SKIPPED")
                continue
            subject_id, prefix = SUBJECT[b["subject"]]

        code = b["paper_code"]
        if code in seen_codes:
            warnings.append(f"{b['key']}: duplicate paper_code {code!r}")
        seen_codes.add(code)

        meta = {
            "block_key": b["key"],
            "block_label": b["label"],
            "printed_page": b["printed_page"],
            "expected_from_heading": b["expected_from_heading"],
            "shift_source": b.get("shift_source"),
            "source_pdf": "docs/reference/pyq/NABARD-Grade-A-PYQ.pdf",
            "extraction": "workbench/nabard_extract.py",
        }
        if combined:
            meta["subject_combined"] = True
            meta["combined_subjects"] = ["ARD", "ESI"]
        else:
            meta["subject_id"] = subject_id
            meta["subject"] = b["subject"]

        paper = {
            "exam_id": EXAM_ID,
            "exam_phase_id": PHASE_ID[b["phase"]],
            "year": b["year"],
            "shift": b.get("shift"),
            "paper_code": code,
            "source_type": SOURCE_TYPE,
            "metadata": meta,
        }

        questions = []
        for pos, q in enumerate(b["questions"], start=1):
            printed = int(q["q_no"])
            # display_order is POSITIONAL: unique per paper by construction, and
            # immune to the printed-number defects the source carries.
            # question_number keeps the printed value.
            qmeta = {"printed_question_number": printed, "block_key": b["key"]}
            if q.get("flags"):
                qmeta["extraction_flags"] = q["flags"]
            if q.get("context"):
                qmeta["shared_context"] = q["context"]
            if combined:
                qmeta["subject_attribution"] = "deferred_to_tagging"
            row = {
                "question_number": printed,
                "display_order": pos,
                # Prefixed from the start: (pyq_paper_id, source_question_ref) is
                # unique, and unprefixed refs collided across sections.
                "source_question_ref": f"{prefix}-{b['year']}-Q{printed:03d}",
                "question_text": q["stem"],
                "question_type": "mcq",
                "metadata": qmeta,
                "options": [
                    {
                        "option_label": lab.upper(),
                        "source_label": f"({lab})",
                        "option_text": text,
                        "display_order": i,
                        "is_correct": (lab == q.get("answer_label")),
                    }
                    for i, (lab, text) in enumerate(sorted((q["options"] or {}).items()), start=1)
                ],
            }
            questions.append(row)

        # Per-paper integrity, asserted before anything is sent.
        nums = [r["question_number"] for r in questions]
        orders = [r["display_order"] for r in questions]
        refs = [r["source_question_ref"] for r in questions]
        if len(set(orders)) != len(orders):
            warnings.append(f"{code}: display_order not unique per paper")
        if len(set(refs)) != len(refs):
            warnings.append(f"{code}: source_question_ref not unique per paper")
        if len(set(nums)) != len(nums):
            dupes = [n for n, c in Counter(nums).items() if c > 1]
            warnings.append(f"{code}: question_number repeats {dupes} — printed-number defect")
        if nums != sorted(nums):
            warnings.append(f"{code}: printed question_number is not monotonic {nums}")
        no_answer = [r["question_number"] for r in questions
                     if not any(o["is_correct"] for o in r["options"])]
        if no_answer:
            warnings.append(f"{code}: {len(no_answer)} question(s) with no correct option: {no_answer}")

        papers.append({"key": b["key"], "paper": paper,
                       "questions": questions, "block_count": b["parsed"]})
    return papers, warnings


def report(papers: list[dict], warnings: list[str]) -> int:
    print(f"{'paper_code':38} {'block':>6} {'built':>6} {'delta':>6}  key")
    bad = 0
    tot_block = tot_built = 0
    for p in papers:
        nb, nq = p["block_count"], len(p["questions"])
        tot_block += nb
        tot_built += nq
        delta = nq - nb
        if delta:
            bad += 1
        print(f"{'*' if delta else ' '}{p['paper']['paper_code']:37} {nb:>6} {nq:>6} "
              f"{delta:>+6}  {p['key']}")
    print()
    print(f"papers: {len(papers)}    block total: {tot_block}    built total: {tot_built}")
    print(f"papers whose built count differs from the block count: {bad}")
    if warnings:
        print("\nwarnings:")
        for w in warnings:
            print("  ⚠", w)
    return bad


# ── HTTP ─────────────────────────────────────────────────────────────────────

class AuthExpired(RuntimeError):
    """401/403 mid-run. The JWT is ~1h and this load is 1055 questions."""


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body


def _request(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")

    last_timeout: Exception | None = None
    for attempt in range(MAX_TIMEOUT_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code in (401, 403):
                raise AuthExpired(f"HTTP {exc.code}: {body[:200]}") from exc
            # Never retried: the POST is not idempotent and may have partially
            # landed. Surface it and let the paper_code skip handle a resume.
            raise ApiError(exc.code, body) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if not isinstance(reason, (TimeoutError, OSError)) and not isinstance(exc, TimeoutError):
                raise
            last_timeout = exc
            if attempt == MAX_TIMEOUT_RETRIES:
                break
            wait = RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)]
            print(f"      timeout ({reason}); retry {attempt + 1}/{MAX_TIMEOUT_RETRIES} in {wait}s",
                  flush=True)
            time.sleep(wait)
    raise RuntimeError(f"{method} {url} timed out after {MAX_TIMEOUT_RETRIES} retries: {last_timeout}")


def existing_paper_codes(api_base: str, token: str) -> dict[str, str]:
    """paper_code -> paper id, for every paper already on this exam."""
    found: dict[str, str] = {}
    offset, limit = 0, 200
    while True:
        url = f"{api_base}{PAPERS_PATH}?exam_id={EXAM_ID}&limit={limit}&offset={offset}"
        page = _request("GET", url, token)
        items = page.get("items") or []
        for row in items:
            if row.get("paper_code"):
                found[row["paper_code"]] = row.get("id")
        if len(items) < limit:
            return found
        offset += limit


def apply_load(papers: list[dict], api_base: str, token: str) -> int:
    api_base = api_base.rstrip("/")
    bulk = f"{api_base}{BULK_PATH}"
    print("\nreading existing papers for the skip check …", flush=True)
    existing = existing_paper_codes(api_base, token)
    print(f"  {len(existing)} paper(s) already on this exam\n", flush=True)

    created_papers = skipped = failed_papers = 0
    created_qs = failed_qs = 0
    reached: tuple[str, int] | None = None

    for p in papers:
        code = p["paper"]["paper_code"]
        n = len(p["questions"])
        if code in existing:
            skipped += 1
            print(f"  SKIP    {code:38} already exists ({existing[code]})", flush=True)
            continue
        try:
            res = _request("POST", bulk, token,
                           {"reason": REASON, "entity": "pyq-papers", "rows": [p["paper"]]})
            rows = res.get("results") or []
            first = rows[0] if rows else {}
            if not first.get("ok"):
                failed_papers += 1
                print(f"  FAIL    {code:38} paper: {first.get('error', res)}", flush=True)
                continue
            paper_id = (first.get("row") or {}).get("id")
            if not paper_id:
                failed_papers += 1
                print(f"  FAIL    {code:38} paper insert returned no id", flush=True)
                continue
            created_papers += 1

            qrows = [dict(q, pyq_paper_id=paper_id) for q in p["questions"]]
            reached = (code, 0)
            qres = _request("POST", bulk, token,
                            {"reason": REASON, "entity": "pyq-questions", "rows": qrows})
            qresults = qres.get("results") or []
            ok = [r for r in qresults if r.get("ok")]
            bad = [r for r in qresults if not r.get("ok")]
            created_qs += len(ok)
            failed_qs += len(bad)
            reached = (code, len(ok))
            status = "OK  " if not bad else "PART"
            print(f"  {status}    {code:38} {len(ok):>3}/{n} questions", flush=True)
            for r in bad[:5]:
                idx = r.get("index")
                qn = qrows[idx]["source_question_ref"] if isinstance(idx, int) and idx < len(qrows) else idx
                print(f"            └ {qn}: {r.get('error')}", flush=True)
            if len(bad) > 5:
                print(f"            └ … and {len(bad) - 5} more", flush=True)
        except AuthExpired as exc:
            where = f"{reached[0]} after {reached[1]} question(s)" if reached else f"{code} (paper row)"
            print(f"\nTOKEN EXPIRED at {where}: {exc}", file=sys.stderr)
            print("Re-run the same command with a fresh --token; papers already "
                  "created are skipped by paper_code.", file=sys.stderr)
            return 4
        except (ApiError, RuntimeError) as exc:
            failed_papers += 1
            print(f"  FAIL    {code:38} {exc}", flush=True)

    print(f"\npapers created {created_papers}   skipped {skipped}   failed {failed_papers}")
    print(f"questions created {created_qs}   failed {failed_qs}")
    return 0 if (failed_papers == 0 and failed_qs == 0) else 5


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit", metavar="DIR", help="write the bulk-import payloads")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--api-base")
    ap.add_argument("--token")
    args = ap.parse_args()

    papers, warnings = build(load_blocks())
    bad = report(papers, warnings)

    if args.emit:
        out = Path(args.emit)
        out.mkdir(parents=True, exist_ok=True)
        (out / "papers.json").write_text(json.dumps(
            {"reason": REASON, "entity": "pyq-papers",
             "rows": [p["paper"] for p in papers]}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        for p in papers:
            (out / f"questions__{p['paper']['paper_code']}.json").write_text(json.dumps(
                {"reason": REASON, "entity": "pyq-questions", "rows": p["questions"]},
                ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nwrote payloads to {out}")

    if bad:
        print("\nHALTED: a paper's built count differs from its block count.", file=sys.stderr)
        return 1
    if args.apply:
        if not (args.confirm and args.api_base and args.token):
            print("--apply needs --confirm, --api-base and --token", file=sys.stderr)
            return 2
        return apply_load(papers, args.api_base, args.token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
