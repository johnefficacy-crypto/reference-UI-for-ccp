#!/usr/bin/env python3
"""Extract the NABARD Grade A PYQ compendium into a structured block file.

Source : docs/reference/pyq/NABARD-Grade-A-PYQ.pdf
Output : workbench/nabard_blocks.json

Segmentation is heading-driven and closed: the table of contents (PDF pages
2-3) gives every heading and its printed page, each heading is then located in
the body, and a block is exactly the span between its own heading and the next
one. Nothing lives outside a block, so content absorbed past a heading shows up
as one block long and the next short by the same amount rather than as silent
loss -- see the reconciliation printed by ``--report``.

Usage:
    python3 workbench/nabard_extract.py            # extract + write JSON
    python3 workbench/nabard_extract.py --report   # extract + full reconciliation
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / "docs/reference/pyq/NABARD-Grade-A-PYQ.pdf"
OUT = ROOT / "workbench/nabard_blocks.json"

# The compendium prints "N | P a g e" where N is one less than the PDF page
# index, so a TOC page number maps to PDF page number + 1.
TOC_PAGES = (2, 3)
PRINTED_TO_PDF_OFFSET = 1

# Question markers. "I.2)" occurs once where the source mis-set "Q" as "I";
# it is accepted at line start only, so option text like "I & II" cannot match.
# Two question-marker shapes occur: "Q.1)" / "Q.1." and, in the 2023 blocks,
# a bare "Q.1 " with no punctuation. The bare form must not swallow the
# "Q.1 to Q.20" range text that appears inside headings and instruction lines.
# A stray leading "." occasionally precedes the marker in the text layer
# (".Q.6)"), and "-" is used as the separator in a few 2023 items ("Q. 18-").
# The digit guard applies only to the dash separators, which is where the
# "Q.121-160" ranges arise; ")" may legitimately be followed by a digit when a
# stem opens with a number ("Q.95)8, ?, 45, ...").
RE_QMARK = re.compile(
    r"^[ \t]*\.?[ \t]*(Q|I)[ \t]*\.[ \t]*(\d{1,3})[ \t]*(?:[).:]|[-–—](?!\d))", re.M
)
RE_QMARK_SPACED = re.compile(
    r"^[ \t]*\.?[ \t]*(Q)[ \t]*(\d{1,3})[ \t]*(?:[).:]|[-–—](?!\d))", re.M
)
RE_QMARK_BARE = re.compile(
    r"^[ \t]*\.?[ \t]*(Q)[ \t]*\.[ \t]*(\d{1,3})[ \t]+(?![Tt]o\b|[-–—]|\d)", re.M
)
# "I.<n>)" is the compendium's context marker, not a question: it introduces the
# Directions block or the stimulus paragraph that the identically-numbered
# "Q.<n>)" then asks about. It is captured as context and never counted as a
# question, which is what keeps every Phase 2 block on its printed range.
# Options are printed three ways: "(a) ...", "[a] ..." and, in the 2023 blocks,
# "A. ...".
# a-j, not a-e: one item prints its five options as (f)-(j) while its answer
# still reads (a). Capturing them is what turns that into a visible
# answer-label mismatch instead of a silently option-less question.
RE_OPTION = re.compile(r"^[ \t]*[(\[]([a-j])[)\]][ \t]*(.*)$")
RE_OPTION_ALT = re.compile(r"^[ \t]*([A-E])[.)][ \t]+(.*)$")
# A third option shape: "1) ...", "2) ..." on their own lines, used by parts of
# the 2020 ESI block. It is a LAST RESORT only -- accepted when neither the paren
# nor the "A." shape produced a set, and only for a COMPLETE consecutive run
# starting at 1 (>= 4 members). That guard matters: many stems number their
# STATEMENTS "1) ... 2) ..." and then print real "(a)-(e)" options underneath
# (1|ESI|2020 Q.139 does exactly this), and those must keep the paren options.
RE_OPTION_NUM = re.compile(r"^[ \t]*([1-5])\)[ \t]+(.*)$")
_NUM_TO_ALPHA = {"1": "a", "2": "b", "3": "c", "4": "d", "5": "e"}
# "Answer" and "Solution" are both used, with -, –, — or : as the separator.
RE_ANSWER = re.compile(
    r"^[ \t]*(?:answer|solution)[ \t]*[-–—:.]*[ \t]*\(?\s*([a-e])\s*\)?",
    re.I,
)
RE_EXPLANATION = re.compile(r"^[ \t]*explanation[ \t]*[-–—:]", re.I)

# Ranges as printed in headings: (Q.1 to Q.20) / (1 to 20) / ( 101 - 120) / (1-20)
RE_RANGE = re.compile(
    r"\(?\s*(?:Q\s*\.?\s*)?(\d{1,3})\s*(?:to|To|TO|[-–—])\s*(?:Q\s*\.?\s*)?(\d{1,3})\s*\)?"
)
RE_YEAR = re.compile(r"(20[12]\d)")

# --- Anchored source repairs -------------------------------------------------
# The text layer drops the "Q." prefix on one marker: 1|English|2023 prints its
# 24th item as a bare "24. Which of the following ..." (verified on PDF page 127).
# Accepting a bare "^\d+\." marker globally is not safe -- statement numbering
# and list items would match -- so the marker is restored by an EXACT anchored
# substitution instead, asserted to hit exactly once so a changed source fails
# loudly rather than silently no-opping.
# Each entry is (line-anchored pattern, replacement, expected hit count).
TEXT_REPAIRS: list[tuple[str, str, int]] = [
    (
        r"(?m)^24\. Which of the following statements is incorrect as per the context",
        "Q.24. Which of the following statements is incorrect as per the context",
        1,
    ),
]

# --- Blocks printed under one heading that hold two shifts -------------------
# 1|Reasoning|2022 prints a single "(Q.1 to Q.20)" heading but contains two
# complete 1..20 runs -- every other 2022 subject prints separate Morning/
# Evening headings. Split on the numbering restart into the same
# "<key>|morning" / "<key>|evening" shape the rest of the file uses.
# The shift NAMES are an inference from document order (the source prints no
# shift label here); 4 of the 5 other 2022 subjects print Morning first.
SPLIT_ON_RESTART = {"1|Reasoning|2022": ("morning", "evening")}

PHASE1_SUBJECTS = [
    "Reasoning",
    "English",
    "Computer Knowledge",
    "Quantitative Aptitude",
    "General Awareness",
    "Economic and Social Issues",
    "Agriculture Rural Development",
]
SUBJECT_CANON = {
    "Reasoning": "Reasoning",
    "English": "English",
    "Computer Knowledge": "Computer Knowledge",
    "Quantitative Aptitude": "Quantitative Aptitude",
    "Decision Making": "Decision Making",
    "General Awareness": "General Awareness",
    "Economic and Social Issues": "ESI",
    "Economic and Social Issue": "ESI",
    "Agriculture Rural Development": "ARD",
    "ESI/ARD": "ESI/ARD",
}


def read_pages() -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(str(PDF))
    pages = [(page.extract_text() or "") for page in reader.pages]
    return apply_text_repairs(pages)


def apply_text_repairs(pages: list[str]) -> list[str]:
    """Apply the anchored TEXT_REPAIRS to the extracted page text.

    Repairs run BEFORE heading location and segmentation so every character
    offset downstream is computed against the repaired text. Each repair must
    hit its declared number of times across the whole document -- a miss raises
    rather than silently leaving the defect in place.
    """
    for pattern, replacement, expected in TEXT_REPAIRS:
        hits = 0
        for i, text in enumerate(pages):
            pages[i], n = re.subn(pattern, replacement, text)
            hits += n
        if hits != expected:
            raise SystemExit(
                f"text repair {pattern!r} hit {hits} time(s), expected {expected} "
                "-- the source PDF changed; re-verify the repair before extracting"
            )
    return pages


def parse_toc(pages: list[str]) -> list[dict]:
    """Return the TOC entries in printed order: label, printed page, kind."""
    entries: list[dict] = []
    for pno in TOC_PAGES:
        for raw in pages[pno - 1].splitlines():
            line = raw.strip()
            if not line or line.startswith("Contents"):
                continue
            # "Label .......... 123"
            m = re.match(r"^(.*?)[.\s]{4,}(\d{1,3})\s*$", line)
            if not m:
                continue
            label, printed = m.group(1).strip(), int(m.group(2))
            if not label or label.lower() == "exam pattern":
                continue
            entries.append({"label": label, "printed_page": printed})
    return entries


def classify(entries: list[dict]) -> list[dict]:
    """Attach phase / subject / year / shift / expected to every TOC entry.

    Bare section names ("Reasoning", "PHASE 2") carry no questions themselves;
    they set the context that the year rows beneath them inherit.
    """
    blocks: list[dict] = []
    phase = 1
    subject: str | None = None
    for e in entries:
        label = e["label"]
        low = label.lower()
        if low.startswith("phase 1"):
            phase, subject = 1, None
            continue
        if low.startswith("phase 2"):
            phase, subject = 2, None
            continue
        if low.startswith("descriptive questionnaire"):
            phase, subject = 3, None
            continue
        # A bare Phase-1 subject header.
        if phase == 1 and label in PHASE1_SUBJECTS:
            subject = SUBJECT_CANON[label]
            continue

        subj = subject
        for name, canon in SUBJECT_CANON.items():
            if label.lower().startswith(name.lower()):
                subj = canon
                break
        if phase == 3 and "esi/ard" in low:
            subj = "ESI/ARD"

        ym = RE_YEAR.search(label)
        year = int(ym.group(1)) if ym else None

        shift = None
        if "morning" in low:
            shift = "morning"
        elif "evening" in low:
            shift = "evening"

        expected = None
        # Do not read a range out of the year itself.
        for rm in RE_RANGE.finditer(label):
            lo, hi = int(rm.group(1)), int(rm.group(2))
            if lo >= 2000 or hi >= 2000 or hi < lo:
                continue
            expected = hi - lo + 1
            break

        blocks.append(
            {
                "key": f"{phase}|{subj}|{year}" + (f"|{shift}" if shift else ""),
                "label": label,
                "phase": phase,
                "subject": subj,
                "year": year,
                "shift": shift,
                "printed_page": e["printed_page"],
                "expected_from_heading": expected,
            }
        )
    return blocks


def locate_headings(pages: list[str], blocks: list[dict]) -> list[dict]:
    """Find each heading in the body and record its absolute character offset."""
    body = []
    starts = []  # absolute offset at which each PDF page begins
    pos = 0
    for text in pages:
        starts.append(pos)
        body.append(text)
        pos += len(text) + 1
    full = "\n".join(body)

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    for b in blocks:
        pdf_page = b["printed_page"] + PRINTED_TO_PDF_OFFSET
        target = norm(b["label"])
        found = None
        # Search the stated page first, then a small window around it.
        for cand in [pdf_page] + [pdf_page + d for d in (1, -1, 2, -2, 3)]:
            if not (1 <= cand <= len(pages)):
                continue
            page_text = pages[cand - 1]
            for m in re.finditer(r"^.*$", page_text, re.M):
                if m.group(0).strip() and norm(m.group(0)) == target:
                    found = starts[cand - 1] + m.start()
                    break
            if found is not None:
                b["heading_pdf_page"] = cand
                break
        b["_offset"] = found
    return blocks, full


def _is_complete_numeric_run(num: dict[str, str]) -> bool:
    """True for a complete consecutive "1)".."N)" run with N >= 4.

    The completeness requirement is the guard against statement numbering: a
    stem that lists two or three numbered statements never satisfies it, and a
    stem that lists four or five ALSO prints real "(a)-(e)" options, which win
    because this pass only runs when nothing else produced a set.
    """
    keys = sorted(num)
    return len(keys) >= 4 and keys == [str(i) for i in range(1, len(keys) + 1)]


def _relabel_positionally(options: dict[str, str]) -> tuple[dict[str, str], bool]:
    """Re-letter a contiguous option run that does not start at "a".

    2|ARD|2021 Q.14 prints its five options as (f)-(j) while its answer key
    still reads (a). The labels are a source typo, the ORDER is intact, so the
    run is mapped positionally back onto "a".. -- which is what makes the
    printed answer resolvable. Only a contiguous run is remapped; a gapped set
    is left alone so a genuine parse defect stays visible.
    """
    keys = sorted(options)
    if not keys or keys[0] == "a":
        return options, False
    codes = [ord(k) for k in keys]
    if codes != list(range(codes[0], codes[0] + len(codes))):
        return options, False
    return {chr(ord("a") + i): options[k] for i, k in enumerate(keys)}, True


def parse_questions(segment: str) -> list[dict]:
    """Parse one block's text span into questions."""
    marks = [(m.start(), int(m.group(2)), m.group(1)) for m in RE_QMARK.finditer(segment)]
    marks += [(m.start(), int(m.group(2)), m.group(1)) for m in RE_QMARK_SPACED.finditer(segment)]
    marks += [(m.start(), int(m.group(2)), m.group(1)) for m in RE_QMARK_BARE.finditer(segment)]
    marks.sort()
    # Drop duplicate offsets produced by the patterns overlapping.
    deduped: list[tuple[int, int, str]] = []
    for off, num, kind in marks:
        if deduped and off - deduped[-1][0] < 3:
            continue
        deduped.append((off, num, kind))

    questions = []
    contexts: list[dict] = []
    for i, (off, num, kind) in enumerate(deduped):
        end = deduped[i + 1][0] if i + 1 < len(deduped) else len(segment)
        chunk = segment[off:end]
        paren: dict[str, str] = {}
        alt: dict[str, str] = {}
        numeric: dict[str, str] = {}
        answer = None
        stem_lines: list[str] = []
        in_options = False
        for line in chunk.splitlines():
            if RE_EXPLANATION.match(line):
                break
            am = RE_ANSWER.match(line)
            if am:
                answer = am.group(1).lower()
                continue
            om = RE_OPTION.match(line)
            if om:
                in_options = True
                paren[om.group(1)] = om.group(2).strip()
                continue
            am2 = RE_OPTION_ALT.match(line)
            if am2:
                alt[am2.group(1).lower()] = am2.group(2).strip()
                if len(alt) >= 2:
                    in_options = True
                continue
            nm = RE_OPTION_NUM.match(line)
            if nm:
                # Recorded but NOT treated as entering the option list: the same
                # shape numbers statements inside a stem, and a stem must keep
                # reading until real options appear. Promoted below only if
                # nothing else produced a set.
                numeric[nm.group(1)] = nm.group(2).strip()
                if not in_options:
                    stem_lines.append(line)
                continue
            if not in_options:
                stem_lines.append(line)
        # The alternate "A." shape is only trusted when it produced a real set,
        # so a stray sentence beginning "A. " cannot masquerade as an option.
        options = paren if len(paren) >= len(alt) or len(alt) < 2 else alt
        relabelled = False
        if len(options) < 2 and _is_complete_numeric_run(numeric):
            # Last resort: "1) ... 5)" WAS the option list (1|ESI|2020 Q.134/138).
            options = {_NUM_TO_ALPHA[k]: v for k, v in sorted(numeric.items())}
            # Those lines were provisionally kept as stem text; drop them again.
            stem_lines = [ln for ln in stem_lines if not RE_OPTION_NUM.match(ln)]
        elif options:
            options, relabelled = _relabel_positionally(options)
        stem = " ".join(" ".join(stem_lines).split())
        if kind == "I":
            contexts.append({"q_no": num, "text": stem})
            continue
        flags = []
        answers_in_chunk = len(
            [ln for ln in chunk.splitlines() if RE_ANSWER.match(ln)]
        )
        if answers_in_chunk > 1:
            # Two answer lines in one chunk means the source dropped a question
            # marker and two questions were read as one.
            flags.append("merged_missing_marker")
        if len(options) < 2:
            flags.append("no_options")
        if answer is None:
            flags.append("no_answer")
        if relabelled:
            flags.append("options_relabelled_positionally")
        if answer is not None and options and answer not in options:
            flags.append("answer_label_not_in_options")
        if len(stem) < 15:
            flags.append("short_stem")
        questions.append(
            {
                "q_no": num,
                "stem": stem,
                "options": options,
                "answer_label": answer,
                "flags": flags,
                # Marker offset within the segment; used by the shift split to
                # give each half a real char_span. Stripped before writing.
                "_off": off,
            }
        )
    # Attach each context paragraph to the question it introduces.
    by_no: dict[int, dict] = {}
    for q in questions:
        by_no.setdefault(q["q_no"], q)
    unattached = 0
    for c in contexts:
        target = by_no.get(c["q_no"])
        if target is None:
            unattached += 1
            continue
        target["context"] = c["text"]
    return questions, {"context_blocks": len(contexts), "context_unattached": unattached}


