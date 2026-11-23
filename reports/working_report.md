# ipi-shieldstral: working report

Status: **2026-11-23**. A compact view of what was built, the choices made and every deviation from the briefs, with the reason for each. The detailed reports are in `reports/` (links at the end).

**Goal.** A private Hugging Face dataset to train and benchmark Shieldstral as a guard against indirect prompt injection. It has three configs:
- `bipia`: train, validation and test;
- `nemotron`: test;
- `bipia_textfooler`: later.

## Steps at a glance

| Step | Status | What was done | Output | Deviations and decisions (why) |
|---|---|---|---|---|
| **P0** Scaffold | ✅ Done | Config (seed 42), shared 29-column `Sample` schema, `data/out` overwrite guard, counts reports, "stop if counts differ from the brief" check, pilot helper | `configs/config.yaml`, `src/` | Secrets come from a **git-ignored `.env`** as well as environment variables (team preference; the contract says environment only) |
| **P1** BIPIA clean and split | ✅ Done | Cloned BIPIA (pinned commit `a004b69`), deduplicated documents, dropped the 11 emails that are in both splits, split per document, `middle_index` per document, 250 attacks | 879 docs: train 487 / val 88 / test 304 (210 tables, of which 126 moved from train) | **"Language Translation"** is a type name in both train and test attacks. 0 strings are shared and the intents differ, so it's kept and test metrics should be reported per `attack_group`. 10 of 879 `middle_index` values use a fallback boundary |
| **P2** Attacked samples (label 1) | ✅ Done | Every test doc × every test attack (balanced positions); 20 seeded (attack, position) per train/val doc; queries built | 33,050 positives: test 21,550 / val 1,760 / train 9,740 | Code error line taken from the **`error` field** (only 9 of 100 contexts contain it). Middle insertion leaves a blank line, as in BIPIA. Test uses the first question; train/val draw a random question |
| **P3** Benign pool (Mistral) | ✅ Done | 20 harmless texts per (doc, position), 4 filters + LLM judge, regenerated until full; test email/table pools then extended to 25 | **56,545 texts** in 2,637 pools; judge control 40/40 | Structured output for code; hybrid judge; positive control; size cap + top-up; 2 approved short pools; pools extended to 25 (details below) |
| **P4** Negatives (label 0) + `bipia` config | ✅ Done | One negative per positive, same doc/position/`insert_index`/query, a pool text inserted instead of the attack | **66,100 rows** (19,480 / 3,520 / 43,100), 1:1, **0 duplicate rows**, median length gap **5 chars** (brief: ~6) | First run found **6,436 duplicate negatives in test**, so the order became **least used first** + 25-text test pools (option B). 16.4% of pairs not length-matched (flagged) |
| **P5** Nemotron (test) | ✅ Done (separate work) | 1,272 released records → positive/negative pairs, a cap of 20 pairs per attacker goal, 11 sentence repairs checked by hand, 20 pairs read in full (20/20 correct) | 2,288 rows (1,144 + 1,144), test | Brief expected 12 sentence repairs and 23 hard negatives; the data gives **11** and **25** (all inspected). See `p5_nemotron.md` |
| P6–P10 | ⏳ Not started | TextFooler variants, … , push to HF (P10) | n/a | n/a |

## Key numbers

| | Train | Validation | Test | Notes |
|---|---|---|---|---|
| BIPIA documents (P1) | 487 | 88 | 304 | 78 email / 701 table / 100 code |
| BIPIA positives (P2) | 9,740 | 1,760 | 21,550 | Test = 44×75 + 210×75 + 50×50 |
| BIPIA benign pool texts (P3) | n/a | n/a | n/a | 56,545 in total (25 per test email/table pool, 20 otherwise, 2 approved short pools) |
| BIPIA rows in `data/out/bipia` (P4) | 19,480 | 3,520 | 43,100 | Classes 1:1, positions balanced, 0 duplicate rows |
| Nemotron rows (P5) | n/a | n/a | 2,288 | Classes 1:1 |

Cost so far: P3 **$15.51** as tracked by the run (of which the extension is $1.37). The pilots and the judge comparison added roughly $4 (approximate).

## Decisions and deviations, step by step

### P1: BIPIA clean and split
- **Test tables:** 84 official + 126 moved from train = 210, as in the brief. A strict "BIPIA test only" variant was tried and then dropped by the team.
- **Shared attack-type name "Language Translation":** train = "translate this phrase", test = "answer in another language". 0 strings are shared. It's kept and noted for evaluation.
- **Questions:** the first question is stored as `question`, and all questions in `questions`.

