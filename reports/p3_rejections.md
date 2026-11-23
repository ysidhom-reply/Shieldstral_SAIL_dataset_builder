# Report: p3

## Counts (total rows: **52735**)

### scenario

| scenario | rows |
|---|---:|
| code | 5995 |
| email | 4680 |
| table | 42060 |

### split

| split | rows |
|---|---:|
| test | 18235 |
| train | 29220 |
| validation | 5280 |

### position

| position | rows |
|---|---:|
| end | 17577 |
| middle | 17580 |
| start | 17578 |

### cross

| scenario | split | position | rows |
|---|---|---|---:|
| code | test | end | 997 |
| code | test | middle | 1000 |
| code | test | start | 998 |
| code | train | end | 800 |
| code | train | middle | 800 |
| code | train | start | 800 |
| code | validation | end | 200 |
| code | validation | middle | 200 |
| code | validation | start | 200 |
| email | test | end | 880 |
| email | test | middle | 880 |
| email | test | start | 880 |
| email | train | end | 520 |
| email | train | middle | 520 |
| email | train | start | 520 |
| email | validation | end | 160 |
| email | validation | middle | 160 |
| email | validation | start | 160 |
| table | test | end | 4200 |
| table | test | middle | 4200 |
| table | test | start | 4200 |
| table | train | end | 8420 |
| table | train | middle | 8420 |
| table | train | start | 8420 |
| table | validation | end | 1400 |
| table | validation | middle | 1400 |
| table | validation | start | 1400 |

## Run

- Mode: **full** (879 documents x 3 positions = 2637 pools, 20 texts each)
- Texts generated: 88700; accepted: 52735; surplus (valid but pool already full): 7299
- Rounds needed per pool: 1 round(s): 238, 2 round(s): 1431, 3 round(s): 372, 4 round(s): 210, 5 round(s): 138, 6 round(s): 82, 7 round(s): 52, 8 round(s): 28, 9 round(s): 86
- Documents truncated for the prompt: 0 (limit 20000 chars)
- **Top-up after the last round:** 58 pools (2.2% of pools) took 117 set-aside texts (0.2% of accepted texts) over the size cap. See *Top-up after the last round*.

## Top-up after the last round (flagged)

**Flag.** After the last regeneration round, a pool still below 20 took **its own set-aside texts**: valid texts (filters 1-3 passed) that had been set aside because their size was already at the cap of 10. They were checked again for duplicates, sent to the judge (they had not reached it), and accepted over the cap. Each one has `over_size_cap: true`.

| scenario | pools topped up | texts added | sizes added |
|---|---|---|---|
| email | 26 / 234 (11.1%) | 34 | medium: 34 |
| table | 14 / 2103 (0.7%) | 23 | medium: 23 |
| code | 18 / 300 (6.0%) | 60 | long: 17, medium: 38, short: 5 |

Total: **58 of 2637 pools (2.2%)**, **117 of 52735 texts (0.2%)**.

Why (team decision): the full run left 59 of 2,637 pools 1-2 texts short after 9 rounds. In every one, medium was at the cap: asked for short or long texts, the model kept writing medium ones, and over 20,000 such valid texts were set aside. More rounds would not have changed that. Taking what the model proposed needs no new generation; the judge and the duplicate check still apply. The effect on the size mix is small (an 11th or 12th medium text in those pools), and medium is the size closest to the attacks.

## Accepted short pools (flagged deviation)

**Flag (approved deviation).** These pools end with fewer than 20 texts. Each is listed in `p3.accepted_short_pools` with its reason and approval. The step checks that each still has at least its approved minimum and at least what P4 needs (its attacked samples at that position in P2); any other short pool stops the step.

| document | position | texts | approved minimum | P4 needs | enough | reason | approved |
|---|---|---|---|---|---|---|---|
| `bipia-code-7f1bb6f33328` | start | 18 / 20 | 18 | 16 | yes | Answer about urllib.request / urlopen(URL): natural additions use urllib or URLs, which the brief forbids (rejections: URL 42, import urllib 38, socket 4, shape 21). | team, 2026-11-23 |
| `bipia-code-7f1bb6f33328` | end | 17 / 20 | 17 | 17 | yes | Answer about urllib.request / urlopen(URL): natural additions use urllib or URLs, which the brief forbids (rejections: URL 83, import urllib 29, socket/http.client 5). | team, 2026-11-23 |

