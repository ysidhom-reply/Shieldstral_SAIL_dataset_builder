# ipi-shieldstral: working report

**Status:** 2026-11-23 · **Scope:** P0–P5 · **Detailed reports:** `reports/p1/` … `reports/p5/` (links at the end)

## Summary
- **Goal:** a private Hugging Face dataset, `ipi-shieldstral`, to train and benchmark Shieldstral as a guard against indirect prompt injection. It has three configs:
  - `bipia`: train, validation and test;
  - `nemotron`: test;
  - `bipia_textfooler`: later.
- **Done:** steps P0–P5.
  - `bipia` has **66,100 rows**: train 19,480, validation 3,520, test 43,100. Classes are 1:1 and positions are balanced, with no duplicate rows.
  - `nemotron` has **2,288 test rows**, with classes 1:1.
  - Test is larger than train **by design**: every test document meets every test attack (a benchmark), while train samples 20 attacks per document. Train still has more documents (487 vs 304).
- **Every deviation from the briefs is documented,** with its reason, in this report and in the step reports. The largest:
  - the P3 prompt and judge set-up;
  - extending the test pools to 25 texts;
  - P4's "least used first" picking, which removed 6,436 duplicate rows.
- **API cost:** about $15.5 for P3's runs, as tracked, plus roughly $4 for pilots and tests (approximate).

## Pipeline
```
P1 BIPIA documents + attacks ──► P2 attacked samples (label 1) ──┐
                         └─────► P3 benign text pool (Mistral) ──┴─► P4 negatives (label 0) ─► data/out/bipia
P5 Nemotron records ─────────────────────────────────────────────────────────────────────────► data/out/nemotron
```
- **Each pair** is the same document and query, with either an **attack** (label 1) or a **harmless text** (label 0) inserted at the same place. Only the inserted text differs.
- **Splits are per document:** no document appears in two splits.
- **Attacks follow the split:** test documents only get BIPIA's test attacks.

## Steps at a glance

| Step | Status | Output | Main deviations and decisions |
|---|---|---|---|
| **P0** Scaffold | ✅ | Config (seed 42), shared 29-column schema, checks, reports | Secrets also read from a git-ignored `.env` (team preference) |
| **P1** BIPIA clean and split | ✅ | 879 docs: 487 / 88 / 304; 250 attacks | Attack-type name "Language Translation" in both splits (strings differ); kept, noted for evaluation |
| **P2** Attacked samples | ✅ | 33,050 positives (9,740 / 1,760 / 21,550) | Code query uses the `error` field; test = first question, train/val = random question |
| **P3** Benign pool | ✅ | 56,545 texts in 2,637 pools | Structured output for code; hybrid judge; positive control (40/40); size cap + top-up; test pools 25 texts; 2 approved short pools |
| **P4** Negatives + `bipia` | ✅ | 66,100 rows, 1:1, 0 duplicates | Least used first (removed 6,436 duplicates); 16.4% of pairs outside the length window (flagged) |
| **P5** Nemotron | ✅ (separate work) | 2,288 test rows, 1:1 | 11 sentence repairs (brief: 12) and 25 hard negatives (brief: 23), all inspected |
| P6–P10 | ⏳ | n/a | TextFooler variants, …, push to Hugging Face (P10) |

## Key numbers

| | Train | Validation | Test | Notes |
|---|---|---|---|---|
| BIPIA documents (P1) | 487 | 88 | 304 | 78 email / 701 table / 100 code; 210 test tables, of which 126 moved from train (brief) |
| BIPIA positives (P2) | 9,740 | 1,760 | 21,550 | Test = 44×75 + 210×75 + 50×50 |
| Benign pool texts (P3) | n/a | n/a | n/a | 56,545: 25 per test email/table pool, 20 otherwise, 2 approved short pools |
| `bipia` rows (P4) | 19,480 | 3,520 | 43,100 | Classes 1:1, positions balanced, 0 duplicate rows |
| `nemotron` rows (P5) | n/a | n/a | 2,288 | Classes 1:1 |

### Why test has more rows than train (by design)
Test has **more rows** (43,100) than train (19,480), but train has **more documents** (487 vs 304). The difference comes from how many attacks each document gets:

| | Documents | Attacks per document | Rows (positives + twins) |
|---|---|---|---|
| Train | 487 | 20, sampled (`n_per_doc`) | 19,480 |
| Validation | 88 | 20, sampled | 3,520 |
| Test | 304 | **every** test attack (75 text, 50 code) | 43,100 |

- **Test is a benchmark:** every test document meets every test attack, so results can be broken down by attack type and position with enough samples in each cell. BIPIA's own test set works the same way.
- **Train is a sample:** 20 attacks per document give variety without repeating the same document dozens of times; for generalisation, the number of different documents (487) matters more than repetitions.
- **The size of train can be raised without new API calls:** `--n-per-doc 40` gives about 38,960 train rows and `60` about 58,440 (60 is the most the 20-text train pools allow without reusing texts; above that the pools would need extending first). See the open points.

## Decisions and deviations, by step

### P0: scaffold
- Every step is deterministic (seed 42) and writes a counts report.
- A step **stops** if a count differs from its brief.
- `data/out` is never overwritten without `--force`.
- **Secrets:** read from environment variables or a git-ignored `.env` (team preference; the contract says environment only). They're never logged or written.

### P1: BIPIA clean and split
- **The split follows the brief:** official test documents, plus 126 tables moved from train to give 210 test tables, and about 10% of each scenario held out for validation. A "BIPIA test only" variant was tried and dropped by the team.
- **The 11 emails in both official splits** are dropped from train.
- **"Language Translation"** is a type name in both train and test attacks. Train = "translate this phrase", test = "answer in another language". 0 strings are shared, so it's kept. Test metrics should be reported per `attack_group`.
- **`middle_index`:** the brief's boundary (sentence, or line for tables and code) for 869 of 879 documents; a fallback boundary for 10.