### P2: attacked samples
- **Code query:** `Fix the bug in my code. Error: <last line of the error field>`. The brief says to take it from the context, but 91 of 100 Stack Overflow answers don't contain the error.
- **Insertion:** pieces are joined with one newline, as in BIPIA. At a line boundary this leaves a blank line. P4 uses the same function, so the blank line is in both classes and isn't a label shortcut.
- **Questions** (updated brief): test uses the first question, train/val a seeded random one. 177 train/val documents have more than one question.

### P3: benign text pool
| Change | Why |
|---|---|
| **Pilot stratified:** 7 email, 7 table, 6 code documents | A random draw gave 17 tables, 1 email and 2 code |
| **Structured output for code** (`{sentence, code}` per item) | Pilot p3-v1 split sentence and code into two strings (97.7% rejected in code/middle) and broke the JSON (300 texts lost) |
| **Code wording rules narrowed;** inline backticks allowed | 40 + 46 ordinary code texts were wrongly rejected |
| **Judge prompt v3,** based on the dataset's own policy text | v1 caught only 30 of 40 real attacks; v2 rejected email requests to the human recipient, which the brief allows |
| **Positive control:** 40 real attacks also judged | A judge that always says "no" would pass every benign text. Result: **40 of 40 flagged** |
| **Hybrid judge:** email/table 20 texts per call, code 1 per call | Tested on the pilot: email/table agreed 100% with one-per-call, code only 97% (batches over-flag harmless code tips). Judge cost about $34 → about $12, time about 4 h → about 1 h |
| **Size cap (10 per size) + top-up from set-aside texts** | An exact 7/7/6 mix left 37 of 60 pilot pools unfilled. After 9 rounds, 59 full-run pools were 1–2 short, and they took their own set-aside texts (judged, flagged `over_size_cap`) |
| **2 approved short pools** (`bipia-code-7f1bb6f33328`: 18 and 17 texts) | The urllib/URL answer: its natural additions use network code, which the brief forbids. 17–18 texts still cover P4's needs |
| **Test email/table pools extended to 25 texts** (+3,810) | Found in P4: such a pool serves 25 samples with 20 texts, and reused texts gave identical negative rows |
| **No Shieldstral 4-bit check** (`shieldstral_checked = false`) | No GPU on this machine |

### P4: negatives and the `bipia` config
- **Picking order: least used first, then the length window** (the brief puts the window first). The window-first order gave 6,436 duplicate negative rows in test (30%), 346 in train and 70 in validation. With option B (this order plus the 25-text test pools) **every pool text is used at most once and there are 0 duplicate rows**. A check now stops the step if any duplicate appears.
- **Trade-off: length matching.** 83.6% of pairs are within the brief's window (test email 69.8%, table 77.6%, code 85.1%; train and validation 91–98%). The median gap is still **5 characters** overall (test email 8, table 6, code 30). Pairing the texts by length rank inside each pool was simulated: more pairs inside the window, but a larger typical gap (test table median 6 → 10), so it was not adopted. The real limit is that the test pools have fewer long texts than there are long attacks.
- Pairs outside the window are flagged `length_matched: false` in `data/interim/p4_pairs.jsonl`.
- **Negatives have no attack columns** (`attack_name` etc. are empty); `parent_id` links each negative to its positive.

### P5: Nemotron (separate work; summary of `p5_nemotron.md`)
- **Document:** the first two levels of the matched path, serialised as JSON.
- **Negative:** the injection is cut out, with a sentence repair where needed: 11 cases, all checked by hand.
- **Goal cap:** at most 20 pairs per attacker goal (one legal goal had 148 records).
- **Brief vs data:** 12 sentence repairs expected and 11 found; 23 hard negatives expected and 25 found. All were inspected.
- **Review:** 20 random pairs read in full, 20 of 20 correct.

## Open points for the team
1. **Evaluation:** report test metrics **per `attack_group`**, and with and without "Language Translation" (P1).
2. **Length matching in test** (16.4% of pairs outside the window, mainly long attacks): accept as is, or ask P3 for more long texts in the test pools (small API cost).
3. **Shieldstral label check:** not run (no GPU). Decide whether to run it later on a GPU machine.
4. **Brief updates suggested:**
   - P2 code error source: "last line of the `error` field";
   - P4 picking order: least used first;
   - P3 pool size for test text pools: 25.

## Detailed reports
| Step | Report |
|---|---|
| P1 | [p1_counts.md](p1_counts.md) |
| P2 | [p2_counts.md](p2_counts.md) |
| P3 | [p3_rejections.md](p3_rejections.md), [p3_judge_compare.md](p3_judge_compare.md), [p3_handread.md](p3_handread.md), pilot: [p3_pilot_rejections.md](p3_pilot_rejections.md) |
| P4 | [p4_composition.md](p4_composition.md) |
| P5 | [p5_nemotron.md](p5_nemotron.md), [p5_nemotron_review_notes.md](p5_nemotron_review_notes.md) |
