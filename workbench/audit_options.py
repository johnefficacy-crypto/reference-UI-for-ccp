#!/usr/bin/env python3
"""Audit the option-label repairs in nabard_extract.py.

Uniform {a,b,c,d,e} output proves the repair produced five labels; it does NOT
prove no question was silently re-lettered wrong. This walks the SAME parser
code path (parse_questions, via the private _raw_labels it records) and reports,
per question, the option labels exactly as printed in reading order before any
repair -- so every question the repair touched can be inspected by hand.

Three questions are answered:
  A. which questions have a DUPLICATE label inside one option run
  B. which questions' printed labels are not a clean ascending a..e run
  C. which questions hit each repair path, with before -> after

Usage:  python3 workbench/audit_options.py [--all]
        --all also lists the clean a..e majority (1000+ lines).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nabard_extract as N  # noqa: E402

CLEAN = ["a", "b", "c", "d", "e"]


def collect() -> list[dict]:
    pages = N.read_pages()
    blocks = N.classify(N.parse_toc(pages))
    blocks, full = N.locate_headings(pages, blocks)
    located = sorted(
        [b for b in blocks if b["_offset"] is not None], key=lambda b: b["_offset"]
    )
    rows: list[dict] = []
    for i, b in enumerate(located):
        start = b["_offset"]
        end = located[i + 1]["_offset"] if i + 1 < len(located) else len(full)
        segment = full[start:end]
        qs, _ = N.parse_questions(segment)
        for j, q in enumerate(qs):
            nxt = qs[j + 1]["_off"] if j + 1 < len(qs) else len(segment)
            chunk = segment[q["_off"] : nxt]
            # Every line matching an option SHAPE, independent of the parser's
            # roman-numeral guard -- so the guard itself is auditable.
            shaped: list[str] = []
            # Option-shaped lines come in GROUPS separated by prose. More than
            # one group means the parser entered option mode early, mid-stem,
            # and everything after that point was dropped from the stem -- the
            # stem-truncation failure mode, which a correct-looking a..e option
            # set completely hides.
            groups = 0
            in_group = False
            for line in chunk.splitlines():
                if N.RE_EXPLANATION.match(line):
                    break
                om = N.RE_OPTION.match(line)
                am = N.RE_OPTION_ALT.match(line)
                if om or am:
                    shaped.append((om.group(1) if om else am.group(1).lower()))
                    if not in_group:
                        groups += 1
                        in_group = True
                    continue
                if line.strip() and not N.RE_ANSWER.match(line):
                    in_group = False
            rows.append(
                {
                    "key": b["key"],
                    "q_no": q["q_no"],
                    "raw": q["_raw_labels"],
                    "shape": q["_raw_shape"],
                    "shaped": shaped,
                    "groups": groups,
                    "stem": q["stem"],
                    "final": sorted(q["options"]),
                    "flags": q["flags"],
                    "answer": q["answer_label"],
                    "options": q["options"],
                }
            )
    return rows


def main() -> None:
    rows = collect()
    show_all = "--all" in sys.argv
    print(f"questions audited: {len(rows)}\n")

    dupes = [r for r in rows if len(set(r["raw"])) != len(r["raw"])]
    notclean = [r for r in rows if r["raw"] and r["raw"] != CLEAN]
    suppressed = [r for r in rows if r["shaped"] != r["raw"] and r["shape"] != "numeric"]
    repaired = [
        r
        for r in rows
        if any(
            f.startswith("options_") or f == "empty_option_dropped" or f == "stray_option_dropped"
            for f in r["flags"]
        )
    ]

    def dump(title: str, sel: list[dict]) -> None:
        print(f"== {title}: {len(sel)}")
        for r in sel:
            print(
                f"   {r['key']:26} Q{str(r['q_no']):<4} printed={r['raw']} "
                f"shape={r['shape']} -> final={r['final']} ans={r['answer']} {r['flags']}"
            )
        print()

    dump("A. duplicate label inside one option run", dupes)
    dump("B. printed labels not a clean ascending a..e run", notclean)
    dump("C. lines matching an option shape that the parser did NOT take", suppressed)
    dump("D. questions a repair path touched", repaired)

    split_groups = [r for r in rows if r["groups"] > 1]
    print(f"== E. option-shaped lines in MORE THAN ONE group (stem-truncation risk): "
          f"{len(split_groups)}")
    for r in split_groups:
        print(f"   {r['key']:26} Q{str(r['q_no']):<4} groups={r['groups']} "
              f"printed={r['raw']} shaped={r['shaped']}")
        print(f"       stem: {r['stem'][:160]}")
    print()

    resequenced = [r for r in rows if "options_resequenced_from_columns" in r["flags"]]
    print(f"== label-restart (two-column) repair path: {len(resequenced)}")
    for r in resequenced:
        print(f"   {r['key']} Q{r['q_no']}  printed {r['raw']}  answer ({r['answer']})")
        for k, v in sorted(r["options"].items()):
            mark = " <-- answer" if k == r["answer"] else ""
            print(f"       ({k}) {v}{mark}")
    print()

    if show_all:
        dump("E. every question with a clean a..e run", [r for r in rows if r["raw"] == CLEAN])
    else:
        print(f"clean a..e runs (not listed; pass --all): "
              f"{len([r for r in rows if r['raw'] == CLEAN])}")
        print(f"no option lines at all: {len([r for r in rows if not r['raw']])}")


if __name__ == "__main__":
    main()
