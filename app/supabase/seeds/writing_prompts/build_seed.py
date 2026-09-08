#!/usr/bin/env python3
"""Build the English writing-prompt bank seed as Content Studio bulk-import JSON.

The seed is authored here as curated content and emitted as per-category
``{reason, subject_id, rows:[...]}`` payloads that the Content Studio **Bulk
Import** tab (POST /api/admin/content-studio/writing-prompts/bulk) consumes.
Every prompt therefore lands ``reviewer_status='pending'`` / ``is_active=false``
through the audited ``cms_bulk_upsert_writing_prompts`` RPC — it must pass the
reviewer lifecycle to reach ``verified`` (governance: verified-only reads). We do
NOT insert rows with raw SQL; that would bypass the review lifecycle + audit.

IDs are the deterministic ones migration 205 assigns
(``md5('ewp:subject:english-language')`` / ``md5('ewp:topic:<slug>')`` /
``md5('ewp:microtopic:<slug>')``), so the emitted UUIDs resolve against any
205-seeded database without a slug-resolution step.

Run:  python3 build_seed.py    (writes *.json next to this file + prints counts)

Targets (career-copilot-checklist.md "Prompt bank seed", 270 total):
  50 sentence-construction · 50 sentence-correction · 100 grammar ·
  50 vocabulary · 20 paragraph.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent

# --- deterministic id resolution (mirrors migration 205) --------------------

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

SUBJECT_ID = _uuid("ewp:subject:english-language")
TOPIC = {s: LIVE_TOPIC_ID.get(s, _uuid(f"ewp:topic:{s}")) for s in (
    "sentence-construction", "grammar", "vocabulary-in-context", "paragraph-writing",
)}
MICRO = lambda s: _uuid(f"ewp:microtopic:{s}")

# --- backend-parity validation ---------------------------------------------
# _canonicalize_required_words: each entry NFC+trim, exactly one token of the
# deterministic tokenizer, unique case-insensitively.
_WORD_RE = re.compile(r"[^\W_]+(?:['\-][^\W_]+)*", re.UNICODE)


def _valid_required_word(entry: str) -> bool:
    e = unicodedata.normalize("NFC", entry).strip()
    toks = _WORD_RE.findall(e)
    return len(toks) == 1 and toks[0] == e


_seen_keys: set[str] = set()


def row(external_key, exercise_type, topic_slug, prompt_text, *, micro=None,
        difficulty=3, source_text=None, required_words=None, min_words=None,
        max_words=None, required_sentence_count=None):
    assert external_key not in _seen_keys, f"duplicate external_key {external_key}"
    _seen_keys.add(external_key)
    assert 1 <= difficulty <= 10, f"{external_key}: difficulty {difficulty}"
    assert prompt_text.strip(), f"{external_key}: blank prompt_text"
    r = {
        "external_key": external_key,
        "exercise_type": exercise_type,
        "topic_id": TOPIC[topic_slug],
        "prompt_text": prompt_text,
        "difficulty_level": difficulty,
    }
    if micro:
        r["microtopic_id"] = MICRO(micro)
    if source_text:
        r["source_text"] = source_text
    if required_words:
        for w in required_words:
            assert _valid_required_word(w), f"{external_key}: bad required word {w!r}"
        r["required_words"] = required_words
    if required_sentence_count is not None:
        r["required_sentence_count"] = required_sentence_count
    if min_words is not None:
        r["min_words"] = min_words
    if max_words is not None:
        r["max_words"] = max_words
        assert min_words is None or max_words >= min_words, f"{external_key}: max<min"
    return r


# ===========================================================================
# 1. SENTENCE CONSTRUCTION (50) — build a sentence of a given type using words
# ===========================================================================
# (micro, [required_words], sentence_kind label, difficulty)
_CONSTRUCTION = [
    ("simple-sentences", ["diligent"], "simple", 2),
    ("simple-sentences", ["harvest"], "simple", 2),
    ("simple-sentences", ["punctual"], "simple", 2),
    ("simple-sentences", ["scarce"], "simple", 2),
    ("simple-sentences", ["migrate"], "simple", 2),
    ("simple-sentences", ["fragile"], "simple", 2),
    ("simple-sentences", ["vivid"], "simple", 3),
    ("simple-sentences", ["ancient"], "simple", 2),
    ("simple-sentences", ["reluctant"], "simple", 3),
    ("simple-sentences", ["generous"], "simple", 2),
    ("compound-sentences", ["however", "budget"], "compound", 4),
    ("compound-sentences", ["therefore", "deadline"], "compound", 4),
    ("compound-sentences", ["moreover", "committee"], "compound", 4),
    ("compound-sentences", ["nevertheless", "forecast"], "compound", 5),
    ("compound-sentences", ["furthermore", "proposal"], "compound", 4),
    ("compound-sentences", ["meanwhile", "corridor"], "compound", 4),
    ("compound-sentences", ["otherwise", "penalty"], "compound", 4),
    ("compound-sentences", ["consequently", "shortage"], "compound", 5),
    ("compound-sentences", ["nonetheless", "verdict"], "compound", 5),
    ("compound-sentences", ["likewise", "colleague"], "compound", 4),
    ("complex-sentences", ["although", "persevere"], "complex", 5),
    ("complex-sentences", ["because", "flourish"], "complex", 4),
    ("complex-sentences", ["whenever", "commute"], "complex", 4),
    ("complex-sentences", ["unless", "renew"], "complex", 4),
    ("complex-sentences", ["whereas", "abundant"], "complex", 5),
    ("complex-sentences", ["since", "advocate"], "complex", 5),
    ("complex-sentences", ["provided", "comply"], "complex", 6),
    ("complex-sentences", ["despite", "obstacle"], "complex", 6),
    ("complex-sentences", ["wherever", "settle"], "complex", 4),
    ("complex-sentences", ["until", "accumulate"], "complex", 5),
    ("complex-sentences", ["though", "hesitate"], "complex", 5),
    ("complex-sentences", ["if", "postpone"], "complex", 4),
    # Non-inflecting required words (nouns/adjectives/adverbs), so the "keep the
    # word in its given form" instruction never contradicts the required grammar
    # (an earlier draft paired base-form verbs with passive-voice tasks — removed).
    ("complex-sentences", ["although", "diligent"], "complex", 5),
    ("compound-sentences", ["however", "appointment"], "compound", 5),
    ("simple-sentences", ["delivery"], "simple", 3),
    ("complex-sentences", ["because", "committee"], "complex", 5),
    ("compound-sentences", ["therefore", "complete"], "compound", 4),
    ("simple-sentences", ["astonished"], "simple", 4),
    ("complex-sentences", ["whenever", "insistent"], "complex", 5),
    ("compound-sentences", ["nevertheless", "warning"], "compound", 5),
    ("simple-sentences", ["horizon"], "simple", 3),
    ("simple-sentences", ["gratitude"], "simple", 3),
    ("compound-sentences", ["instead", "monsoon"], "compound", 4),
    ("compound-sentences", ["yet", "ambition"], "compound", 4),
    ("complex-sentences", ["before", "assembly"], "complex", 4),
    ("complex-sentences", ["after", "restoration"], "complex", 4),
    ("complex-sentences", ["as", "scarcity"], "complex", 5),
    ("simple-sentences", ["sturdy"], "simple", 2),
    ("simple-sentences", ["remote"], "simple", 2),
    ("compound-sentences", ["otherwise", "organiser"], "compound", 5),
]


def build_construction():
    rows = []
    for i, (micro, words, kind, diff) in enumerate(_CONSTRUCTION, 1):
        wlist = ", ".join(f'"{w}"' for w in words)
        prompt = (
            f"Write a grammatically correct {kind} sentence that uses the word"
            f"{'s' if len(words) > 1 else ''} {wlist}. "
            "Keep every required word in its given form."
        )
        rows.append(row(
            f"ewp-seed-scon-{i:03d}", "sentence_construction", "sentence-construction",
            prompt, micro=micro, difficulty=diff, required_words=words,
            min_words=6, max_words=40,
        ))
    return rows


# ===========================================================================
# 2. SENTENCE CORRECTION (50) — fix a structural error (fragments/run-ons/…)
# ===========================================================================
# (micro, faulty_sentence, difficulty, short_label)
_CORRECTION = [
    ("sentence-structure", "Because the train was late.", 3, "sentence fragment"),
    ("sentence-structure", "Running across the busy platform with a heavy bag.", 3, "sentence fragment"),
    ("sentence-structure", "Which is the reason we cancelled the trip.", 4, "sentence fragment"),
    ("sentence-structure", "The old library near the river, a place full of quiet corners.", 4, "sentence fragment"),
    ("sentence-structure", "After she had finished her homework and cleared the table.", 3, "sentence fragment"),
    ("sentence-structure", "To improve his marks in the final examination.", 3, "sentence fragment"),
    ("sentence-structure", "The results were declared yesterday everyone was thrilled.", 4, "run-on sentence"),
    ("sentence-structure", "I woke up early I still missed the bus.", 3, "run-on sentence"),
    ("sentence-structure", "The rain stopped the children rushed outside to play.", 3, "run-on sentence"),
    ("sentence-structure", "She loves painting she has never taken a formal class.", 4, "run-on sentence"),
    ("sentence-structure", "He studied all night therefore he was too tired to write the test.", 4, "run-on sentence"),
    ("sentence-structure", "The shop was closed, we went home disappointed.", 3, "comma splice"),
    ("sentence-structure", "It was raining heavily, the match was postponed.", 3, "comma splice"),
    ("sentence-structure", "The plan sounded simple, it turned out to be difficult.", 4, "comma splice"),
    ("sentence-structure", "She is talented, she works harder than anyone else.", 4, "comma splice"),
    ("sentence-transformation", "The manager likes reading, to travel, and painting.", 5, "faulty parallelism"),
    ("sentence-transformation", "He is not only clever but also works hard.", 5, "faulty parallelism"),
    ("sentence-transformation", "The course teaches you to plan, organising, and to lead.", 5, "faulty parallelism"),
    ("sentence-transformation", "She would rather write than speaking in public.", 5, "faulty parallelism"),
    ("sentence-transformation", "The report was clear, detailed, and it had accuracy.", 5, "faulty parallelism"),
    ("sentence-structure", "There is many people waiting outside the office.", 3, "structure"),
    ("sentence-structure", "The reason he failed is because he did not revise.", 5, "redundant structure"),
    ("sentence-structure", "Being a hot afternoon, the workers took a long break.", 5, "dangling structure"),
    ("sentence-structure", "The more you practise, you become more confident.", 5, "faulty correlative"),
    ("sentence-structure", "Hardly had I sat down when the phone rang, and then it stopped and rang again continuously without any pause.", 5, "run-on sentence"),
    ("sentence-structure", "My brother he is studying to be an engineer.", 3, "double subject"),
    ("sentence-structure", "The book what I borrowed was very useful.", 4, "structure"),
    ("sentence-structure", "This is one of those problems that has no easy answer, and which many people they struggle with.", 5, "structure"),
    ("sentence-structure", "Walking to school, the rain soaked my uniform.", 4, "dangling modifier"),
    ("sentence-structure", "The teacher explained the lesson very clear.", 3, "structure"),
    ("sentence-transformation", "Neither the coach nor the players was happy with the result.", 4, "structure"),
    ("sentence-structure", "He asked me that where I was going.", 4, "reported-speech structure"),
    ("sentence-structure", "I did not knew the answer to the question.", 3, "structure"),
    ("sentence-structure", "Despite of the traffic, we reached on time.", 3, "structure"),
    ("sentence-structure", "The committee were divided over the new rule.", 4, "structure"),
    ("sentence-structure", "The list of names have been posted on the board.", 3, "structure"),
    ("sentence-structure", "Having finished the report, the printer broke down.", 5, "dangling modifier"),
    ("sentence-structure", "The scenery of the hills were breathtaking.", 3, "structure"),
    ("sentence-structure", "Each of the volunteers were given a badge.", 3, "structure"),
    ("sentence-structure", "Not only she sings well but she also dances.", 5, "inversion structure"),
    ("sentence-structure", "Seldom I have seen such a beautiful sunrise.", 5, "inversion structure"),
    ("sentence-structure", "The teacher along with her students are attending the fair.", 4, "structure"),
    ("sentence-structure", "No sooner the bell rang than the students left.", 4, "structure"),
    ("sentence-structure", "He is senior than me by three years.", 3, "structure"),
    ("sentence-structure", "She helped to carry the boxes and arranging the chairs.", 4, "faulty parallelism"),
    ("sentence-structure", "Hardly ever he visits his old village now.", 4, "inversion structure"),
    ("sentence-structure", "She cannot able to attend the meeting today.", 3, "structure"),
    ("sentence-structure", "The scissors is kept in the top drawer.", 3, "structure"),
    ("sentence-structure", "Whoever finishes first, they may leave the hall.", 4, "structure"),
    ("sentence-structure", "The instructions was printed on the back of the sheet.", 3, "structure"),
]


def build_correction():
    rows = []
    for i, (micro, bad, diff, label) in enumerate(_CORRECTION, 1):
        prompt = (
            "Rewrite the sentence below as one grammatically correct, well-formed "
            f"sentence. It contains a {label}. Do not change the intended meaning."
        )
        rows.append(row(
            f"ewp-seed-scor-{i:03d}", "sentence_correction", "sentence-construction",
            prompt, micro=micro, difficulty=diff, source_text=bad,
        ))
    return rows


# ===========================================================================
# 3. GRAMMAR (100) — fix a specific grammatical error, by microtopic
# ===========================================================================
# micro -> list of (faulty_sentence, difficulty)
_GRAMMAR = {
    "subject-verb-agreement": [
        ("The list of items are on the desk.", 3),
        ("Each of the players have a locker in the room.", 3),
        ("Neither the teacher nor the students was ready for the test.", 4),
        ("There is many reasons to celebrate today.", 3),
        ("One of my friends live in Canada.", 3),
        ("The quality of the roads have improved a lot.", 3),
        ("Mathematics are my favourite subject.", 3),
        ("The number of applicants were higher this year.", 4),
        ("A pair of scissors were lying on the table.", 4),
        ("Everybody in the class have submitted the form.", 3),
        ("The news about the floods were alarming.", 4),
        ("Ten kilometres are a long way to walk.", 4),
        ("Bread and butter are all he eats for breakfast.", 5),
        ("My trousers is torn at the knee.", 3),
    ],
    "tense": [
        ("She has finished her lunch an hour ago.", 4),
        ("I am knowing the answer to this question.", 3),
        ("When I reached the station, the train already left.", 4),
        ("He said that he will call me the next day.", 4),
        ("They are living here since 2015.", 4),
        ("If I would have known, I would have helped you.", 5),
        ("By next month, I will complete two years at this office.", 5),
        ("She was used to walk to school every morning.", 4),
        ("I have seen that film yesterday.", 3),
        ("While he cooked, the phone was ringing twice.", 4),
        ("The teacher told us that water boiled at 100 degrees.", 5),
        ("He is having a car and a motorbike.", 3),
        ("No sooner had I left than it starts to rain.", 5),
        ("They were playing cricket since morning.", 4),
    ],
    "articles": [
        ("She is honest girl who never lies.", 3),
        ("He wants to become a engineer.", 3),
        ("The Mount Everest is the highest peak in the world.", 3),
        ("I bought an university sweatshirt yesterday.", 4),
        ("She plays the football every evening.", 3),
        ("He was sent to the prison for the theft.", 4),
        ("Sun rises in the east.", 3),
        ("She is best singer in our school.", 3),
        ("We had a breakfast at eight o'clock.", 3),
        ("He is an one-eyed man with a kind face.", 4),
        ("A honest officer refused the bribe.", 3),
        ("I met him a hour before the show.", 3),
    ],
    "prepositions": [
        ("She is married with a doctor.", 3),
        ("He has been ill since three days.", 3),
        ("I am good in mathematics.", 3),
        ("The cat jumped in the wall.", 3),
        ("He was accused for stealing the money.", 4),
        ("She is senior to me by two years, but reports directly under our manager.", 4),
        ("We discussed about the plan for an hour.", 4),
        ("He is fond for sweets and chocolate.", 3),
        ("The train will arrive at five minutes.", 3),
        ("She has an advantage above the other players.", 4),
        ("They reached to the airport in time.", 3),
        ("He apologised for me for the delay.", 4),
    ],
    "pronoun-reference": [
        ("Between you and I, the plan will not work.", 4),
        ("The manager praised John and myself for the report.", 4),
        ("She is taller than me, though I am older than her.", 4),
        ("Give the tickets to whoever wants them, but not to he.", 4),
        ("Me and my friend went to the fair together.", 3),
        ("The dog wagged its tail when it saw it's owner.", 4),
        ("Us students should support the new library rule.", 3),
        ("This is a secret between she and her sister.", 4),
        ("Him and I finished the project a day early.", 3),
        ("The prize was shared between she and me.", 4),
    ],
    "modifiers": [
        ("Running down the street, my hat blew away.", 4),
        ("She almost drove the children to school every day.", 4),
        ("He only eats vegetables on Mondays and nothing else.", 4),
        ("Covered in mud, the coach scolded the players.", 4),
        ("I saw a puppy walking to the market.", 4),
        ("To write well, practice is essential every day.", 5),
        ("The waiter served a steak to the guest that was overcooked.", 4),
        ("Barely two years old, my father taught me to swim.", 5),
        ("She served sandwiches to the children on paper plates that were half eaten.", 5),
        ("Hoping to win, the trophy seemed within his reach.", 5),
    ],
    "punctuation": [
        ("Lets eat grandma before the food gets cold.", 3),
        ("The teacher said the exam is on friday.", 3),
        ("Its raining again and the dog lost it's collar.", 4),
        ("Where are you going she asked.", 3),
        ("We bought apples oranges and bananas from the market.", 3),
        ("My sister who lives in Delhi is a doctor.", 4),
        ("He shouted stop the bus is leaving.", 4),
        ("The childrens toys were scattered across the floor.", 4),
        ("I have three hobbies reading painting and cycling.", 4),
        ("She replied yes I will certainly come.", 3),
        ("Its a long way to the top isnt it.", 4),
        ("The mens room is at the end of the corridor.", 4),
        ("He asked, whether the shop was open?", 4),
        ("Although it was late; we decided to continue.", 4),
    ],
    "spelling": [
        ("He recieved the parcel on Monday morning.", 3),
        ("It was a beautifull sunset over the sea.", 2),
        ("The government made an important annoucement.", 3),
        ("She was embarassed by the sudden question.", 4),
        ("They faced a lot of dificulties on the trek.", 3),
        ("The commitee will meet again next week.", 3),
        ("He has a good knowlege of history.", 3),
        ("Please seperate the waste before disposal.", 3),
        ("The medicine had no noticable side effect.", 4),
        ("We must maintain proper disipline in class.", 3),
        ("The wether was pleasant throughout the trip.", 2),
        ("Her arguement was clear and convincing.", 3),
        ("He signed the form in the space provieded.", 3),
        ("The manager gave a breif reply to the query.", 3),
    ],
}


def build_grammar():
    labels = {
        "subject-verb-agreement": "subject–verb agreement error",
        "tense": "tense error",
        "articles": "article error",
        "prepositions": "preposition error",
        "pronoun-reference": "pronoun / case error",
        "modifiers": "misplaced or dangling modifier",
        "punctuation": "punctuation error",
        "spelling": "spelling error",
    }
    rows = []
    n = 0
    for micro, items in _GRAMMAR.items():
        for bad, diff in items:
            n += 1
            prompt = (
                f"Correct the {labels[micro]} in the sentence below and rewrite it. "
                "Change only what is necessary; keep the meaning the same."
            )
            rows.append(row(
                f"ewp-seed-gram-{n:03d}", "sentence_correction", "grammar",
                prompt, micro=micro, difficulty=diff, source_text=bad,
            ))
    return rows


# ===========================================================================
# 4. VOCABULARY IN CONTEXT (50)
# ===========================================================================
# micro -> list of (source_sentence_or_none, instruction, difficulty, required_words?)
_VOCAB = {
    "word-choice": [
        ("The medicine had an adverse affect on his health.", "Replace the incorrectly chosen word with the right one.", 3, None),
        ("The lawyer will council her client before the trial.", "Replace the incorrectly chosen word with the right one.", 4, None),
        ("Please accept my apology; I did not mean to loose your book.", "Replace the incorrectly chosen word with the right one.", 3, None),
        ("The two countries signed a peace treaty to avoid further conflict, which had a profound principle on the region.", "Replace the incorrectly chosen word with the right one.", 4, None),
        ("He gave me a complement on my new haircut.", "Replace the incorrectly chosen word with the right one.", 3, None),
        ("The weather had a bad effect on the crops, so farmers had to adopt to the change.", "Replace the incorrectly chosen word with the right one.", 4, None),
        (None, 'Use the word "meticulous" correctly in a sentence about someone\'s work.', 4, ["meticulous"]),
        (None, 'Use the word "inevitable" correctly in a sentence.', 4, ["inevitable"]),
        (None, 'Use the word "deteriorate" correctly in a sentence.', 4, ["deteriorate"]),
        (None, 'Use the word "prudent" correctly in a sentence about a decision.', 5, ["prudent"]),
        (None, 'Use the word "abundant" correctly in a sentence.', 3, ["abundant"]),
        (None, 'Use the word "reluctant" correctly in a sentence.', 3, ["reluctant"]),
        (None, 'Use the word "diligent" correctly in a sentence about a student.', 3, ["diligent"]),
    ],
    "collocations": [
        ("She made a big mistake by ignoring the warning signs.", "Replace the underlined-style verb collocation error if any, or rewrite using the natural collocation for 'mistake'.", 3, None),
        ("He did a strong effort to finish the project on time.", "Correct the verb collocation.", 3, None),
        ("He did a mistake while copying the figures.", "Correct the verb collocation.", 3, None),
        ("Please do me a favour and pass the salt.", "This collocation may be correct; if it is, keep it and add a second sentence using 'make a decision' correctly.", 4, None),
        ("The scientist made an important discovery about the virus.", "If the collocation is correct, keep it; otherwise correct it.", 3, None),
        (None, 'Write one sentence using the collocation "keen interest".', 4, ["keen"]),
        (None, 'Write one sentence using the collocation "heavy rain".', 3, ["heavy"]),
        (None, 'Write one sentence using the collocation "make progress".', 3, ["progress"]),
        (None, 'Write one sentence using the collocation "pay attention".', 3, ["attention"]),
        (None, 'Write one sentence using the collocation "bitterly disappointed".', 5, ["bitterly"]),
        (None, 'Write one sentence using the collocation "strong argument".', 4, ["argument"]),
        (None, 'Write one sentence using the collocation "meet a deadline".', 4, ["deadline"]),
    ],
    "formal-vocabulary": [
        ("The manager wants to get rid of the old rules.", "Rewrite the sentence in formal English, replacing the informal phrase.", 4, None),
        ("The report says the plan is a big deal for the company.", "Rewrite in formal English.", 4, None),
        ("We need to figure out what went wrong.", "Rewrite in formal English.", 4, None),
        ("The kids were told to shut up during the exam.", "Rewrite in formal English.", 3, None),
        ("The boss was okay with the new timings.", "Rewrite in formal English.", 3, None),
        ("They put off the meeting because of the rain.", "Rewrite in formal English.", 4, None),
        ("A lot of people showed up for the event.", "Rewrite in formal English.", 3, None),
        ("The company is going to look into the complaint.", "Rewrite in formal English.", 4, None),
        ("The plan fell through.", "Rewrite in formal English.", 5, None),
        ("He wants to get the job done quickly.", "Rewrite in formal English.", 3, None),
        ("The staff were fed up with the constant changes.", "Rewrite in formal English.", 4, None),
        ("Let's touch base after lunch.", "Rewrite in formal English.", 4, None),
    ],
    "redundancy": [
        ("She returned back the book to the library.", "Remove the redundant word(s) and rewrite the sentence.", 3, None),
        ("The two twins looked exactly alike in every way.", "Remove the redundancy and rewrite.", 3, None),
        ("Please repeat that again for the class.", "Remove the redundancy and rewrite.", 3, None),
        ("We must plan ahead for the future carefully.", "Remove the redundancy and rewrite.", 3, None),
        ("At this moment in time, the office is closed.", "Remove the wordiness and rewrite concisely.", 4, None),
        ("He is a new beginner who just started last week.", "Remove the redundancy and rewrite.", 3, None),
        ("The final outcome of the match was a draw.", "Remove the redundancy and rewrite.", 3, None),
        ("She combined together all the ingredients in the bowl.", "Remove the redundancy and rewrite.", 3, None),
        ("In my personal opinion, I think the plan is good.", "Remove the redundancy and rewrite concisely.", 4, None),
        ("They gathered together in the hall for the meeting.", "Remove the redundancy and rewrite.", 3, None),
        ("The reason why he left is because he was unwell.", "Remove the wordiness and rewrite concisely.", 4, None),
        ("Each and every one of the students passed the test.", "Remove the redundancy and rewrite.", 3, None),
        ("It is a true fact that the earth orbits the sun.", "Remove the redundancy and rewrite.", 3, None),
    ],
}


def build_vocab():
    rows = []
    n = 0
    for micro, items in _VOCAB.items():
        for src, instr, diff, words in items:
            n += 1
            rows.append(row(
                f"ewp-seed-vocab-{n:03d}", "vocabulary_in_context", "vocabulary-in-context",
                instr, micro=micro, difficulty=diff, source_text=src,
                required_words=words, min_words=6 if words else None,
                max_words=40 if words else None,
            ))
    return rows


# ===========================================================================
# 5. PARAGRAPH WRITING (20)
# ===========================================================================
# (micro, task, min_words, max_words, difficulty)
_PARAGRAPH = [
    ("topic-sentence", "Write a paragraph describing your favourite season and why you enjoy it. Begin with a clear topic sentence.", 60, 100, 3),
    ("topic-sentence", "Write a paragraph explaining the benefits of reading books. Start with a strong topic sentence.", 70, 110, 3),
    ("cohesion", "Write a paragraph about the importance of clean drinking water, using linking words to connect your ideas.", 70, 120, 4),
    ("cohesion", "Write a paragraph describing a typical morning in your household, using appropriate connectors to show sequence.", 70, 120, 4),
    ("logical-order", "Explain, in one paragraph, the steps you would take to prepare for an important examination. Present the steps in a logical order.", 80, 130, 4),
    ("logical-order", "Describe how to plant a small kitchen garden, presenting the stages in a sensible order.", 80, 130, 4),
    ("conclusion", "Write a paragraph on whether students should be given homework, and end with a clear concluding sentence.", 90, 140, 5),
    ("conclusion", "Write a paragraph about the advantages and disadvantages of mobile phones, closing with a balanced conclusion.", 90, 150, 5),
    ("topic-sentence", "Describe a place you would like to visit and explain why. Open with a topic sentence that states the place.", 70, 110, 3),
    ("cohesion", "Write a paragraph about the role of trees in a city, ensuring your sentences flow smoothly from one to the next.", 80, 120, 4),
    ("logical-order", "Narrate a memorable day of your life, keeping the events in the order they happened.", 90, 140, 4),
    ("conclusion", "Argue whether public transport is better than private vehicles, and finish with a firm concluding statement.", 100, 150, 6),
    ("topic-sentence", "Write a paragraph about a person you admire, beginning with a topic sentence that names the person and the main quality.", 70, 110, 3),
    ("cohesion", "Explain why regular exercise is important, linking each idea clearly to the next.", 80, 120, 4),
    ("logical-order", "Describe the process of making a cup of tea, in the correct sequence of steps.", 60, 100, 3),
    ("conclusion", "Discuss whether examinations are the best way to judge a student, and end with your own reasoned conclusion.", 100, 150, 6),
    ("topic-sentence", "Write a paragraph about the importance of saving money, opening with a clear topic sentence.", 70, 110, 4),
    ("cohesion", "Describe your neighbourhood, using connectors to move smoothly between details.", 70, 120, 4),
    ("logical-order", "Explain how to stay safe during heavy monsoon rains, presenting the advice in a logical order.", 80, 130, 5),
    ("conclusion", "Write a paragraph on whether social media does more good than harm, closing with a balanced conclusion.", 100, 150, 6),
]


def build_paragraph():
    rows = []
    for i, (micro, task, mn, mx, diff) in enumerate(_PARAGRAPH, 1):
        rows.append(row(
            f"ewp-seed-para-{i:03d}", "paragraph_writing", "paragraph-writing",
            task, micro=micro, difficulty=diff, min_words=mn, max_words=mx,
        ))
    return rows


# ===========================================================================

# (filename, target_count, builder). The committed .json files are plain ROW
# ARRAYS — the exact shape the Content Studio Bulk Import UI parses
# (PromptBulkImport.jsx: `Array.isArray(data) ? data : [data]`). The operator
# supplies `subject_id` and `reason` in the form fields; for a direct `curl` to
# the API, wrap a file into the `{reason, subject_id, rows}` envelope with
# to_api_envelope.py.
BATCHES = [
    ("01_sentence_construction.json", 50, build_construction),
    ("02_sentence_correction.json", 50, build_correction),
    ("03_grammar.json", 100, build_grammar),
    ("04_vocabulary.json", 50, build_vocab),
    ("05_paragraph.json", 20, build_paragraph),
]


def main():
    total = 0
    summary = []
    for fname, target, builder in BATCHES:
        rows = builder()
        (HERE / fname).write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
        flag = "" if len(rows) == target else f"  (target {target})"
        summary.append(f"  {fname}: {len(rows)} rows{flag}")
        total += len(rows)
    print("Wrote Content Studio bulk-import seed batches (row arrays for UI upload):")
    print("\n".join(summary))
    print(f"  TOTAL: {total} prompts (target 270)")
    print(f"  subject_id to enter in the Bulk Import form: {SUBJECT_ID}")
    print("  (verify/resolve topic + subject IDs against the target DB first — see README)")


if __name__ == "__main__":
    main()
