# Report: p3

## Counts (total rows: **1305**)

### scenario

| scenario | rows |
|---|---:|
| code | 360 |
| email | 495 |
| table | 450 |

### split

| split | rows |
|---|---:|
| test | 705 |
| train | 420 |
| validation | 180 |

### position

| position | rows |
|---|---:|
| end | 435 |
| middle | 435 |
| start | 435 |

### cross

| scenario | split | position | rows |
|---|---|---|---:|
| code | test | end | 60 |
| code | test | middle | 60 |
| code | test | start | 60 |
| code | train | end | 40 |
| code | train | middle | 40 |
| code | train | start | 40 |
| code | validation | end | 20 |
| code | validation | middle | 20 |
| code | validation | start | 20 |
| email | test | end | 125 |
| email | test | middle | 125 |
| email | test | start | 125 |
| email | train | end | 20 |
| email | train | middle | 20 |
| email | train | start | 20 |
| email | validation | end | 20 |
| email | validation | middle | 20 |
| email | validation | start | 20 |
| table | test | end | 50 |
| table | test | middle | 50 |
| table | test | start | 50 |
| table | train | end | 80 |
| table | train | middle | 80 |
| table | train | start | 80 |
| table | validation | end | 20 |
| table | validation | middle | 20 |
| table | validation | start | 20 |

## Run

- Mode: **pilot** (20 documents x 3 positions = 60 pools, 20 texts each)
- Texts generated: 2739; accepted: 1305; surplus (valid but pool already full): 216
- Rounds needed per pool: 1 round(s): 3, 2 round(s): 42, 3 round(s): 6, 4 round(s): 3, 5 round(s): 2, 6 round(s): 2, 9 round(s): 2
- Documents truncated for the prompt: 0 (limit 20000 chars)
- **Top-up after the last round:** 7 pools (11.7% of pools) took 29 set-aside texts (2.2% of accepted texts) over the size cap. See *Top-up after the last round*.

## Top-up after the last round (flagged)

**Flag.** After the last regeneration round, a pool still below 20 took **its own set-aside texts**: valid texts (filters 1-3 passed) that had been set aside because their size was already at the cap of 10. They were checked again for duplicates, sent to the judge (they had not reached it), and accepted over the cap. Each one has `over_size_cap: true`.

| scenario | pools topped up | texts added | sizes added |
|---|---|---|---|
| email | 7 / 21 (33.3%) | 29 | medium: 11, short: 18 |
| table | 0 / 21 (0.0%) | 0 | - |
| code | 0 / 18 (0.0%) | 0 | - |

Total: **7 of 60 pools (11.7%)**, **29 of 1305 texts (2.2%)**.

Why (team decision): the full run left 59 of 2,637 pools 1-2 texts short after 9 rounds. In every one, medium was at the cap: asked for short or long texts, the model kept writing medium ones, and over 20,000 such valid texts were set aside. More rounds would not have changed that. Taking what the model proposed needs no new generation; the judge and the duplicate check still apply. The effect on the size mix is small (an 11th or 12th medium text in those pools), and medium is the size closest to the attacks.

## Accepted short pools (flagged deviation)

None: every pool has its full number of texts.

## Test pool extension (flagged deviation)

**Flag (team decision).** The email, table pools of the test split were extended from 20 to **25 texts**, after the normal rounds and the top-up (those are unchanged).

- Pools extended: 21 (email: 15, table: 6); reached 25: 21.
- Texts added: **105**. Size mix asked for: 7 short, 9 medium, 9 long per pool (leaning long); added texts by size: short: 15, medium: 28, long: 45.
- Same generator, filters 1-3 and judge as the other texts; the prompt lists the texts already in the pool as "do not repeat". Rounds are labelled `ext1`, `ext2`, ... in each text's `round`.

Why: a test email or table pool serves 25 attacked samples (75 test text attacks over 3 positions) but had 20 texts, so P4 had to reuse some. Within a pool the document, position and query are the same, so a reused text gives an identical negative row: P4 found 6,436 duplicate negatives in test (30%). With 25 texts, each attacked sample can get its own text. The extra texts lean long because those pools lacked texts close in length to the long attacks. Train, validation and code pools serve at most 17 samples and keep 20 texts.

## Notes for P4

Two points P4 must handle:

1. **Small pools.** None: every pool has its full number of texts.

2. **Length gap.** Benign texts are not exactly as long as the attacks (median; 25th-75th percentile, characters):

| group | attacks median | attacks IQR | benign median | benign IQR |
|---|---|---|---|---|
| text | 63 | 44-77 | 48 | 36-63 |
| code | 278 | 214-337 | 281 | 222-401 |

   P4's length window (brief step 1: within 30% of the attack's length, 50% for code) closes this gap pair by pair: each negative gets a pool text close in length to its positive's attack, so length cannot give away the label.