def extract() -> dict:
    pages = read_pages()
    blocks = classify(parse_toc(pages))
    blocks, full = locate_headings(pages, blocks)

    located = [b for b in blocks if b["_offset"] is not None]
    located.sort(key=lambda b: b["_offset"])
    for i, b in enumerate(located):
        start = b["_offset"]
        end = located[i + 1]["_offset"] if i + 1 < len(located) else len(full)
        b["questions"], stats = parse_questions(full[start:end])
        b.update(stats)
        b["parsed"] = len(b["questions"])
        b["char_span"] = [start, end]

    for b in blocks:
        if b["_offset"] is None:
            b["questions"], b["parsed"], b["char_span"] = [], 0, None
            b["context_blocks"] = b["context_unattached"] = 0

    blocks = split_shift_blocks(blocks)

    for b in blocks:
        b.pop("_offset", None)
        for q in b["questions"]:
            q.pop("_off", None)

    return {
        "source_pdf": str(PDF.relative_to(ROOT)),
        "pdf_pages": len(pages),
        "blocks": blocks,
    }


def split_shift_blocks(blocks: list[dict]) -> list[dict]:
    """Split each SPLIT_ON_RESTART block into its two shifts at the numbering
    restart, producing the "<key>|<shift>" shape the rest of the file uses.

    The restart is the split point: the first question whose number is not
    greater than its predecessor's. A block that does not restart exactly once
    raises -- the split is a claim about the source, not a best effort.
    """
    out: list[dict] = []
    for b in blocks:
        shifts = SPLIT_ON_RESTART.get(b["key"])
        if not shifts or not b["questions"]:
            out.append(b)
            continue
        qs = b["questions"]
        restarts = [i for i in range(1, len(qs)) if qs[i]["q_no"] <= qs[i - 1]["q_no"]]
        if len(restarts) != 1:
            raise SystemExit(
                f"{b['key']}: expected exactly one numbering restart to split on, "
                f"found {len(restarts)}"
            )
        cut = restarts[0]
        halves = (qs[:cut], qs[cut:])
        span_start, span_end = b["char_span"]
        boundary = span_start + halves[1][0]["_off"]
        for shift, half, (lo, hi) in zip(
            shifts, halves, ((span_start, boundary), (boundary, span_end))
        ):
            nb = dict(b)
            nb["key"] = f"{b['key']}|{shift}"
            nb["shift"] = shift
            nb["label"] = f"{b['label']} [{shift.capitalize()} Shift — inferred, "
            nb["label"] += "source prints one heading for both shifts]"
            nb["questions"] = half
            nb["parsed"] = len(half)
            nb["char_span"] = [lo, hi]
            out.append(nb)
    return out