Texts missing against 20 per pool: **5** (0.0% of the expected total). `expected_counts.p3_benign_pool` was lowered by the same number, with the approval noted in the config.

Why: the document is a Stack Overflow answer about `import urllib.request` and `urlopen(URL)`. Almost every natural code addition for it uses `urllib`, `socket` or a URL, and the brief forbids network code and URLs, so filter 2 rejects them. Nine rounds and the top-up did not change that, and loosening the filter for one document would break the brief's safety rule. The pool is still large enough: as a test document it has about 17 attacked samples per position (50 code attacks over 3 positions), and P4 needs one benign text per attacked sample.

## Rejection rate per filter

| filter | name | texts checked | rejected | rejection rate | share of all generated |
|---|---|---|---|---|---|
| 1 | shape | 81514 | 1897 | 2.3% | 2.1% |
| 2 | forbidden content | 79617 | 905 | 1.1% | 1.0% |
| 3 | duplicate | 78712 | 229 | 0.3% | 0.3% |
| 4 | judge | 52864 | 16 | 0.0% | 0.0% |

*Rejection rate* = rejected / texts that reached the filter. A text is counted at the first filter it fails.

## Rejections per scenario and position

| scenario | position | generated | f1 shape | f2 forbidden content | f3 duplicate | f4 judge | accepted |
|---|---|---|---|---|---|---|---|
| code | start | 4460 | 348 (7.8%) | 322 (7.8%) | 0 (0.0%) | 3 (0.1%) | 1998 |
| code | middle | 4395 | 44 (1.0%) | 295 (6.8%) | 12 (0.3%) | 1 (0.0%) | 2000 |
| code | end | 3750 | 88 (2.3%) | 254 (6.9%) | 0 (0.0%) | 4 (0.2%) | 1997 |
| email | start | 2667 | 75 (2.8%) | 4 (0.2%) | 5 (0.2%) | 0 (0.0%) | 1560 |
| email | middle | 2786 | 44 (1.6%) | 3 (0.1%) | 6 (0.2%) | 0 (0.0%) | 1560 |
| email | end | 2580 | 195 (7.6%) | 8 (0.3%) | 5 (0.2%) | 0 (0.0%) | 1560 |
| table | start | 20368 | 367 (1.8%) | 3 (0.0%) | 67 (0.3%) | 4 (0.0%) | 14020 |
| table | middle | 19623 | 441 (2.2%) | 7 (0.0%) | 68 (0.4%) | 2 (0.0%) | 14020 |
| table | end | 20885 | 295 (1.4%) | 9 (0.0%) | 66 (0.3%) | 2 (0.0%) | 14020 |

## Most common rejection reasons

| filter | reason | texts |
|---|---|---|
| shape | length N outside N-N | 764 |
| shape | invalid JSON reply | 698 |
| forbidden content | URL | 451 |
| forbidden content | forbidden code | 394 |
| shape | not one intro + exactly one fenced code block | 351 |
| duplicate | exact duplicate | 155 |
| duplicate | near duplicate (ratio N.N) | 74 |
| shape | intro is not one sentence | 50 |
| forbidden content | assistant wording | 32 |
| shape | N sentences (expected N-N) | 28 |
| forbidden content | email address | 22 |
| judge | judge (batch-N): injection | 8 |
| judge | judge: addressed to the assistant | 8 |
| shape | empty | 6 |
| forbidden content | request to include/run code | 6 |

## Length mix of accepted texts

Asked per pool: 7 short, 7 medium, 6 long. Kept per pool: at most 10 of any one size (the other sizes are regenerated).

| scenario | short | medium | long |
|---|---|---|---|
| code | 1521 (25.4%) | 2980 (49.7%) | 1494 (24.9%) |
| email | 2156 (46.1%) | 2246 (48.0%) | 278 (5.9%) |
| table | 18717 (44.5%) | 19991 (47.5%) | 3352 (8.0%) |

Length in characters, BIPIA attacks vs accepted benign texts (median; 25th-75th percentile). They should overlap, so that length does not give away the label:

| group | attacks median | attacks IQR | benign median | benign IQR |
|---|---|---|---|---|
| text | 63 | 44-77 | 48 | 37-60 |
| code | 278 | 214-337 | 271 | 215-390 |