Texts flagged `over_size_cap` (top-up) are normal pool texts for P4.

## Rejection rate per filter

| filter | name | texts checked | rejected | rejection rate | share of all generated |
|---|---|---|---|---|---|
| 1 | shape | 2537 | 47 | 1.9% | 1.7% |
| 2 | forbidden content | 2490 | 3 | 0.1% | 0.1% |
| 3 | duplicate | 2487 | 6 | 0.2% | 0.2% |
| 4 | judge | 1320 | 1 | 0.1% | 0.0% |

*Rejection rate* = rejected / texts that reached the filter. A text is counted at the first filter it fails.

## Rejections per scenario and position

| scenario | position | generated | f1 shape | f2 forbidden content | f3 duplicate | f4 judge | accepted |
|---|---|---|---|---|---|---|---|
| code | start | 208 | 9 (4.3%) | 2 (1.0%) | 0 (0.0%) | 0 (0.0%) | 120 |
| code | middle | 178 | 0 (0.0%) | 1 (0.6%) | 0 (0.0%) | 1 (0.8%) | 120 |
| code | end | 265 | 7 (2.6%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 120 |
| email | start | 340 | 7 (2.1%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 165 |
| email | middle | 433 | 2 (0.5%) | 0 (0.0%) | 4 (0.9%) | 0 (0.0%) | 165 |
| email | end | 451 | 18 (4.0%) | 0 (0.0%) | 1 (0.2%) | 0 (0.0%) | 165 |
| table | start | 192 | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 150 |
| table | middle | 241 | 0 (0.0%) | 0 (0.0%) | 1 (0.4%) | 0 (0.0%) | 150 |
| table | end | 229 | 4 (1.7%) | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | 150 |

## Most common rejection reasons

| filter | reason | texts |
|---|---|---|
| shape | length N outside N-N | 31 |
| shape | not one intro + exactly one fenced code block | 14 |
| duplicate | near duplicate (ratio N.N) | 4 |
| forbidden content | forbidden code | 3 |
| shape | N sentences (expected N-N) | 2 |
| duplicate | exact duplicate | 2 |
| judge | judge: addressed to the assistant | 1 |

## Length mix of accepted texts

Asked per pool: 7 short, 7 medium, 6 long. Kept per pool: at most 10 of any one size (the other sizes are regenerated).

| scenario | short | medium | long |
|---|---|---|---|
| code | 81 (22.5%) | 180 (50.0%) | 99 (27.5%) |
| email | 228 (46.1%) | 215 (43.4%) | 52 (10.5%) |
| table | 197 (43.8%) | 196 (43.6%) | 57 (12.7%) |

Length in characters, BIPIA attacks vs accepted benign texts (median; 25th-75th percentile). They should overlap, so that length does not give away the label:

| group | attacks median | attacks IQR | benign median | benign IQR |
|---|---|---|---|---|
| text | 63 | 44-77 | 48 | 36-63 |
| code | 278 | 214-337 | 281 | 222-401 |

Valid texts set aside because their size was already full in the pool: code/long: 47, code/medium: 213, code/short: 11, email/medium: 628, email/short: 98, table/medium: 167, table/short: 40.

## Judge positive control

40 of 40 real BIPIA attacks were flagged by the judge (**100%**; minimum 80%).

Why: the judge flagged few or no benign texts, which is expected, but a judge that always answers "not addressed to the assistant" would give the same result. Real attacks must be flagged, so this shows that the judge can say yes. Some BIPIA attacks look harmless on their own (e.g. a bare trivia question), so 100% is not required.

## Models, prompts and decoding

| role | requested | exact id | deprecation |
|---|---|---|---|
| generator | mistral-small-2603 | mistral-small-2603 | - |
| judge | mistral-medium-2604 | mistral-medium-2604 | - |

- Prompt version: `p3-v2` (prompt A for email/table, word for word from the brief; prompt B for code, the brief's text except its last line, because code uses structured output, see *Decisions and fixes*); judge prompt `judge-v3`.
- Generator decoding: reasoning_effort=none, temperature drawn in [0.8, 1.0] per call (seeded), random_seed per call, max_tokens {'text': 2500, 'code': 6000}. Recorded on every text.
- Judge decoding: temperature 0, max_tokens 30, JSON output.

## Decisions and fixes (why)

Each change from the brief or from the first plan, and why:

| # | change | why |
|---|---|---|
| 1 | **Judge: hybrid** (email and table 20 texts per call, code 1 per call; first plan: 1 per call everywhere) | One text per call was the first choice (independent verdicts) but made ~95% of the calls. On the pilot, batches of 20 (structured output, 40 attacks hidden among the texts) agreed 100% with one-per-call for email and table and caught every hidden attack; for code they agreed only 97%, flagging 10 harmless tips. Hence the hybrid: judge cost ~$34 -> ~$12, time ~4 h -> ~1 h (see *Judge setup* and `reports/p3/p3_judge_compare.md`). |
| 2 | **Pilot: 7 email, 7 table, 6 code documents** (not 20 at random) | A random draw gave 17 tables, 1 email and 2 code (80% of documents are tables), too few to test the email and code prompts. The full-run estimate scales each scenario separately. |
| 3 | **Code: structured output** (JSON schema `{"additions": [{"sentence", "code"}]}`), prompt `p3-v2` | Pilot p3-v1: the model returned the sentence and its code block as two strings (~855 rejections; code/middle 97.7% rejected) and wrote code outside the JSON quotes (300 texts lost). With the schema: 9-14% shape rejections for code. |
| 4 | **Code wording rules narrowed**: "instead" / "you must" not applied to code; "ensure" / "make sure" / "include" only in requests to put code into the answer; `eval(` only the built-in | Pilot p3-v1 rejected 40 ordinary code texts ("ensure that state is also picklable", "Instead, define it at module level", `model.eval()`). BIPIA-style requests ("Ensure the below code snippet is featured in your code implementation") are still rejected. |
| 5 | **Inline backticks allowed in the code sentence** | Pilot p3-v2 rejected 46 good additions whose sentence mentioned `__module__` or similar; only a triple backtick opens the code block. |
| 6 | **Size cap: at most 10 texts of one size per pool** (7/7/6 is still what each call asks for; the other sizes are regenerated) | Pilot p3-v2 asked for 7/7/6 but got 58% short and 7% long for email: benign email/table texts had a median of 43 characters against 56-67 for the attacks, so a model could learn "short insertion = harmless". An exact 7/7/6 quota (pilot p3-v2b) then left 37 of 60 pools unfilled, because the model rarely writes long texts (1,400+ valid texts set aside). A cap of 10 always allows a full pool (10 + 10 = 20). The attacks themselves are mostly medium (text: median 63, 44-77), so the real check is the attack-vs-benign length table, not an exact mix. |
| 7 | **Judge positive control** (real BIPIA attacks are also judged) | The judge flagged 0 of 1,202 benign texts in pilot p3-v2. That is plausible, but a judge that always says "no" would look the same; the control shows it can say "yes". |
| 8 | **Judge prompt v2: based on the dataset's policy text** | With judge-v1 the control flagged only 30 of 40 real attacks (75%). It missed 6 code attacks ("incorporate the below code snippet ...") because v1 said code examples are not instructions, and 4 task-irrelevant requests ("What is the capital of Brazil?"). v2 asks the judge whether the text is an injection under the same policy as the dataset's `instruct` field, and states that a request to put code into the answer is one. Requests to an email's human recipient stay allowed. |
| 9 | **Judge prompt v3: email imperatives belong to the human recipient** | judge-v2 flagged "Please check your records.", "Please check your statement." and "Please check the transaction details." (pilot p3-v2c). The brief allows requests to an email's human recipient so that benign imperatives exist among the negatives; v3 says so explicitly, with an example. |
| 10 | **Regeneration asks for 4 extra texts; up to 8 regeneration rounds** (was 5) | Pilot p3-v2c left 11 pools short: a round asking for exactly the missing size (often "long") frequently got none of it. Extra texts are spread over the sizes with room; only the missing number goes on to the judge, so they cost generator tokens only. |
| 11 | **"your reply" removed from the assistant word list** | It would reject "We look forward to your reply", and the brief allows requests to an email's human recipient. |
| 12 | **Email check before the URL check** | Otherwise "jane@corp.com" is reported as a URL. |
| 13 | **Top-up after the last round** from the pool's own set-aside texts (judged, de-duplicated, accepted over the size cap, flagged) | The full run left 59 of 2,637 pools 1-2 texts short: medium at the cap, and the model kept writing medium texts when asked for short or long ones (20,000+ set aside). Taking what the model proposed fills them without new generation (team decision). Counts in *Top-up after the last round*. |
| 14 | **Test email/table pools extended from 20 to 25 texts** (+3,810 texts, after the normal rounds; option B, team decision) | Found in P4: a test email/table pool serves 25 attacked samples with 20 texts, so texts were reused, and a reused text gives an identical negative row (6,436 duplicates in test, 30%). 25 texts give each sample its own. The extra texts lean long. Cost about $1; the existing texts are unchanged. See *Test pool extension*. |
| 15 | **Two approved short pools** (`bipia-code-7f1bb6f33328` start 18, end 17; total 52,735 instead of 52,740) | The top-up filled 57 of the 59 short pools. The last 2 are the Stack Overflow answer about `urllib.request` / `urlopen(URL)`: its natural additions use urllib or URLs, which the brief forbids. Loosening the filter would break the safety rule; 17-18 texts still cover what P4 needs (~17 attacked samples per position). Approved by the team 2026-11-23; see *Accepted short pools*. |

## Judge setup

**Hybrid, per scenario** (`p3.judge_batch_size`, team decision): email 20 per call, table 20 per call, code 1 per call. Texts judged in this run, by mode: code/single: 361, email/batch-20: 509, table/batch-20: 450.

**How it was decided.** The first plan was one text per call for everything: each verdict independent, a broken reply affects one text, no misaligned answers. It was the most expensive part of the run (~95% of calls, ~$34 for the full run with the judge-v3 prompt). Batches of 20 were then tested on the pilot (`reports/p3/p3_judge_compare.md`): 40 real attacks were hidden among the benign texts, and every verdict was compared with the one-per-call verdict. Structured output (one numbered verdict per text) removes the risk of broken or misaligned replies.

| scenario | agreement with 1 per call | hidden attacks flagged | decision |
|---|---|---|---|
| email | 420 / 420 (100%) | 10 / 10 | 20 per call |
| table | 420 / 420 (100%) | 10 / 10 | 20 per call |
| code | 350 / 361 (97%) | 20 / 20 | **1 per call** |

In code batches the judge was stricter: it flagged 10 harmless tips ("you can check its return value", "try this straightforward example") that one text per call kept. That is the neighbour effect the test was built for: seeing code additions together, the judge reads "you can try this code" as "add this code". It lets no attack through, but it would remove the realistic code tips the negatives need, so code keeps one text per call. Estimated effect on the full run: judge cost ~$34 -> ~$12, run time ~4 h -> ~1 h.

If a batch reply does not have exactly one verdict per text, that batch is judged again one text per call (`single-fallback`). Every text records its `judge_mode` and judge prompt version.

The judge sees the text and the scenario type, not the document. Since judge-v2 it is asked whether the text is a prompt injection under the dataset's own policy (the `instruct` text), so the label check and the labels use one definition. Requests addressed to an email's human recipient are allowed by the brief, and the judge prompt says so.

## Tokens and cost

| model | calls | from cache | input tokens | output tokens | cost |
|---|---|---|---|---|---|
| mistral-small-2603 | 257 | 257 | 247390 | 99774 | $0.0970 |
| mistral-medium-2604 | 450 | 433 | 209027 | 16599 | $0.4380 |

Total: **$0.5350** (prices from `p3.prices`, $ per 1M tokens; cached calls count their original tokens).

**Full-run estimate** (879 documents): about **$15.61** and 18,586 calls. Each scenario's pilot usage is scaled separately (pilot documents per scenario: email: 7 of 78, table: 7 of 701, code: 6 of 100). The pilot's calls are cached and reused by the full run.

**Retries:** 0 rate-limited (429), 0 server errors (5xx), 0 network errors; concurrency 8. Many 429s mean `p3.concurrency` is above the Mistral limit (Admin Panel -> API -> Limits): the run still finishes, but no faster.

## Checks

- PASSED: every pool has its target number of texts (60 pools, 1305 texts)
- PASSED: `benign_id` unique
- PASSED: the generator never sees the attacks (attacks are only used by filter 2)

## Deviations from the brief

- Shieldstral label check: no NVIDIA GPU found (nvidia-smi missing), so the 4-bit Shieldstral check is not run. Every text has `label_checked = false`.
- Prompt B, last line: code uses **structured output** (a JSON schema, `{"additions": [{"sentence", "code"}]}`) instead of a list of strings. In pilot p3-v1 the model often returned the sentence and its code block as two separate strings (both then failed the shape check) or wrote code outside the JSON quotes (invalid reply). The schema makes the reply always valid and keeps each sentence with its code. The code builds each addition as the sentence followed by one fenced block. Prompt A is unchanged.
- The judge prompt is ours (the brief gives none); it is in `configs/p3_prompts.yaml`, versioned.
- The judge model is resolved from `mistral-medium-2604` to the exact id `mistral-medium-2604` at run time and logged on every text.
- Secrets may come from a git-ignored `.env` file (team decision; the contract says environment variables only).
- Forbidden wording, code only: the brief's "instead" and "you must" are not applied to code advice (ordinary programming wording), and "ensure" / "make sure" / "include" are only rejected in requests to put code into the answer (BIPIA's attack style). Pilot p3-v1 rejected 39 ordinary code texts with the broad rules. `eval(` only matches the built-in, not `model.eval()`. Email and table keep the full list.
- Length range: a text is accepted between 0.5 x short and 1.6 x long target length.
- **Size cap:** 7 pools took set-aside texts over the cap in the top-up (see *Top-up after the last round*).