def report(data: dict) -> None:
    blocks = data["blocks"]
    print(f"PDF pages: {data['pdf_pages']}   blocks: {len(blocks)}")
    print()
    print(f"{'key':34} {'pg':>4} {'exp':>4} {'got':>4} {'delta':>6}  label")
    tot_exp = tot_got = 0
    for b in blocks:
        exp = b["expected_from_heading"]
        got = b["parsed"]
        delta = "" if exp is None else f"{got - exp:+d}"
        tot_got += got
        if exp:
            tot_exp += exp
        mark = " " if (exp is None or got == exp) else "*"
        print(
            f"{mark}{b['key']:33} {b['printed_page']:>4} "
            f"{(exp if exp is not None else '-'):>4} {got:>4} {delta:>6}  {b['label'][:52]}"
        )
    print()
    print(f"TOTAL expected (headings with a range): {tot_exp}")
    print(f"TOTAL parsed  (all blocks)            : {tot_got}")
    rangeless = sum(b["parsed"] for b in blocks if b["expected_from_heading"] is None)
    print(f"  of which in blocks with no printed range: {rangeless}")
    print(f"  in blocks with a printed range          : {tot_got - rangeless}")
    print(f"  balance against expected                : {tot_got - rangeless - tot_exp:+d}")

    print()
    mism = [b for b in blocks if b["expected_from_heading"] not in (None, b["parsed"])]
    print(f"Blocks not matching their heading range: {len(mism)}")
    for b in mism:
        print(f"  {b['key']:33} exp {b['expected_from_heading']:>3} got {b['parsed']:>3} "
              f"({b['parsed'] - b['expected_from_heading']:+d})")

    print()
    allq = [(b, q) for b in blocks for q in b["questions"]]
    flagged = [(b, q) for b, q in allq if q["flags"]]
    print(f"Questions parsed: {len(allq)}   flagged: {len(flagged)}")
    from collections import Counter
    fc = Counter(f for _, q in flagged for f in q["flags"])
    for name, n in fc.most_common():
        print(f"  {name:32} {n}")


def main() -> None:
    data = extract()
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    print(f"wrote {OUT.relative_to(ROOT)}")
    if "--report" in sys.argv:
        print()
        report(data)


if __name__ == "__main__":
    main()