Valid texts set aside because their size was already full in the pool: code/long: 535, code/medium: 4373, code/short: 391, email/long: 1, email/medium: 2625, email/short: 416, table/long: 4, table/medium: 13993, table/short: 3511.

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
| 1 | **Judge: hybrid** (email and table 20 texts per call, code 1 per call; first plan: 1 per call everywhere) | One text per call was the first choice (independent verdicts) but made ~95% of the calls. On the pilot, batches of 20 (structured output, 40 attacks hidden among the texts) agreed 100% with one-per-call for email and table and caught every hidden attack; for code they agreed only 97%, flagging 10 harmless tips. Hence the hybrid: judge cost ~$34 -> ~$12, time ~4 h -> ~1 h (see *Judge setup* and `reports/p3_judge_compare.md`). |
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
| 14 | **Two approved short pools** (`bipia-code-7f1bb6f33328` start 18, end 17; total 52,735 instead of 52,740) | The top-up filled 57 of the 59 short pools. The last 2 are the Stack Overflow answer about `urllib.request` / `urlopen(URL)`: its natural additions use urllib or URLs, which the brief forbids. Loosening the filter would break the safety rule; 17-18 texts still cover what P4 needs (~17 attacked samples per position). Approved by the team 2026-11-23; see *Accepted short pools*. |

## Judge setup

**Hybrid, per scenario** (`p3.judge_batch_size`, team decision): email 20 per call, table 20 per call, code 1 per call. Texts judged in this run, by mode: code/single: 6036, email/batch-20: 4732, table/batch-20: 42096.

**How it was decided.** The first plan was one text per call for everything: each verdict independent, a broken reply affects one text, no misaligned answers. It was the most expensive part of the run (~95% of calls, ~$34 for the full run with the judge-v3 prompt). Batches of 20 were then tested on the pilot (`reports/p3_judge_compare.md`): 40 real attacks were hidden among the benign texts, and every verdict was compared with the one-per-call verdict. Structured output (one numbered verdict per text) removes the risk of broken or misaligned replies.

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
| mistral-small-2603 | 7600 | 7600 | 7739921 | 2325659 | $2.5564 |
| mistral-medium-2604 | 8408 | 8408 | 4382874 | 667262 | $11.5788 |

Total: **$14.1352** (prices from `p3.prices`, $ per 1M tokens; cached calls count their original tokens).

**Retries:** 0 rate-limited (429), 0 server errors (5xx), 0 network errors; concurrency 8. Many 429s mean `p3.concurrency` is above the Mistral limit (Admin Panel -> API -> Limits): the run still finishes, but no faster.

## Checks

- PASSED: every pool has 20 accepted texts, except 2 approved short pools with enough for P4 (2637 pools, 52735 texts)
- PASSED: `benign_id` unique
- PASSED: the generator never sees the attacks (attacks are only used by filter 2)

## Deviations from the brief

- Shieldstral label check: no NVIDIA GPU found (nvidia-smi missing), so the 4-bit Shieldstral check is not run. Every text has `shieldstral_checked = false`.
- Prompt B, last line: code uses **structured output** (a JSON schema, `{"additions": [{"sentence", "code"}]}`) instead of a list of strings. In pilot p3-v1 the model often returned the sentence and its code block as two separate strings (both then failed the shape check) or wrote code outside the JSON quotes (invalid reply). The schema makes the reply always valid and keeps each sentence with its code. The code builds each addition as the sentence followed by one fenced block. Prompt A is unchanged.
- The judge prompt is ours (the brief gives none); it is in `configs/p3_prompts.yaml`, versioned.
- The judge model is resolved from `mistral-medium-2604` to the exact id `mistral-medium-2604` at run time and logged on every text.
- Secrets may come from a git-ignored `.env` file (team decision; the contract says environment variables only).
- Forbidden wording, code only: the brief's "instead" and "you must" are not applied to code advice (ordinary programming wording), and "ensure" / "make sure" / "include" are only rejected in requests to put code into the answer (BIPIA's attack style). Pilot p3-v1 rejected 39 ordinary code texts with the broad rules. `eval(` only matches the built-in, not `model.eval()`. Email and table keep the full list.
- Length range: a text is accepted between 0.5 x short and 1.6 x long target length.
- **Pool size:** the brief asks for 20 texts per document and position; 2 approved pools have fewer (see *Accepted short pools*), so the total is below 52,740 texts. Approved by the team.
- **Size cap:** 58 pools took set-aside texts over the cap in the top-up (see *Top-up after the last round*).
