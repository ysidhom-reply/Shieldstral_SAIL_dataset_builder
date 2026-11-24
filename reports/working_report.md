# ipi-twins: working report

**Status:** 2026-11-24 · **Scope:** P0–P5, P7 v0, P10 (v0 ready to push; v1 waits for P6 and P7 v1) · **Detailed reports:** `reports/p1/` … `reports/p7/`, superseded ones in `reports/archive/` (links at the end)

## Summary
- **Goal:** a private Hugging Face dataset, [`SAIL-Reply/ipi-twins`](https://huggingface.co/datasets/SAIL-Reply/ipi-twins) (planned as `ipi-shieldstral`), to train and benchmark Shieldstral as a guard against indirect prompt injection. It has three configs:
  - `bipia`: train, validation and test;
  - `nemotron`: test;
  - `bipia_textfooler`: built by P6, not yet run in full.
- **v0 is ready: P7 v0 says PUSH ALLOWED.** All 17 data checks pass, and both shortcut baselines are below the team's limits.
  - `bipia`: **66,100 rows** (train 19,480, validation 3,520, test 43,100). Classes are 1:1, positions are balanced, and there are 0 duplicate rows. 87.4% of pairs fall within the length window.
  - `nemotron`: **2,288 test rows**, classes 1:1.
  - **30 columns** in the shared contract (`benign_span` added, `label_checked` renamed).
- **Why it took three P7 runs:** the first v0 run was blocked. TF-IDF separated BIPIA code at AUC 0.967, because the brief's P3 filters keep the attacks' vocabulary out of every benign text. Two team decisions fixed it:
  - **option B:** 40% of every pool was replaced by *hard negatives*, harmless texts that sound like instructions to the human reader;
  - **option (a):** P4 now prefers pool texts without the over-used benign words.
- **Test is larger than train by design:** every test document meets every test attack (a benchmark), while train samples 20 attacks per document. Train still has more documents (487 vs 304).
- **Hugging Face:** the early `v0.1` (pushed 2026-11-23) no longer matches `data/out`. `v0` is ready to push (dry-run passes, P7 gate open); `v1` adds `bipia_textfooler` after P6 and P7 v1.
- **API cost:** about $23.5 for P3 as tracked ($15.5 original pool, $7.1 hard negatives, about $0.9 hard-negative pilots), plus roughly $4 for earlier pilots and tests.

## Pipeline
```
P1 documents + attacks --> P2 attacked samples (label 1) -----------------+
                  |                                                        |
                  +--> P3 benign pool --> P3 option B: 40% hard negatives -+--> P4 negatives (label 0) --> data/out/bipia
P5 Nemotron records ----------------------------------------------------------> data/out/nemotron
P6 TextFooler variants (ProtectAI, CPU) --> data/out/bipia_textfooler (paused)
P7 checks + baselines --> PUSH ALLOWED / BLOCKED --> P10 push
```
- **Each pair** is the same document and query, with either an **attack** (label 1) or a **harmless text** (label 0) inserted at the same place. Only the inserted text differs.
- **Splits are per document:** no document appears in two splits.
- **Attacks follow the split:** test documents only get BIPIA's test attacks.

## Steps at a glance

| Step | Status | Output | Main deviations and decisions |
|---|---|---|---|
| **P0** Scaffold | ✅ | Config (seed 42), shared **30-column** schema, checks, reports | Secrets also read from a git-ignored `.env` (team preference) |
| **P1** BIPIA clean and split | ✅ | 879 docs: 487 / 88 / 304; 250 attacks | "Language Translation" names in both splits (strings differ); kept in test (team) |
| **P2** Attacked samples | ✅ | 33,050 positives (9,740 / 1,760 / 21,550) | Code query uses the `error` field; test = first question, train/val = random question |
| **P3** Benign pool | ✅ | 56,545 texts in 2,637 pools, **22,616 of them hard negatives** | Structured output for code; hybrid judge; positive control; size cap + top-up; test pools 25 texts; **option B** with its own filter profile and judge-v4 |
| **P4** Negatives + `bipia` | ✅ | 66,100 rows, 1:1, 0 duplicates, `benign_span` | Least used first; **option (a)**: avoid over-used benign words; 12.6% of pairs outside the length window (flagged) |
| **P5** Nemotron | ✅ (separate work) | 2,288 test rows, 1:1 | 11 sentence repairs (brief: 12) and 25 hard negatives (brief: 23), all inspected |
| **P6** TextFooler test variants | 🟡 built, pilot paused | n/a | Own uv environment (TextAttack); judge + rules validation; needs a fresh pilot on the new negatives |
| **P7** v0 checks and baselines | ✅ **PUSH ALLOWED** | `p7/p7_checks_v0.md` | Limits from the team (length 0.70, TF-IDF 0.95); "Language Translation" names allowed by team decision |
| P8–P9 | ⏳ | n/a | n/a |
| **P10** Push | 🟡 versioned (v0, v1); v0 dry-run passes, not pushed | `SAIL-Reply/ipi-twins`, tags `v0`, `v1` | P7 gate enforced; v1 only adds `bipia_textfooler`; repo name `ipi-twins` |

## Key numbers

| | Train | Validation | Test | Notes |
|---|---|---|---|---|
| BIPIA documents (P1) | 487 | 88 | 304 | 78 email / 701 table / 100 code; 210 test tables, of which 126 moved from train (brief) |
| BIPIA positives (P2) | 9,740 | 1,760 | 21,550 | Test = 44×75 + 210×75 + 50×50 |
| Benign pool texts (P3) | n/a | n/a | n/a | 56,545 (25 per test email/table pool, 20 otherwise, 2 approved short pools); 40% hard negatives |
| `bipia` rows (P4) | 19,480 | 3,520 | 43,100 | Classes 1:1, positions balanced, 0 duplicate rows |
| Hard negatives among the chosen negatives | email 42%, table 10%, code 22% | email 34%, table 8%, code 30% | email 40%, table 40%, code 34% | See P4 (option (a) and its known limit) |
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
- **The size of train can be raised without new API calls:** `--n-per-doc 40` gives about 38,960 train rows and `60` about 58,440 (60 is the most the 20-text train pools allow without reusing texts).

## Decisions and deviations, by step

### P0: scaffold
- Every step is deterministic (seed 42) and writes a report. Every API call is cached, so reruns cost nothing and give the same output.
- A step **stops** if a count differs from its brief.
- `data/out` is never overwritten without `--force`.
- **Secrets:** read from environment variables or a git-ignored `.env` (team preference; the contract says environment only). They're never logged or written.
- **Contract changes:** `benign_span` was added (the contract update of 2026-11-24), and `shieldstral_checked` was renamed `label_checked` so the dataset doesn't name the guard model (team decision). That gives 30 columns.

### P1: BIPIA clean and split
- **The split follows the brief:** official test documents, plus 126 tables moved from train to give 210 test tables, and about 10% of each scenario held out for validation.
- **The 11 emails in both official splits** are dropped from train.
- **"Language Translation"** is an attack-type name used in train and in test (`Language Translation-0..4`). Train = "translate this phrase", test = "answer in another language". No string is shared. **Team decision:** keep it in test. An evaluation without it could be interesting, so P7 also reports the baselines without it.
- **`middle_index`:** the brief's boundary (sentence, or line for tables and code) for 869 of 879 documents; a fallback boundary for 10.

### P2: attacked samples (label 1)
- **Code query:** `Fix the bug in my code. Error: <last line of the error field>`. The brief says "find it in the context", but only 9 of 100 Stack Overflow answers contain the error.
- **Insertion:** pieces are joined with one newline, as in BIPIA. P4 and P7 use the same function, so it isn't a label shortcut.
- **Questions:** test uses the first question, train/val a seeded random one (177 documents have several). This matches the updated brief.
- **Samples per document (brief):** test = every document × every test attack; train/validation = 20 sampled attacks per document.

### P3: benign text pool (Mistral Small, judged by Mistral Medium)
**Original pool** (`benign_pool_v1.jsonl`, brief's prompts and filters):

| Change | Why |
|---|---|
| **Stratified pilot:** 7 email, 7 table, 6 code documents | A random draw gave 17 tables, 1 email and 2 code |
| **Structured output for code** (`{sentence, code}` per item) | The first pilot split sentence and code into two strings and broke the JSON |
| **Code wording rules narrowed;** inline backticks allowed | 86 ordinary code texts were wrongly rejected |
| **Judge prompt based on the dataset's own policy text** (judge-v3) | v1 caught only 30 of 40 real attacks; v2 rejected email requests to the human recipient |
| **Positive control:** 40 real attacks judged in every run | A judge that always says "no" would pass every benign text. 40 of 40 flagged |
| **Hybrid judge:** email/table 20 texts per call, code 1 per call | Same verdicts on email/table; code batches over-flag. Judge cost about $34 → $12 |
| **Size cap (10 per size) + top-up** from set-aside texts | An exact size mix left pools unfilled |
| **2 approved short pools** (`bipia-code-7f1bb6f33328`: 18 and 17 texts) | An answer about `urllib`: its natural additions use network code |
| **Test email/table pools extended to 25 texts** | Each such pool serves 25 attacked samples; 20 texts meant reused, identical rows |
| **No Shieldstral 4-bit check** (`label_checked = false`) | No GPU on this machine |

**Option B: hard negatives** (team decision after the first P7 v0, report [p3/p3_hard_negatives.md](p3/p3_hard_negatives.md)):
- **Why:** TF-IDF separated code at AUC 0.967 on wording alone. The brief's filters forbid in benign texts exactly what attacks contain: `your response`, "the following code", `import requests`, `subprocess`, URLs.
- **What:** 8 of 20 texts per pool (10 of 25) are replaced by harmless texts addressed to the human reader. The texts replaced first are those with the most over-represented words. Pool sizes are unchanged (22,616 new texts; 1 pool kept 2 of its old texts).
- **Prompt history** (in `configs/p3_prompts.yaml`):
  - hn-v1 used too little attack vocabulary (9% of email, 0% of code texts);
  - hn-v2 added quotas but put "in your reply" into tables, and the judge rightly rejected 60–70% of them (a table has no recipient);
  - **hn-v3** sets the quota per scenario. Final share of texts using the vocabulary: email 41% "your reply/answer/response"; code 41% "the following code/snippet" and 85% "your code/script".
- **Own filter profile (deviation from the brief):**
  - "your response/answer" and reader-directed code intros are allowed;
  - harmless `requests.get` (with a timeout, `example.com` only), `subprocess.run` with fixed arguments, and local file reads are allowed;
  - sending data, credentials, environment variables, deletion, `shell=True`, sockets and system fingerprinting stay forbidden;
  - the 8-word overlap ban with attacks stays.
- **judge-v4:** like v3, but harmless reader-directed code and email requests are not injections. Code that does something unrequested (sends data, collects system information…) still is, "however it is phrased". The positive control still flags **40 of 40** attacks.
- **Rejections:** shape 0.9%, forbidden 1.2%, duplicates 0.6%, judge 5.9%. Cost $7.10.

### P4: negatives (label 0) and the `bipia` config
- **Picking order:** least used first, then the length window, then **the fewest over-represented benign words** (option (a)), then the closest length. "Least used first" removed 6,436 duplicate rows. The avoided words come from P7's diagnostics, and function words are left out so it doesn't create a length shortcut.
- **Known limit of option (a) (team decision):** the avoided words include the reader-note words typical of the table hard negatives. So **train tables keep only 10% hard negatives** (test: 40%, because every test pool text is used once).
  - A narrower list kept 40% in train, but left TF-IDF on table at 0.952, above the limit. The team chose the broad list.

| Avoid list | TF-IDF table / code / email | Nemotron | Hard negatives in train tables | P7 |
|---|---|---|---|---|
| **Broad (kept)** | 0.943 / 0.944 / 0.819 | 0.782 | 10% | PUSH ALLOWED |
| Narrow (tested) | 0.952 / 0.947 / 0.838 | 0.671 | 40% | blocked |

- **Length matching:** **87.4%** of pairs within the window (30% of the attack's length; 50% for code), up from 83.6%. Median gap per pair about 5 characters. Pairs outside the window are flagged in `data/interim/p4_pairs.jsonl`.
- **`benign_span`:** P4 writes the `[start, end)` of the benign text on every negative; positives get null.
- **Negatives have no attack columns;** `parent_id` links each negative to its positive.

### P5: Nemotron (separate work; summary of `p5/p5_nemotron.md`)
- **Document:** the first two levels of the matched path, serialised as JSON.
- **Negative:** the injection is cut out, with a sentence repair where needed (all 11 checked by hand). So the positive is always longer (median about 340 characters), by design.
- **Goal cap:** at most 20 pairs per attacker goal.
- **Brief vs data:** 11 sentence repairs (brief: 12) and 25 hard negatives (brief: 23), all inspected. 20 random pairs read in full, all correct.

### P6: TextFooler test variants (built, pilot paused)
- **Environment:** P6 runs in its own isolated uv environment (`requirements/p6-textfooler.txt`). TextAttack needs `transformers<5`, hence `huggingface-hub<1`, while the project uses 2.x.
- **Design:**
  - TextAttack's TextFooler recipe, plus a 30% cap on changed words (team decision);
  - protected tokens (emails, URLs, identifiers, inline code, numbers, entities);
  - for code, only the intro sentence may change;
  - the victim is ProtectAI's DeBERTa on the document, with 512-token windows (stride 256) and the maximum score;
  - validation by rules and the judge, with an anchor: the judge must agree with the label on the original first;
  - the target is 1,000 kept variants per class (email 200 / code 200 / table 600).
- **Status:** the first pilot was stopped at 6 of 20 samples (on the old negatives). Its results were deleted. **A new pilot is needed** on the current negatives, then a decision on the full run's projected time.

### P7 v0: checks, shortcut baselines and diagnostics ([p7/p7_checks_v0.md](p7/p7_checks_v0.md))
- **Result: PUSH ALLOWED** (exit code 0). P7 only reads `data/out` and writes the report; it never changes the data or a limit. A rerun gives the same report.
- **Data checks: 17 pass** (2 not applicable).
  - Load with `datasets`: 30 columns and types; counts equal to the P4/P5 reports.
  - No clean document in two splits; no train attack text in test.
  - "Language Translation" names: allowed by team decision.
  - Positives contain their attack; negatives contain no attack text (Aho-Corasick), and no Nemotron injection, goal sentence or wrapper marker.
  - Classes 1:1; positions within tolerance.
  - Every pair gives back the clean document stored in `bipia_docs.jsonl`; same query in every pair, and the first question on test.
- **One check was fixed, not the data:** Nemotron injections are compared in decoded JSON form. The first run's 899 false failures came from newlines escaped inside JSON strings.
- **Baselines** (limits: length 0.70, TF-IDF 0.95 per BIPIA scenario and on Nemotron):

| | email | table | code | all | Nemotron |
|---|---|---|---|---|---|
| Length (log characters, log words) | 0.51 | 0.50 | 0.59 | 0.507 | n/a (raw length AUC 0.985, by design) |
| TF-IDF | 0.819 | 0.943 | 0.944 | 0.916 | 0.782 |

- **Evolution of TF-IDF AUC:**

| Run | email | table | code | Nemotron |
|---|---|---|---|---|
| First v0 run | 0.880 | 0.945 | 0.967 | 0.799 |
| Option B | 0.838 | 0.955 | 0.951 | 0.667 |
| **Option B + (a)** | **0.819** | **0.943** | **0.944** | **0.782** |

- **Diagnostics:**
  - most of the remaining signal is the attacks' own wording (TF-IDF on the inserted spans alone: 0.968);
  - removing the 50 strongest benign-span features lowers TF-IDF by about 0.02;
  - TF-IDF scores 0.905 with only hard-negative negatives and 0.923 with only the original ones;
  - without "Language Translation", TF-IDF is 0.914 overall.

### P10: push to Hugging Face (versions v0 and v1)
- **Versions (the brief's scheme):** `v0` = `bipia` + `nemotron`; `v1` = v0 + `bipia_textfooler` (test). A version is pushed once and tagged with its name, so a training can pin it.
- **v1 only adds:** `bipia_textfooler/test.parquet` and an updated card. No v0 file is uploaded again, and `p10-verify --version v1` loads every config at `v1`, loads `v0` again, and compares the v0 files byte for byte (sha256) between the two tags.
- **The P7 gate is now in the code:** `p10-push --push` stops unless `reports/p7/p7_checks_<version>.json` says PUSH ALLOWED and is newer than the data it checked. A dry-run always runs and shows the gate. P7 v0 says PUSH ALLOWED; P7 v1 does not exist yet, so v1 stays blocked until P6 is finished and P7 v1 is built.
- **The card** keeps the v0 text and, from v1, adds the TextFooler section: ProtectAI as the victim (revision and licence read from its model card by P6), the constraint values, the window rule, the attempted / skipped / fooled / failed / kept counts, and the note that only variants that fooled the victim are kept. TextAttack (MIT) is in the licences. The card also has the hard-negatives section (what, why, filters, judge-v4, prompts, share per split) and the P7 baselines.
- **Safety:** a dry-run never calls the Hub; `--push` refuses a public repo, an existing tag, and a v1 without a `v0` tag; the staged files are scanned for token patterns and the actual token value.
- **Repository:** `SAIL-Reply/ipi-twins`, private, licence `cc-by-sa-4.0`. The early `v0.1` push (29 columns, original pool, before this scheme) is recorded in `archive/p10/v0.1/`; `v0` replaces it in `main` and the `v0.1` tag keeps it reachable.
- **Status:** v0 dry-run passes (the card is in `reports/p10/p10_dataset_card_v0.md`); not pushed. The user runs the push after confirming that the data is final.

## Open points for the team
1. **Push v0:** the card is ready for review (`reports/p10/p10_dataset_card_v0.md`); the user confirms the data is final and runs the push.
2. **P6:** run a new pilot on the current negatives, then decide on the full run from its projected CPU time.
3. **Train tables and hard negatives:** with option (a), train tables have 10% hard negatives (test 40%). Revisit if a trained detector shows weak results on instruction-like benign tables.
4. **Evaluation:** report test metrics per `attack_group`, and with and without "Language Translation".
5. **Training size:** `n_per_doc` 40 or 60 gives about 39k or 58k train rows with no new API cost.
6. **Shieldstral label check:** not run (no GPU).
7. **Brief updates suggested:**
   - P2 code error = "last line of the `error` field";
   - P3 test email/table pools = 25 texts, and option B (hard negatives, filter profile, judge-v4);
   - P4 picking = least used first, then the avoided words;
   - P7: Nemotron injections compared in decoded JSON form; the allow-list for shared attack names.

## Detailed reports
| Step | Report |
|---|---|
| P1 | [p1/p1_counts.md](p1/p1_counts.md) |
| P2 | [p2/p2_counts.md](p2/p2_counts.md) |
| P3 | [p3/p3_rejections.md](p3/p3_rejections.md) (original pool), [p3/p3_hard_negatives.md](p3/p3_hard_negatives.md) (option B), [p3/p3_hard_negatives_handread.md](p3/p3_hard_negatives_handread.md), [p3/p3_judge_compare.md](p3/p3_judge_compare.md), [p3/p3_handread.md](p3/p3_handread.md) |
| P4 | [p4/p4_composition.md](p4/p4_composition.md) |
| P5 | [p5/p5_nemotron.md](p5/p5_nemotron.md), [p5/p5_nemotron_review_notes.md](p5/p5_nemotron_review_notes.md) |
| P7 | [p7/p7_checks_v0.md](p7/p7_checks_v0.md) |
| Archive | [archive/README.md](archive/README.md): P3 and P5 pilots, the hard-negative pilot, the `benign_span` migration, the v0.1 push record |