### P2: attacked samples (label 1)
- **Code query:** `Fix the bug in my code. Error: <last line of the error field>`. The brief says "find it in the context", but only 9 of 100 Stack Overflow answers contain the error.
- **Insertion:** pieces are joined with one newline, as in BIPIA. At a line boundary this leaves a blank line. P4 uses the same function, so it isn't a label shortcut.
- **Questions:** test uses the first question, train/val a seeded random one (177 documents have several).
- **Samples per document (brief):** test = every document × every test attack; train/validation = 20 sampled attacks per document. This is why test (21,550 positives) is larger than train (9,740) although train has more documents (see *Why test has more rows than train*).

### P3: benign text pool (Mistral Small, judged by Mistral Medium)
| Change | Why |
|---|---|
| **Stratified pilot:** 7 email, 7 table, 6 code documents | A random draw gave 17 tables, 1 email and 2 code |
| **Structured output for code** (`{sentence, code}` per item) | The first pilot split sentence and code into two strings (97.7% rejected in code/middle) and broke the JSON (300 texts lost) |
| **Code wording rules narrowed;** inline backticks allowed | 86 ordinary code texts were wrongly rejected ("ensure that…", `model.eval()`, inline code such as `__module__` in the sentence) |
| **Judge prompt based on the dataset's own policy text** (v3) | v1 caught only 30 of 40 real attacks; v2 rejected email requests to the human recipient, which the brief allows |
| **Positive control:** 40 real attacks also judged in every run | A judge that always says "no" would pass every benign text. Result: **40 of 40 flagged** |
| **Hybrid judge:** email/table 20 texts per call, code 1 per call | Compared on the pilot: email/table agreed 100% with one-per-call; code only 97% (batches over-flag harmless code tips). Judge cost about $34 → $12, time about 4 h → 1 h |
| **Size cap (10 per size) + top-up** from the pool's own set-aside texts | An exact 7/7/6 mix left 37 of 60 pilot pools unfilled; after 9 rounds, 59 full-run pools were 1–2 texts short. Top-up texts are judged and flagged `over_size_cap` |
| **2 approved short pools** (`bipia-code-7f1bb6f33328`: 18 and 17 texts) | An answer about `urllib` and URLs: its natural additions use network code, which the brief forbids. 17–18 texts still cover P4's needs |
| **Test email/table pools extended to 25 texts** (+3,810) | Found in P4: such a pool serves 25 attacked samples with 20 texts, so reused texts gave identical negative rows |
| **No Shieldstral 4-bit check** (`shieldstral_checked = false`) | No GPU on this machine |

### P4: negatives (label 0) and the `bipia` config
- **Picking order: least used first, then the length window** (the brief puts the window first). The window-first order gave **6,436 duplicate negative rows in test** (30%), 346 in train and 70 in validation. With this order and the 25-text test pools, every pool text is used at most once, and there are **0 duplicate rows**. A check stops the step if a duplicate appears.
- **Length matching (accepted trade-off):** 83.6% of pairs are within the brief's window (30% of the attack's length; 50% for code). The median gap is **5 characters** overall (brief: about 6). The pairs outside the window are mostly long test attacks, because the pools have few long texts.
  - Pairs outside the window are flagged `length_matched: false` in `data/interim/p4_pairs.jsonl`.
  - **Two alternatives were tested and not adopted:** pairing texts by length rank (more pairs in the window, but a larger typical gap), and asking P3's prompt for lengths in words (no gain for long attacks on the pilot).
- **Negatives have no attack columns;** `parent_id` links each negative to its positive.

### P5: Nemotron (separate work; summary of `p5/p5_nemotron.md`)
- **Document:** the first two levels of the matched path, serialised as JSON.
- **Negative:** the injection is cut out, with a sentence repair where needed. All 11 repairs were checked by hand.
- **Goal cap:** at most 20 pairs per attacker goal (one legal goal had 148 records).
- **Brief vs data:** 11 sentence repairs (brief: 12) and 25 hard negatives (brief: 23). Every cut point and hard negative was inspected.
- **Review:** 20 random pairs read in full, all correct.

## Open points for the team
1. **Evaluation:** report test metrics per `attack_group`, and with and without "Language Translation".
2. **Training size:** train has 19,480 rows (20 attacks per document). Decide after a first Shieldstral training whether that is enough; `n_per_doc` 40 or 60 gives about 39k or 58k train rows with no new API cost (above 60, the train pools need extending).
3. **Shieldstral label check:** not run (no GPU). Decide whether to run it later on a GPU machine.
4. **Brief updates suggested:**
   - P2 code error = "last line of the `error` field";
   - P3 test email/table pools = 25 texts;
   - P4 picking = least used first.

## Detailed reports
| Step | Report |
|---|---|
| P1 | [p1/p1_counts.md](p1/p1_counts.md) |
| P2 | [p2/p2_counts.md](p2/p2_counts.md) |
| P3 | [p3/p3_rejections.md](p3/p3_rejections.md), [p3/p3_judge_compare.md](p3/p3_judge_compare.md), [p3/p3_handread.md](p3/p3_handread.md), pilot: [p3/p3_pilot_rejections.md](p3/p3_pilot_rejections.md) |
| P4 | [p4/p4_composition.md](p4/p4_composition.md) |
| P5 | [p5/p5_nemotron.md](p5/p5_nemotron.md), [p5/p5_nemotron_review_notes.md](p5/p5_nemotron_review_notes.md) |
